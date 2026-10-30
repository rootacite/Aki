#!/usr/bin/env python3
"""Migrate legacy Letta reminders out of conversation and reflection data."""

from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.letta_reminders import (  # noqa: E402
    apply_conversation_migration,
    apply_transcript_migration,
    build_conversation_migration,
    build_transcript_migration,
    create_backup,
)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "")).strip("_")
    return cleaned or "unknown"


def profile_identity(config_path: Path) -> tuple[str, str] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if not parser.read(config_path, encoding="utf-8"):
        return None
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    conversation = parser.get("gateway", "conversation", fallback="default").strip()
    if not agent_id:
        return None
    return agent_id, conversation or "default"


def conversation_dir(backend_dir: Path, agent_id: str, conversation: str) -> Path:
    return backend_dir / "conversations" / b64url(f"{conversation}:{agent_id}")


def transcript_dir(
    transcript_root: Path,
    agent_id: str,
    conversation: str,
) -> Path:
    return transcript_root / safe_segment(agent_id) / safe_segment(conversation)


def profile_names(requested: list[str], config_root: Path) -> list[str]:
    if requested:
        return requested
    discovered = sorted(path.stem for path in config_root.glob("*.conf"))
    return discovered or ["aki", "fuyu", "haru", "natsu"]


def migration_summary(
    profile: str,
    conversation,
    transcript,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "conversation": {
            "messages": str(conversation.messages_path),
            "changed": conversation.changed,
            "removed_rows": conversation.removed_rows,
            "changed_rows": conversation.changed_rows,
            "removed_message_ids": conversation.removed_message_ids,
        },
        "transcript": (
            {
                "path": str(transcript.transcript_path),
                "state": str(transcript.state_path),
                "changed": transcript.changed,
                "removed_rows": transcript.removed_rows,
                "changed_rows": transcript.changed_rows,
            }
            if transcript is not None
            else {"changed": False, "missing": True}
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--config-root",
        type=Path,
        default=Path.home() / ".aki",
    )
    parser.add_argument(
        "--backend-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "AKI_LETTA_BACKEND_DIR",
                Path.home() / ".letta" / "lc-local-backend",
            )
        ).expanduser(),
    )
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=Path(
            os.environ.get(
                "LETTA_TRANSCRIPT_ROOT",
                Path.home() / ".letta" / "transcripts",
            )
        ).expanduser(),
    )
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    plans: list[tuple[str, Any, Any]] = []
    errors: list[dict[str, str]] = []
    for profile in profile_names(args.profile, args.config_root):
        config_path = args.config_root / f"{profile}.conf"
        identity = profile_identity(config_path)
        if identity is None:
            errors.append(
                {"profile": profile, "error": f"missing gateway.agent_id in {config_path}"}
            )
            continue
        agent_id, conversation = identity
        messages_path = (
            conversation_dir(args.backend_dir, agent_id, conversation)
            / "messages.jsonl"
        )
        if not messages_path.is_file():
            errors.append(
                {"profile": profile, "error": f"messages.jsonl is missing: {messages_path}"}
            )
            continue
        conversation_plan = build_conversation_migration(messages_path)
        reflection_dir = transcript_dir(
            args.transcript_root, agent_id, conversation
        )
        transcript_path = reflection_dir / "transcript.jsonl"
        transcript_plan = (
            build_transcript_migration(
                transcript_path, reflection_dir / "state.json"
            )
            if transcript_path.is_file()
            else None
        )
        plans.append((profile, conversation_plan, transcript_plan))

    changed = [
        item
        for item in plans
        if item[1].changed or (item[2] is not None and item[2].changed)
    ]
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "changed_profiles": [profile for profile, _conversation, _transcript in changed],
        "profiles": [
            migration_summary(profile, conversation, transcript)
            for profile, conversation, transcript in plans
        ],
        "errors": errors,
    }

    backup: dict[str, Any] | None = None
    if args.apply and changed:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_root = args.backup_root or (
            Path.home()
            / ".letta"
            / "backups"
            / f"letta-reminder-migration-{stamp}"
        )
        files: list[tuple[Path, str]] = []
        for profile, conversation, transcript in changed:
            files.extend(
                [
                    (conversation.messages_path, f"{profile}/messages.jsonl"),
                    (conversation.conversation_path, f"{profile}/conversation.json"),
                ]
            )
            if transcript is not None:
                files.extend(
                    [
                        (transcript.transcript_path, f"{profile}/transcript.jsonl"),
                        (transcript.state_path, f"{profile}/state.json"),
                    ]
                )
        backup = create_backup(backup_root, files)
        for _profile, conversation, transcript in changed:
            apply_conversation_migration(conversation)
            if transcript is not None:
                apply_transcript_migration(transcript)

    summary["backup"] = backup
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
