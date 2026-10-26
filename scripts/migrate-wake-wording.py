#!/usr/bin/env python3
"""Migrate historical @username wake wording to the softer per-turn wording."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.live_context import apply_channel_wake_routing, render_channel_wake_routing

LEGACY = re.compile(
    r"@(?P<name>[^\s，。]+)仅在你只对(?P=name)说话，无需打扰其他人时使用"
    r"（该标记会强制只唤醒(?P=name)）。"
)


def replacement(match: re.Match[str]) -> str:
    name = match.group("name")
    return f"如果你认为只需要回复{name}、无需打扰其他人，使用 @{name}。"


def replace_strings(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        updated, count = LEGACY.subn(replacement, value)
        return updated, count
    if isinstance(value, list):
        total = 0
        output = []
        for item in value:
            updated, count = replace_strings(item)
            output.append(updated)
            total += count
        return output, total
    if isinstance(value, dict):
        total = 0
        output = {}
        for key, item in value.items():
            updated, count = replace_strings(item)
            output[key] = updated
            total += count
        return output, total
    return value, 0


def profile_usernames() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted((Path.home() / ".aki").glob("*.conf")):
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
            agent_id = parser.get("gateway", "agent_id", fallback="").strip()
            username = parser.get("gateway", "username", fallback="").strip()
        except (configparser.Error, OSError):
            continue
        if agent_id:
            result[agent_id] = username or "Master"
    return result


def update_system_prompt(text: str, username: str) -> tuple[str, int]:
    updated, count = replace_strings(text)
    block = render_channel_wake_routing(username)
    updated = apply_channel_wake_routing(updated, block)
    return updated, count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--backend", type=Path, default=Path.home() / ".letta" / "lc-local-backend")
    parser.add_argument("--journal", type=Path, default=Path.home() / ".aki" / "channel-prompt-journal")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    usernames = profile_usernames()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = args.backup_dir or (Path.home() / ".letta" / "backups" / f"wake-wording-migration-{stamp}")
    changed: list[tuple[Path, int]] = []

    def backup(path: Path) -> None:
        if not args.apply:
            return
        try:
            relative = path.relative_to(Path.home())
        except ValueError:
            relative = Path(path.name)
        target = backup_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, target)

    def write(path: Path, text: str) -> None:
        if not args.apply:
            return
        backup(path)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(text, encoding="utf-8")
        os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)

    for path in sorted((args.backend / "agents").glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        system = record.get("system")
        if not isinstance(system, str) or not system:
            continue
        agent_id = str(record.get("id") or "")
        username = usernames.get(agent_id, "Master")
        updated, count = update_system_prompt(system, username)
        if updated != system:
            record["system"] = updated
            changed.append((path, count))
            write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    for path in sorted((args.backend / "conversations").glob("*/system-prompt.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        content = record.get("content")
        if not isinstance(content, str) or not content:
            continue
        conversation = path.parent / "conversation.json"
        agent_id = ""
        try:
            record_meta = json.loads(conversation.read_text(encoding="utf-8"))
            if isinstance(record_meta, dict):
                agent_id = str(record_meta.get("agent_id") or "")
        except (OSError, ValueError):
            pass
        username = usernames.get(agent_id, "Master")
        updated, count = update_system_prompt(content, username)
        if updated != content:
            record["content"] = updated
            changed.append((path, count))
            write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    for path in sorted((args.backend / "conversations").glob("*/messages.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        output: list[str] = []
        count = 0
        for line in lines:
            if not line.strip():
                output.append(line)
                continue
            try:
                record = json.loads(line)
            except ValueError:
                output.append(line)
                continue
            updated, replaced = replace_strings(record)
            count += replaced
            output.append(json.dumps(updated, ensure_ascii=False, separators=(",", ":")))
        if count:
            changed.append((path, count))
            write(path, "\n".join(output) + "\n")

    if args.journal.is_dir():
        for path in sorted(args.journal.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            output: list[str] = []
            count = 0
            for line in lines:
                if not line.strip():
                    output.append(line)
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    output.append(line)
                    continue
                updated, replaced = replace_strings(record)
                count += replaced
                output.append(json.dumps(updated, ensure_ascii=False, separators=(",", ":")))
            if count:
                changed.append((path, count))
                write(path, "\n".join(output) + "\n")

    for path, count in changed:
        print(f"{path}: {count} replacement(s)")
    print(f"files={len(changed)} apply={args.apply} backup={backup_root if args.apply else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
