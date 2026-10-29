"""Switch one member into solo mode and back."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
)
from engine.member_identity import legacy_member_id
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_START,
)

from .access import pkg



def _solo_target(value: str):
    wanted = value.strip()
    if not wanted:
        raise ConfigError("solo member is required")
    matches = []
    for name, path in discover_profiles().items():
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError):
            continue
        labels = {
            name.casefold(),
            config.channel.name.casefold(),
            config.channel.id.casefold(),
            *(alias.casefold() for alias in config.channel.aliases),
        }
        if wanted.casefold() in labels:
            matches.append((name, config))
    if not matches:
        raise ConfigError(f"unknown solo member: {wanted}")
    if len(matches) > 1:
        raise ConfigError(
            "solo member is ambiguous: " + ", ".join(name for name, _ in matches)
        )
    return matches[0]



def _default_solo_config():
    profiles = discover_profiles()
    for name in ("aki", *profiles):
        path = profiles.get(name)
        if path is None:
            continue
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError):
            continue
        if config.mqtt_enabled:
            return config
    raise ConfigError("no MQTT-enabled profile is available")



def _solo_control_state_path() -> Path:
    return Path.home() / ".aki" / "solo" / "control.json"



def _write_solo_control_state(result: dict[str, Any]) -> None:
    from engine.aki_config import atomic_write

    path = _solo_control_state_path()
    atomic_write(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")



def _read_solo_control_state() -> dict[str, Any] | None:
    try:
        data = json.loads(_solo_control_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None



def _clear_solo_control_state() -> None:
    try:
        _solo_control_state_path().unlink()
    except FileNotFoundError:
        pass



def _latest_solo_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest retained event that determines the solo state."""
    for item in reversed(events):
        if item.get("kind") in (SOLO_START, SOLO_ACTIVE, SOLO_END):
            return item
    return None



async def _solo_broker_call(
    config,
    *,
    action: str,
    target=None,
    session_id: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    from engine.mqtt_client import MqttClient
    from engine.solo_mode import SOLO_READY, derive_solo_magic
    from engine.tokifactor import (
        CONTROL_TOPIC,
        decode_single_payload,
        encode_text_payloads,
    )

    sender_id = legacy_member_id("akictl")
    events: list[dict[str, str]] = []
    ready: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def on_message(_topic: str, payload: bytes, _replayed: bool) -> None:
        try:
            envelope = decode_single_payload(payload.decode("utf-8"), config.mqtt_magic)
        except (UnicodeDecodeError, ValueError):
            return
        if envelope is None:
            return
        metadata = dict(envelope.metadata)
        if metadata.get("schema") != "akid.v1":
            return
        if metadata.get("kind") not in (
            SOLO_START,
            SOLO_READY,
            SOLO_END,
            "solo_active",
        ):
            return
        events.append(metadata)
        if (
            action == "start"
            and metadata.get("kind") == SOLO_READY
            and metadata.get("soloId") == session_id
            and not ready.done()
        ):
            ready.set_result(metadata)

    client = MqttClient(
        config.mqtt_host,
        config.mqtt_port,
        client_id=f"akictl-solo-{os.getpid()}-{int(time.time())}",
        clean_session=True,
        on_message=on_message,
    )
    await client.connect(timeout=timeout)
    try:
        granted = await client.subscribe(CONTROL_TOPIC, qos=1)
        if granted == 0x80:
            raise RuntimeError("broker rejected control subscription")
        if action == "status":
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            latest = _latest_solo_event(events)
            if latest is not None:
                if latest.get("kind") == SOLO_END:
                    _clear_solo_control_state()
                    return {"active": False, "session": None, "source": "broker"}
                return {"active": True, "session": latest, "source": "broker"}
            local = _read_solo_control_state()
            if local is not None:
                return {"active": True, "session": local, "source": "local"}
            return {"active": False, "session": None, "source": "broker"}

        if action == "start" and target is not None:
            started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            metadata = {
                "schema": "akid.v1",
                "kind": SOLO_START,
                "protocolVersion": "1",
                "memberId": sender_id,
                "memberName": "akictl",
                "sourceId": f"akictl:{socket.gethostname()}",
                "memberKind": "user",
                "soloId": session_id,
                "soloTargetId": target.channel.id,
                "soloTargetName": target.channel.name,
                "soloStartedAt": started_at,
            }
            for payload in encode_text_payloads(
                "akictl solo start",
                config.mqtt_magic,
                sender="akictl",
                sender_id=sender_id,
                metadata=metadata,
            ):
                await client.publish(
                    CONTROL_TOPIC, payload.encode("utf-8"), qos=1, retain=True
                )
            try:
                result = await asyncio.wait_for(ready, timeout=timeout)
            except asyncio.TimeoutError as exc:
                abort = {
                    **metadata,
                    "kind": SOLO_END,
                }
                for payload in encode_text_payloads(
                    "akictl solo abort",
                    config.mqtt_magic,
                    sender="akictl",
                    sender_id=sender_id,
                    metadata=abort,
                ):
                    await client.publish(
                        CONTROL_TOPIC, payload.encode("utf-8"), qos=1, retain=True
                    )
                raise TimeoutError(
                    f"solo target {target.channel.name} did not become ready"
                ) from exc
            return {
                "active": True,
                "session_id": session_id,
                "target_profile": target.profile,
                "target_id": target.channel.id,
                "target_name": target.channel.name,
                "solo_magic": derive_solo_magic(config.mqtt_magic, target.channel.id),
                "ready": dict(result),
            }

        if action == "off":
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline and not events:
                await asyncio.sleep(0.05)
            latest = _latest_solo_event(events)
            if latest is not None and latest.get("kind") == SOLO_END:
                _clear_solo_control_state()
                return {"active": False, "session": None}
            active = latest
            local = _read_solo_control_state()
            if not active and target is None and local is not None:
                active = local
            if not active and target is None:
                return {"active": False, "session": None}
            target_id = (
                str(active.get("soloTargetId") or active.get("target_id") or "")
                if active
                else target.channel.id
            )
            target_name = (
                str(
                    active.get("soloTargetName")
                    or active.get("target_name")
                    or target_id
                )
                if active
                else target.channel.name
            )
            solo_id = (
                str(active.get("soloId") or active.get("session_id") or "")
                if active
                else ""
            )
            if not target_id:
                raise RuntimeError("active solo state is incomplete")
            for magic in (
                derive_solo_magic(config.mqtt_magic, target_id),
                config.mqtt_magic,
            ):
                metadata = {
                    "schema": "akid.v1",
                    "kind": SOLO_END,
                    "protocolVersion": "1",
                    "memberId": sender_id,
                    "memberName": "akictl",
                    "sourceId": f"akictl:{socket.gethostname()}",
                    "memberKind": "user",
                    "soloId": solo_id,
                    "soloTargetId": target_id,
                    "soloTargetName": target_name,
                }
                for payload in encode_text_payloads(
                    "akictl solo end",
                    magic,
                    sender="akictl",
                    sender_id=sender_id,
                    metadata=metadata,
                ):
                    await client.publish(
                        CONTROL_TOPIC,
                        payload.encode("utf-8"),
                        qos=1,
                        retain=True,
                    )
            return {
                "active": False,
                "ended_session_id": solo_id,
                "ended_target_id": target_id,
                "target_id": target_id,
                "target_name": target_name,
            }
        raise ValueError(f"unsupported solo action: {action}")
    finally:
        await client.close()



def _solo(args: argparse.Namespace) -> int:
    command = (args.solo_command or "status").strip()
    try:
        if command == "status":
            result = asyncio.run(
                _solo_broker_call(
                    _default_solo_config(), action="status", timeout=args.timeout
                )
            )
        elif command == "off":
            _, target = _solo_target(args.solo_target) if args.solo_target else (None, None)
            result = asyncio.run(
                _solo_broker_call(
                    _default_solo_config(),
                    action="off",
                    target=target,
                    timeout=args.timeout,
                )
            )
        else:
            name, target = _solo_target(command)
            result = asyncio.run(
                _solo_broker_call(
                    target,
                    action="start",
                    target=target,
                    session_id=f"solo-{uuid.uuid4().hex}",
                    timeout=args.timeout,
                )
            )
            result["target_profile"] = name
            _write_solo_control_state(result)
        if command == "off" and result.get("ended_target_id"):
            _clear_solo_control_state()
    except (ConfigError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        pkg().print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("active"):
        session = result.get("session") or result
        pkg().print(
            "solo active: "
            f"{session.get('soloTargetName') or session.get('target_name')} "
            f"session={session.get('soloId') or session.get('session_id')}"
        )
    elif command == "off":
        pkg().print(
            "solo mode ended"
            if result.get("ended_session_id")
            or result.get("ended_target_id")
            else "solo mode is not active"
        )
    else:
        pkg().print("solo mode is not active")
    return 0
