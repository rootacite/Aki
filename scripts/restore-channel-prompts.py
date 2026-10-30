#!/usr/bin/env python3
"""Restore original channel prompts recorded in the akid rollback journal."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.channel_prompt import (  # noqa: E402
    read_journal,
    restore_persisted_channel_prompt,
)


def journal_path(profile: str) -> Path:
    return Path.home() / ".aki" / "channel-prompt-journal" / f"{profile}.jsonl"


def service_name(profile: str) -> str:
    return f"akid@{profile}.service"


def service_is_active(profile: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", service_name(profile)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def service_action(action: str, profile: str) -> None:
    subprocess.run(
        ["systemctl", "--user", action, service_name(profile)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def select_records(
    records: list[dict[str, Any]],
    *,
    turn_ids: set[str],
    message_ids: set[str],
    restore_all: bool,
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "strip":
            continue
        turn_id = str(record.get("turn_id") or "")
        message_id = str(record.get("message_id") or "")
        conversation_dir = str(record.get("conversation_dir") or "")
        if not message_id or not conversation_dir:
            continue
        if not restore_all:
            if turn_ids and turn_id not in turn_ids:
                continue
            if message_ids and message_id not in message_ids:
                continue
            if not turn_ids and not message_ids:
                continue
        key = (conversation_dir, message_id)
        previous = selected.get(key)
        if previous is None or str(record.get("stripped_at") or "") >= str(
            previous.get("stripped_at") or ""
        ):
            selected[key] = record
    return sorted(
        selected.values(), key=lambda item: str(item.get("stripped_at") or "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--turn-id", action="append", default=[])
    parser.add_argument("--message-id", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-service-control", action="store_true")
    args = parser.parse_args()

    if not args.all and not args.turn_id and not args.message_id:
        parser.error("choose --turn-id, --message-id, or --all")
    profiles = args.profile or ["aki", "fuyu", "haru", "natsu"]
    journals = (
        [args.journal]
        if args.journal is not None
        else [journal_path(profile) for profile in profiles]
    )
    records: list[dict[str, Any]] = []
    for path in journals:
        records.extend(read_journal(path))
    selected = select_records(
        records,
        turn_ids=set(args.turn_id),
        message_ids=set(args.message_id),
        restore_all=args.all,
    )
    selected_profiles = {
        str(record.get("profile") or "") for record in selected
    } - {""}
    active_services: list[str] = []
    if args.apply and not args.no_service_control:
        for profile in sorted(selected_profiles):
            if service_is_active(profile):
                active_services.append(profile)
        for profile in active_services:
            service_action("stop", profile)

    results: list[dict[str, Any]] = []
    try:
        for record in selected:
            result = restore_persisted_channel_prompt(
                conversation_dir=Path(str(record["conversation_dir"])),
                message_id=str(record["message_id"]),
                original_text=str(record.get("original_text") or ""),
                stripped_text=str(record.get("stripped_text") or ""),
                apply=args.apply,
            )
            results.append(
                {
                    "profile": record.get("profile"),
                    "turn_id": record.get("turn_id"),
                    "message_id": record.get("message_id"),
                    "status": result.status,
                    "updated_at": result.updated_at,
                    "error": result.error,
                }
            )
    finally:
        if args.apply and not args.no_service_control:
            for profile in active_services:
                service_action("start", profile)
    print(
        json.dumps(
            {
                "apply": args.apply,
                "selected": len(selected),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if any(item["status"] == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
