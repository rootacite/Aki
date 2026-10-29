#!/usr/bin/env python3
"""Run an isolated 3-profile prompt-prefix audit through the proxy debug socket."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.aki_config import load_config, resolve_config_path  # noqa: E402
from engine.channel_history import channel_history_key  # noqa: E402
from engine.akid.constants import CHANNEL_MEMBER_TIMEOUT  # noqa: E402
from engine.live_context import estimate_tokens  # noqa: E402
from engine.proxy_debug import default_socket_path  # noqa: E402
from engine.solo_mode import default_solo_state_path  # noqa: E402


TEST_PREFIX = "CACHE-TEST"
DYNAMIC_TAGS = (
    "<turn-control",
    "<channel-transcript",
    "<solo-mode",
    "<solo-memories",
    "<solo-transcript",
    "<live-context",
    "<wait-user",
    "<wait-other",
)
STABLE_TAGS = (
    "<tool-ownership",
    "<channel-policy",
    "<channel-wake-routing",
    "<channel-identity",
    "<channel-context>",
    "<format-guard",
)


def _run(command: Sequence[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), text=True, capture_output=True, timeout=timeout, check=False
    )


def _systemctl(*args: str, timeout: float = 60.0) -> None:
    result = _run(["systemctl", "--user", *args], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}"
        )


def _akictl(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return _run(["akictl", *args], timeout=timeout)


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _profile_config(profile: str):
    _name, path = resolve_config_path(profile)
    return load_config(path, profile=profile)


def _ensure_profiles_not_solo(profiles: Sequence[str]) -> None:
    for profile in profiles:
        path = default_solo_state_path(profile)
        if path.is_file():
            raise RuntimeError(
                f"profile {profile} is in solo mode ({path}); exit solo before running the audit"
            )


def _conversation_dir(config) -> Path:
    backend = Path(
        os.environ.get("AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend")
    ).expanduser()
    return backend / "conversations" / _b64url(f"{config.conversation}:{config.agent_id}")


def _channel_history_root(config) -> Path:
    return config.path.parent / "channel-history"


def _memory_dir(config) -> Path:
    backend = Path(
        os.environ.get("AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend")
    ).expanduser()
    return backend / "memfs" / config.agent_id / "memory"


def _snapshot(profiles: Sequence[str], run_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = {"profiles": {}, "channel_history": ""}
    all_configs = {profile: _profile_config(profile) for profile in profiles}
    first = next(iter(all_configs.values()))
    history_root = _channel_history_root(first)
    history_backup = run_dir / "channel-history"
    if history_root.is_dir():
        shutil.copytree(history_root, history_backup, symlinks=True)
        manifest["channel_history"] = str(history_backup)
    for profile, config in all_configs.items():
        root = run_dir / "profiles" / profile
        root.mkdir(parents=True, exist_ok=True)
        conversation = _conversation_dir(config)
        memory = _memory_dir(config)
        if conversation.is_dir():
            shutil.copytree(conversation, root / "conversation", symlinks=True)
        if memory.is_dir():
            shutil.copytree(memory, root / "memory", symlinks=True)
        manifest["profiles"][profile] = {
            "config": str(config.path),
            "agent_id": config.agent_id,
            "conversation_id": config.conversation,
            "conversation_dir": str(conversation),
            "memory_dir": str(memory),
        }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _restore(manifest: Mapping[str, Any], profiles: Sequence[str]) -> None:
    history_backup = Path(str(manifest.get("channel_history") or ""))
    if history_backup.is_dir():
        history_root = _channel_history_root(_profile_config(profiles[0]))
        if history_root.exists():
            shutil.rmtree(history_root)
        shutil.copytree(history_backup, history_root, symlinks=True)
    for profile in profiles:
        item = (manifest.get("profiles") or {}).get(profile) or {}
        root = history_backup.parent / "profiles" / profile
        conversation = Path(str(item.get("conversation_dir") or ""))
        memory = Path(str(item.get("memory_dir") or ""))
        if (root / "conversation").is_dir() and conversation:
            if conversation.exists():
                shutil.rmtree(conversation)
            conversation.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root / "conversation", conversation, symlinks=True)
        if (root / "memory").is_dir() and memory:
            if memory.exists():
                shutil.rmtree(memory)
            memory.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(root / "memory", memory, symlinks=True)


def _enable_debug(profiles: Sequence[str], run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    base = Path.home() / ".config" / "systemd" / "user"
    for profile in profiles:
        dropin = base / f"akid-proxy@{profile}.service.d" / "90-prompt-cache-audit.conf"
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text('[Service]\nEnvironment="AKI_PROXY_DEBUG_SOCKET=1"\n', encoding="utf-8")
        paths.append(dropin)
        capture = base / f"akid@{profile}.service.d" / "90-prompt-cache-audit.conf"
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text(
            '[Service]\nEnvironment="AKI_API_CALL_CAPTURE='
            + str(run_dir / "bridge")
            + '"\n',
            encoding="utf-8",
        )
        paths.append(capture)
    _systemctl("daemon-reload")
    for profile in profiles:
        _systemctl("restart", f"akid-proxy@{profile}.service")
    return paths


def _disable_debug(paths: Sequence[Path], profiles: Sequence[str]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()
    _systemctl("daemon-reload")
    for profile in profiles:
        _systemctl("restart", f"akid-proxy@{profile}.service")


def _bridge_payloads(
    profile: str,
    run_dir: Path,
    run_id: str,
    turns: int,
) -> list[tuple[str, dict[str, Any]]]:
    config = _profile_config(profile)
    safe_agent = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in config.agent_id
    )
    path = run_dir / "bridge" / f"api-calls-{safe_agent}.jsonl"
    if not path.is_file():
        raise RuntimeError(f"bridge capture missing: {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    selected: list[tuple[str, dict[str, Any]]] = []
    for index in range(1, turns + 1):
        marker = f"{TEST_PREFIX}-{run_id}-{profile}-{index}"
        matches = [
            record
            for record in records
            if marker in json.dumps(record.get("messages"), ensure_ascii=False)
        ]
        if not matches:
            raise RuntimeError(f"missing bridge capture for {marker}")
        record = matches[0]
        system = str(record.get("system_prompt") or "")
        mid = str(record.get("mid_conversation_system_prompt") or "")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(record.get("messages") or [])
        if mid:
            messages.append({"role": "system", "content": mid})
        selected.append(
            (
                marker,
                {
                    "model": record.get("model"),
                    "messages": messages,
                    "tools": record.get("tools"),
                },
            )
        )
    return selected


def _switch_aki_model() -> dict[str, str]:
    current = _akictl("--profile", "aki", "model")
    before = current.stdout.strip()
    switched = _akictl("--profile", "aki", "model", "set", "gemini-3.8-flash")
    if switched.returncode != 0:
        raise RuntimeError(f"failed to switch Aki model: {switched.stderr.strip()}")
    verified = _akictl("--profile", "aki", "model")
    if "gemini-3.8-flash" not in verified.stdout:
        raise RuntimeError("Aki model did not switch to gemini-3.8-flash")
    return {"before": before, "after": verified.stdout.strip()}


def _wait_ready(profile: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _akictl("--profile", profile, "status", "--json", timeout=10.0)
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except ValueError:
                payload = {}
            health = payload.get("health") if isinstance(payload, dict) else None
            if isinstance(health, dict) and health.get("channel_ready"):
                return
        time.sleep(1.0)
    raise RuntimeError(f"profile {profile} did not become ready")


def _collect_test_message_ids(profiles: Sequence[str]) -> set[str]:
    ids: set[str] = set()
    config = _profile_config(profiles[0])
    root = _channel_history_root(config)
    if not root.is_dir():
        return ids
    for path in root.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if TEST_PREFIX not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("message_id"):
                ids.add(str(row["message_id"]))
    return ids


def _mark_seen(profiles: Sequence[str], message_ids: set[str]) -> None:
    if not message_ids:
        return
    for profile in profiles:
        config = _profile_config(profile)
        key = channel_history_key(
            host=config.mqtt_host,
            port=config.mqtt_port,
            topic=config.mqtt_topic,
            magic=config.mqtt_magic,
        )
        path = _channel_history_root(config) / key / f"{profile}.seen.json"
        data: dict[str, Any] = {"version": 1, "ids": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except ValueError:
                pass
        ids = [str(value) for value in data.get("ids", []) if value]
        existing = set(ids)
        for message_id in sorted(message_ids):
            if message_id not in existing:
                ids.append(message_id)
                existing.add(message_id)
        data["version"] = 1
        data["ids"] = ids
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def _capture_debug(profile: str, output: Path, stop: threading.Event) -> None:
    socket_path = default_socket_path(profile)
    deadline = time.monotonic() + 30.0
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not socket_path.exists():
        raise RuntimeError(f"debug socket not found: {socket_path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        client.connect(str(socket_path))
        with output.open("w", encoding="utf-8") as handle:
            while not stop.is_set():
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                handle.write(chunk.decode("utf-8", errors="replace"))
                handle.flush()


def _message_text(profile: str, run_id: str, index: int) -> tuple[str, str]:
    config = _profile_config(profile)
    name = config.channel.name or profile.capitalize()
    marker = f"{TEST_PREFIX}-{run_id}-{profile}-{index}"
    return marker, (
        f"@{name} [{marker}] 只回复 ACK-{index}。不要调用任何工具，不要修改记忆，"
        "不要 @ 或 ^ 其他成员。"
    )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _lcp_chars(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def _requests_from_debug(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "request" and isinstance(event.get("payload"), dict):
            rows.append(event)
    return rows


def _normalize_debug_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("messages"), list):
        return dict(payload)
    raw_input = payload.get("input")
    if not isinstance(raw_input, list):
        return dict(payload)
    messages: list[dict[str, Any]] = []
    split_done = False
    for item in raw_input:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "user")
        content = _extract_text(item.get("content"))
        if role in ("system", "developer") and not split_done:
            split_done = True
            starts = [content.find(tag) for tag in DYNAMIC_TAGS if tag in content]
            split = min(starts) if starts else len(content)
            messages.append({"role": "system", "content": content[:split]})
            if split < len(content):
                messages.append({"role": "system", "content": content[split:]})
            continue
        messages.append({"role": role, "content": content})
    return {
        "model": payload.get("model"),
        "messages": messages,
        "tools": payload.get("tools"),
    }


def _turn_payloads(
    events: Sequence[dict[str, Any]], profile: str, run_id: str, turns: int
) -> list[tuple[str, dict[str, Any]]]:
    selected: list[tuple[str, dict[str, Any]]] = []
    for index in range(1, turns + 1):
        marker = f"{TEST_PREFIX}-{run_id}-{profile}-{index}"
        matches = [
            event for event in events if marker in json.dumps(event.get("payload"), ensure_ascii=False)
        ]
        if not matches:
            raise RuntimeError(f"missing debug request for {marker}")
        event = matches[0]
        payload = event["payload"]
        selected.append((marker, _normalize_debug_payload(payload)))
    return selected


def _report_profile(
    profile: str, payloads: Sequence[tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    bodies: list[str] = []
    systems: list[str] = []
    mid_systems: list[str] = []
    for marker, payload in payloads:
        body = json.dumps(
            {
                "messages": payload.get("messages") or [],
                "tools": payload.get("tools"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        bodies.append(body)
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        system = next(
            (
                _extract_text(message.get("content"))
                for message in messages
                if isinstance(message, dict) and message.get("role") == "system"
            ),
            "",
        )
        systems.append(system)
        mid_systems.extend(
            _extract_text(message.get("content"))
            for message in messages[1:]
            if isinstance(message, dict) and message.get("role") == "system"
        )
        if any(tag in system for tag in DYNAMIC_TAGS):
            raise RuntimeError(f"{profile} system prompt contains dynamic blocks")
        if not all(tag in system for tag in STABLE_TAGS):
            raise RuntimeError(f"{profile} system prompt is missing a stable block")
    if not any("<turn-control" in system for system in mid_systems):
        raise RuntimeError(f"{profile} mid-system tail is missing turn-control")
    hashes = {hashlib.sha256(system.encode()).hexdigest() for system in systems}
    system_stable = len(hashes) == 1
    pairs: list[dict[str, Any]] = []
    for left_index in range(len(bodies)):
        for right_index in range(left_index + 1, len(bodies)):
            left, right = bodies[left_index], bodies[right_index]
            lcp = _lcp_chars(left, right)
            prefix = left[:lcp]
            pairs.append(
                {
                    "left": left_index + 1,
                    "right": right_index + 1,
                    "lcp_chars": lcp,
                    "lcp_tokens": estimate_tokens(prefix),
                    "char_ratio": lcp / max(1, min(len(left), len(right))),
                    "token_ratio": estimate_tokens(prefix)
                    / max(1, min(estimate_tokens(left), estimate_tokens(right))),
                }
            )
    return {
        "profile": profile,
        "turns": [marker for marker, _payload in payloads],
        "system_sha256": next(iter(hashes)),
        "system_stable": system_stable,
        "system_lengths": [len(system) for system in systems],
        "pairs": pairs,
        "median_lcp_tokens": sorted(pair["lcp_tokens"] for pair in pairs)[len(pairs) // 2],
    }


def _check_rollback(profiles: Sequence[str], manifest: Mapping[str, Any]) -> None:
    for profile in profiles:
        item = (manifest.get("profiles") or {}).get(profile) or {}
        conversation = Path(str(item.get("conversation_dir") or ""))
        for path in (
            conversation / "messages.jsonl",
            _channel_history_root(_profile_config(profile)),
        ):
            if path.is_file() and TEST_PREFIX in path.read_text(encoding="utf-8", errors="replace"):
                raise RuntimeError(f"test marker remained after rollback: {path}")
            if path.is_dir():
                for child in path.rglob("*.jsonl"):
                    if TEST_PREFIX in child.read_text(encoding="utf-8", errors="replace"):
                        raise RuntimeError(f"test marker remained after rollback: {child}")


def _stabilize_rollback(profiles: Sequence[str], manifest: Mapping[str, Any]) -> None:
    for _attempt in range(2):
        time.sleep(3.0)
        replayed = _collect_test_message_ids(profiles)
        if not replayed:
            return
        for profile in profiles:
            with contextlib.suppress(Exception):
                _systemctl("stop", f"akid@{profile}.service")
        _restore(manifest, profiles)
        _mark_seen(profiles, replayed)
        for profile in profiles:
            with contextlib.suppress(Exception):
                _systemctl("start", f"akid@{profile}.service")
    time.sleep(3.0)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", nargs="+", default=["aki", "fuyu", "haru"])
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:8])
    parser.add_argument("--out", type=Path, default=Path("/tmp/aki-prompt-audit"))
    parser.add_argument("--label", default="run")
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    run_dir = args.out.expanduser() / f"{args.run_id}-{args.label}"
    run_dir.mkdir(parents=True, exist_ok=False)
    profiles = list(args.profiles)
    debug_paths: list[Path] = []
    manifest: dict[str, Any] | None = None
    report: dict[str, Any] = {"run_id": args.run_id, "label": args.label, "profiles": {}}
    try:
        _ensure_profiles_not_solo(profiles)
        model = _switch_aki_model()
        report["model"] = model
        for profile in profiles:
            _systemctl("stop", f"akid@{profile}.service")
        manifest = _snapshot(profiles, run_dir)
        debug_paths = _enable_debug(profiles, run_dir)
        for profile in profiles:
            _systemctl("start", f"akid@{profile}.service")
            _wait_ready(profile)
            # Let channel presence from previously stopped profiles expire so
            # channel-context is identical for every measured turn.
            time.sleep(CHANNEL_MEMBER_TIMEOUT + 5.0)
            capture_path = run_dir / f"{profile}-debug.jsonl"
            stop = threading.Event()
            capture = threading.Thread(
                target=_capture_debug,
                args=(profile, capture_path, stop),
                daemon=True,
            )
            capture.start()
            for index in range(1, args.turns + 1):
                marker, text = _message_text(profile, args.run_id, index)
                time.sleep(5.0 if index == 1 else 1.0)
                result = _akictl("--profile", profile, "-p", text, "--json", timeout=args.turn_timeout)
                if result.returncode != 0:
                    raise RuntimeError(f"{profile} turn {index} failed: {result.stderr.strip()}")
            stop.set()
            capture.join(timeout=10.0)
            _systemctl("stop", f"akid@{profile}.service")
            events = _requests_from_debug(capture_path)
            try:
                payloads = _bridge_payloads(profile, run_dir, args.run_id, args.turns)
                payload_source = "bridge"
            except RuntimeError:
                payloads = _turn_payloads(events, profile, args.run_id, args.turns)
                payload_source = "debug"
            report["profiles"][profile] = _report_profile(profile, payloads)
            report["profiles"][profile]["debug_request_events"] = len(events)
            report["profiles"][profile]["payload_source"] = payload_source
        if args.baseline:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            for profile in profiles:
                before = (baseline.get("profiles") or {}).get(profile, {}).get("median_lcp_tokens")
                after = report["profiles"][profile]["median_lcp_tokens"]
                report["profiles"][profile]["baseline_median_lcp_tokens"] = before
                report["profiles"][profile]["lcp_multiplier"] = (
                    after / before if isinstance(before, (int, float)) and before else None
                )
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        test_message_ids = _collect_test_message_ids(profiles)
        if manifest is not None:
            for profile in profiles:
                with contextlib.suppress(Exception):
                    _systemctl("stop", f"akid@{profile}.service")
            _restore(manifest, profiles)
            _mark_seen(profiles, test_message_ids)
        if debug_paths:
            _disable_debug(debug_paths, profiles)
        for profile in profiles:
            with contextlib.suppress(Exception):
                _systemctl("start", f"akid@{profile}.service")
        if manifest is not None:
            _stabilize_rollback(profiles, manifest)
            _check_rollback(profiles, manifest)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
