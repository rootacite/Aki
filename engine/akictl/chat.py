"""MQTT chat session and local channel history display."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from engine.aki_config import ConfigError
from engine.client import (
    GatewayClient,
    _memory_command,
    _print_json,
)
from engine.channel_history import (
    channel_history_key,
    channel_history_path,
    read_channel_history,
)
from engine.member_identity import IdentityRegistry
from engine.protocol import RpcError
from .service import _profile_config
from .support import (
    _mcp_call,
    _model_call,
    _mqtt_call,
    _print_error,
    _reasoning_call,
    _rpc_call,
    _socket_from,
)

from .access import pkg


def _parse_compact_args(tokens: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for token in tokens:
        if token == "--omit-tool-results":
            params["omit_tool_results"] = True
        elif "mode" not in params:
            params["mode"] = token
    return params



def _channel_history_file(args: argparse.Namespace, profile: str) -> Path:
    config = _profile_config(profile, getattr(args, "config_path", None))
    key = channel_history_key(
        host=config.mqtt_host,
        port=config.mqtt_port,
        topic=config.mqtt_topic,
        magic=config.mqtt_magic,
    )
    root = config.path.parent / "channel-history"
    return channel_history_path(
        root,
        key=key,
        profile=profile,
        solo=bool(getattr(args, "solo", False)),
    )



def _print_channel_history(entries: list[dict[str, Any]], limit: int) -> None:
    for entry in entries[-max(1, limit) :]:
        timestamp = str(entry.get("timestamp") or "")
        sent_at = entry.get("sent_at")
        if isinstance(sent_at, (int, float)) and sent_at > 0:
            timestamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(sent_at) / 1000.0)
            )
        sender = str(entry.get("sender") or "unknown")
        phase = str(entry.get("phase") or "final")
        suffix = "" if phase == "final" else f" [{phase}]"
        text = str(entry.get("text") or "")
        pkg().print(f"[{timestamp}] {sender}{suffix}: {text}")



def _print_history_file(path: Path, *, limit: int, json_mode: bool = False) -> int:
    entries = read_channel_history(path)
    if json_mode:
        for entry in entries[-max(1, limit) :]:
            pkg().print(json.dumps(entry, ensure_ascii=False))
        return 0
    if not entries:
        pkg().print(f"no local channel history at {path}", file=sys.stderr)
        return 1
    _print_channel_history(entries, limit)
    return 0



def _unique_user_identity() -> tuple[str, str]:
    path = Path.home() / ".aki" / "identities.json"
    users = [
        identity
        for identity in IdentityRegistry(path).identities().values()
        if identity.kind == "user"
    ]
    if len(users) != 1:
        raise ConfigError(
            "MQTT chat needs exactly one kind=user identity in "
            f"{path}; found {len(users)}"
        )
    identity = users[0]
    return identity.id, identity.name



async def _mqtt_chat_once(
    config,
    text: str,
    *,
    sender_id: str,
    sender_name: str,
    client_message_id: str,
    json_mode: bool,
    timeout: float,
) -> int:
    from engine.mqtt_client import MqttClient
    from engine.tokifactor import decode_single_payload, encode_text_payloads

    turn_ids: set[str] = set()
    ended_turns: set[str] = set()
    last_event_at = time.monotonic()
    saw_event = False
    quiet_seconds = 2.0

    async def on_message(_topic: str, payload: bytes, _replayed: bool) -> None:
        nonlocal last_event_at, saw_event
        try:
            envelope = decode_single_payload(payload.decode("utf-8"), config.mqtt_magic)
        except (UnicodeDecodeError, ValueError):
            return
        if envelope is None:
            return
        metadata = dict(envelope.metadata)
        turn_id = str(metadata.get("turnId") or "")
        event_client_id = str(metadata.get("clientMessageId") or "")
        if not turn_id:
            return
        if event_client_id != client_message_id and turn_id not in turn_ids:
            return
        turn_ids.add(turn_id)
        saw_event = True
        last_event_at = time.monotonic()
        kind = str(metadata.get("kind") or "")
        phase = str(metadata.get("phase") or "")
        name = envelope.sender or str(metadata.get("memberName") or "agent")
        if phase in ("ended", "no_reply"):
            ended_turns.add(turn_id)
        if json_mode:
            pkg().print(
                json.dumps(
                    {
                        "sender": envelope.sender,
                        "text": envelope.text,
                        **metadata,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        if kind == "turn" and phase == "progress":
            if envelope.text.strip():
                pkg().print(f"[{name}] {envelope.text}", flush=True)
        elif kind == "turn" and phase == "final":
            if envelope.text.strip():
                pkg().print(envelope.text, flush=True)
        elif kind == "turn" and phase == "no_reply":
            pkg().print(f"[{name}] (no reply)", flush=True)
        elif kind == "error":
            pkg().print(
                f"[{name}] error: {envelope.text or metadata.get('details') or 'unknown'}",
                file=sys.stderr,
                flush=True,
            )

    client = MqttClient(
        config.mqtt_host,
        config.mqtt_port,
        client_id=f"akictl-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        clean_session=True,
        on_message=on_message,
    )
    await client.connect(timeout=min(timeout, 15.0))
    try:
        granted = await client.subscribe(config.mqtt_topic, qos=1)
        if granted == 0x80:
            pkg().print("broker rejected channel subscription", file=sys.stderr)
            return 1
        metadata = {
            "schema": "akid.v1",
            "kind": "message",
            "clientMessageId": client_message_id,
            "memberId": sender_id,
            "memberName": sender_name,
            "memberKind": "user",
            "protocolVersion": "1",
        }
        try:
            for payload in encode_text_payloads(
                text,
                config.mqtt_magic,
                sender=sender_name,
                sender_id=sender_id,
                metadata=metadata,
            ):
                await client.publish(
                    config.mqtt_topic, payload.encode("utf-8"), qos=1
                )
        except KeyboardInterrupt:
            await _publish_mqtt_cancel(
                client,
                config,
                sender_id=sender_id,
                sender_name=sender_name,
                client_message_id=client_message_id,
            )
            return 1
        try:
            deadline = time.monotonic() + max(1.0, timeout)
            while time.monotonic() < deadline:
                if (
                    saw_event
                    and turn_ids
                    and turn_ids.issubset(ended_turns)
                    and time.monotonic() - last_event_at >= quiet_seconds
                ):
                    return 0
                await asyncio.sleep(0.05)
        except KeyboardInterrupt:
            await _publish_mqtt_cancel(
                client,
                config,
                sender_id=sender_id,
                sender_name=sender_name,
                client_message_id=client_message_id,
            )
            return 1
        if turn_ids and not turn_ids.issubset(ended_turns):
            pkg().print("timed out waiting for turn_end", file=sys.stderr)
            return 1
        if not saw_event:
            pkg().print("no agent responded before timeout", file=sys.stderr)
            return 1
        return 0
    finally:
        await client.close()



async def _publish_mqtt_cancel(
    client,
    config,
    *,
    sender_id: str,
    sender_name: str,
    client_message_id: str,
) -> None:
    from engine.tokifactor import encode_text_payloads

    metadata = {
        "schema": "akid.v1",
        "kind": "cancel",
        "clientMessageId": client_message_id,
        "memberId": sender_id,
        "memberName": sender_name,
        "memberKind": "user",
        "protocolVersion": "1",
    }
    try:
        for payload in encode_text_payloads(
            "akictl cancel",
            config.mqtt_magic,
            sender=sender_name,
            sender_id=sender_id,
            metadata=metadata,
        ):
            await client.publish(config.mqtt_topic, payload.encode("utf-8"), qos=1)
    except Exception as exc:
        pkg().print(f"cancel failed: {exc}", file=sys.stderr)



def _mqtt_chat(
    config,
    prompt: str,
    *,
    client_message_id: str | None,
    json_mode: bool,
    timeout: float = 180.0,
) -> int:
    try:
        sender_id, sender_name = _unique_user_identity()
    except (ConfigError, OSError, ValueError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 1
    message_id = client_message_id or f"cli-{uuid.uuid4().hex}"
    try:
        return asyncio.run(
            _mqtt_chat_once(
                config,
                prompt,
                sender_id=sender_id,
                sender_name=sender_name,
                client_message_id=message_id,
                json_mode=json_mode,
                timeout=timeout,
            )
        )
    except KeyboardInterrupt:
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 1



def repl(
    client: GatewayClient,
    config,
    profile: str,
    *,
    json_mode: bool,
    show_reasoning: bool,
    timeout: float = 180.0,
) -> int:
    pkg().print(
        "akictl MQTT REPL. Commands: /status /health /history [n] /allow <dir> "
        "/disallow <dir> /allowlist /memory ... /mqtt ... /mcp ... /model [set <id>] "
        "/reasoning [set <level>] /avatar /username /set-username <name> "
        "/recompile /compact [mode] [--omit-tool-results] /reset /quit",
        file=sys.stderr,
    )
    while True:
        try:
            line = input("you> ")
        except EOFError:
            pkg().print("", file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            pkg().print("", file=sys.stderr)
            continue
        prompt = line.strip()
        if not prompt:
            continue
        if prompt in ("/quit", "/exit"):
            return 0
        try:
            if prompt == "/status":
                _print_json(_rpc_call(client, "aki.status"))
            elif prompt == "/health":
                _print_json(_rpc_call(client, "aki.health"))
            elif prompt.startswith("/history"):
                parts = prompt.split()
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
                solo = "--solo" in parts
                history_args = argparse.Namespace(
                    config_path=None, solo=solo
                )
                path = _channel_history_file(history_args, profile)
                _print_history_file(path, limit=limit)
            elif prompt.startswith("/allowlist"):
                _print_json(_rpc_call(client, "aki.fs.writable.list"))
            elif prompt.startswith("/allow "):
                _print_json(
                    _rpc_call(client, "aki.fs.writable.add", {"path": prompt[7:].strip()})
                )
            elif prompt.startswith("/disallow "):
                _print_json(
                    _rpc_call(
                        client,
                        "aki.fs.writable.remove",
                        {"path": prompt[10:].strip()},
                    )
                )
            elif prompt.startswith("/memory"):
                _memory_command(client, prompt.split()[1:])
            elif prompt.startswith("/mqtt"):
                rest = prompt.split()[1:]
                _print_json(_mqtt_call(client, rest))
            elif prompt.startswith("/mcp"):
                rest = prompt.split()[1:]
                _print_json(_mcp_call(client, rest))
            elif prompt.startswith("/reasoning"):
                rest = prompt.split()[1:]
                _print_json(_reasoning_call(client, rest))
            elif prompt.startswith("/model"):
                rest = prompt.split()[1:]
                _print_json(_model_call(client, rest))
            elif prompt == "/avatar":
                _print_json(_rpc_call(client, "aki.avatar.status"))
            elif prompt == "/reset":
                _print_json(_rpc_call(client, "aki.conversation.reset"))
            elif prompt == "/username":
                _print_json(_rpc_call(client, "aki.username.get"))
            elif prompt.startswith("/set-username "):
                _print_json(
                    _rpc_call(
                        client,
                        "aki.username.set",
                        {"username": prompt[14:].strip()},
                    )
                )
            elif prompt == "/recompile":
                _print_json(_rpc_call(client, "aki.conversation.recompile"))
            elif prompt == "/compact" or prompt.startswith("/compact "):
                rest = prompt.split()[1:]
                params = _parse_compact_args(rest)
                _print_json(
                    _rpc_call(
                        client,
                        "aki.conversation.compact",
                        params,
                        timeout=930.0,
                    )
                )
            else:
                _mqtt_chat(
                    config,
                    line,
                    client_message_id=None,
                    json_mode=json_mode,
                    timeout=timeout,
                )
        except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
            _print_error(exc)



def _connect(socket_path: Path, *, json_mode: bool, client_name: str = "akictl") -> GatewayClient:
    raw_lock = threading.Lock()

    def raw_sink(line: str) -> None:
        with raw_lock:
            pkg().print(line, flush=True)

    client = GatewayClient(socket_path, client_name=client_name, raw_sink=raw_sink if json_mode else None)
    client.connect()
    client.handshake()
    return client



def _chat(args: argparse.Namespace, profile: str = "aki") -> int:
    try:
        config = _profile_config(profile, getattr(args, "config_path", None))
    except (OSError, ValueError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 1
    prompt = args.prompt
    if prompt is None and args.prompt_text:
        prompt = " ".join(args.prompt_text)
    if prompt is not None:
        return _mqtt_chat(
            config,
            prompt,
            client_message_id=args.client_message_id,
            json_mode=args.json,
            timeout=args.timeout,
        )
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        return repl(
            client,
            config,
            profile,
            json_mode=args.json,
            show_reasoning=args.include_reasoning,
            timeout=args.timeout,
        )
    finally:
        client.close()
