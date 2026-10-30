#!/usr/bin/env python3
"""Add local message times to historical akid user-turn prompts."""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROMPT_HEADER = "[akid MQTT channel message]"
TIME_RE = re.compile(r"(?m)^message-time: .+$")
SEQ_RE = re.compile(r"(?m)^seq: (\d+)$")
BODY_MARKER = "\nmessage:\n"


@dataclass(frozen=True)
class PromptInfo:
    seq: int | None
    body: str


@dataclass(frozen=True)
class Match:
    sent_at: int
    source: str


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def local_time(epoch_ms: int) -> str:
    try:
        seconds = epoch_ms / 1000 if epoch_ms > 0 else 0
        if not seconds:
            raise ValueError("missing timestamp")
        value = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone()
    except (OSError, OverflowError, ValueError):
        raise RuntimeError(f"invalid timestamp: {epoch_ms}") from None
    return value.strftime("%Y-%m-%d %H:%M")


def parse_prompt(text: str) -> PromptInfo | None:
    if not text.startswith(PROMPT_HEADER + "\n"):
        return None
    seq_match = SEQ_RE.search(text)
    body = ""
    marker = text.find(BODY_MARKER)
    if marker >= 0:
        body = text[marker + len(BODY_MARKER) :]
        body = body.split("\n\nYou are listening", 1)[0]
        body = re.split(
            r"\n(?:attachment-names:|multimodal-note:)", body, maxsplit=1
        )[0]
        body = body.strip()
    return PromptInfo(
        seq=int(seq_match.group(1)) if seq_match else None,
        body=body,
    )


def add_time_line(text: str, timestamp: str) -> str:
    if TIME_RE.search(text):
        return text
    first, separator, rest = text.partition("\n")
    if first != PROMPT_HEADER or not separator:
        return text
    return f"{first}\nmessage-time: {timestamp}\n{rest}"


def profile_agent_id(config_path: Path) -> tuple[str, str]:
    parser = configparser.ConfigParser()
    parser.read(config_path)
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    conversation = parser.get("gateway", "conversation", fallback="default").strip()
    if not agent_id:
        raise RuntimeError(f"missing gateway.agent_id in {config_path}")
    return agent_id, conversation or "default"


def conversation_messages_path(agent_id: str, conversation: str) -> Path:
    backend = Path(
        os.environ.get(
            "AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend"
        )
    ).expanduser()
    return (
        backend
        / "conversations"
        / b64url(f"{conversation}:{agent_id}")
        / "messages.jsonl"
    )


def load_channel_entries(channel_root: Path, profile: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not channel_root.is_dir():
        return entries
    for path in channel_root.rglob("*.jsonl"):
        if path.name != f"{profile}.jsonl" and path.name != f"{profile}.solo.jsonl":
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict) and not item.get("from_agent"):
                entries.append(item)
    return entries


def row_timestamp(row: dict[str, Any]) -> int:
    raw = str(row.get("timestamp") or "")
    if not raw:
        return 0
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    except ValueError:
        return 0


def find_match(
    info: PromptInfo,
    entries: list[dict[str, Any]],
    row_ms: int,
) -> Match | None:
    candidates = entries
    if info.seq is not None:
        candidates = [
            item for item in candidates if int(item.get("seq") or 0) == info.seq
        ]
    if info.body:
        exact = [
            item
            for item in candidates
            if str(item.get("text") or "").strip() == info.body
        ]
        if not exact:
            return None
        candidates = exact
    if len(candidates) > 1 and row_ms > 0:
        candidates.sort(
            key=lambda item: abs(int(item.get("sent_at") or 0) - row_ms)
        )
        candidates = candidates[:1]
    if len(candidates) != 1:
        return None
    sent_at = int(candidates[0].get("sent_at") or 0)
    if sent_at <= 0:
        return None
    return Match(sent_at=sent_at, source="channel-history")


def atomic_write(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def migrate_profile(
    *,
    profile: str,
    messages_path: Path,
    entries: list[dict[str, Any]],
    backup_root: Path | None,
    apply: bool,
) -> dict[str, Any]:
    if not messages_path.is_file():
        return {"profile": profile, "path": str(messages_path), "missing": True}
    raw_lines = messages_path.read_text(encoding="utf-8").splitlines()
    output_lines: list[str] = []
    changes: list[dict[str, Any]] = []
    stats = {"total": 0, "already": 0, "no_prompt": 0}
    for line in raw_lines:
        if not line.strip():
            output_lines.append(line)
            continue
        try:
            row = json.loads(line)
        except ValueError:
            output_lines.append(line)
            continue
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            output_lines.append(line)
            continue
        text_parts = [
            item
            for item in message.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if len(text_parts) != 1 or not isinstance(text_parts[0].get("text"), str):
            output_lines.append(line)
            continue
        text = text_parts[0]["text"]
        info = parse_prompt(text)
        if info is None:
            output_lines.append(line)
            continue
        stats["total"] += 1
        if TIME_RE.search(text):
            stats["already"] += 1
            output_lines.append(line)
            continue
        row_ms = row_timestamp(row)
        match = find_match(info, entries, row_ms)
        if match is None and row_ms > 0:
            match = Match(sent_at=row_ms, source="turn-timestamp")
        if match is None:
            stats["no_prompt"] += 1
            output_lines.append(line)
            continue
        stamp = local_time(match.sent_at)
        updated = add_time_line(text, stamp)
        if updated == text:
            output_lines.append(line)
            continue
        text_parts[0]["text"] = updated
        output_lines.append(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        )
        changes.append(
            {
                "seq": info.seq,
                "time": stamp,
                "source": match.source,
                "preview": info.body[:100],
            }
        )
    stats["changed"] = len(changes)
    stats["channel-history"] = sum(
        1 for item in changes if item["source"] == "channel-history"
    )
    stats["turn-timestamp"] = sum(
        1 for item in changes if item["source"] == "turn-timestamp"
    )
    if apply and changes:
        if backup_root is None:
            raise RuntimeError("backup root is required when applying")
        destination = backup_root / profile
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(messages_path, destination / "messages.jsonl")
        for name in ("conversation.json", "system-prompt.json"):
            source = messages_path.parent / name
            if source.is_file():
                shutil.copy2(source, destination / name)
        atomic_write(messages_path, "\n".join(output_lines) + "\n")
    return {
        "profile": profile,
        "path": str(messages_path),
        "stats": stats,
        "changes": changes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--channel-root", type=Path, default=Path.home() / ".aki" / "channel-history"
    )
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    profiles = args.profile or ["aki", "fuyu", "haru", "natsu"]
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    backup_root = args.backup_root or (
        Path.home() / ".letta" / "backups" / f"channel-turn-time-migration-{stamp}"
    )
    if args.apply:
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_root, 0o700)
    reports: list[dict[str, Any]] = []
    for profile in profiles:
        config_path = Path.home() / ".aki" / f"{profile}.conf"
        try:
            agent_id, conversation = profile_agent_id(config_path)
            messages_path = conversation_messages_path(agent_id, conversation)
            entries = load_channel_entries(args.channel_root, profile)
            report = migrate_profile(
                profile=profile,
                messages_path=messages_path,
                entries=entries,
                backup_root=backup_root if args.apply else None,
                apply=args.apply,
            )
        except RuntimeError as exc:
            report = {"profile": profile, "error": str(exc)}
        reports.append(report)
    summary = {"apply": args.apply, "backup_root": str(backup_root), "profiles": reports}
    if args.apply:
        (backup_root / "manifest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
