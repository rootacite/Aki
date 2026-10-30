#!/usr/bin/env python3
"""Strip historical channel rosters and annotate the wake-reminder speaker."""

from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.channel_prompt import (  # noqa: E402
    CHANNEL_PROMPT_HEADER,
    bump_conversation_updated_at,
    existing_speaker,
    identity_lookup_from_registry,
    migrate_channel_prompt,
    parse_channel_prompt,
    roster_kind,
    match_history_kind,
)


@dataclass
class ProfilePlan:
    profile: str
    messages_path: Path
    original_text: str
    output_text: str
    stats: dict[str, Any] = field(default_factory=dict)
    changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original_text != self.output_text


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def profile_settings(config_path: Path) -> tuple[str, str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(config_path, encoding="utf-8")
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    conversation = parser.get("gateway", "conversation", fallback="default").strip()
    username = parser.get("gateway", "username", fallback="Master").strip()
    if not agent_id:
        raise RuntimeError(f"missing gateway.agent_id in {config_path}")
    return agent_id, conversation or "default", username or "Master"


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
    for path in sorted(channel_root.rglob("*.jsonl")):
        if path.name not in (f"{profile}.jsonl", f"{profile}.solo.jsonl"):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                entries.append(item)
    return entries


def row_timestamp(row: Mapping[str, Any]) -> int:
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


def atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _kind_and_source(
    text: str,
    *,
    sender_name: str,
    sender_id: str,
    entries: Sequence[Mapping[str, Any]],
    row_ms: int,
    identity_lookup,
) -> tuple[str, str]:
    kind = roster_kind(text, sender_id, sender_name)
    if kind:
        return kind, "roster"
    info = parse_channel_prompt(text)
    kind = match_history_kind(
        entries,
        seq=info.seq if info else None,
        body=info.body if info else "",
        row_ms=row_ms,
        sender_id=sender_id,
        sender_name=sender_name,
    )
    if kind:
        return kind, "channel-history"
    kind = identity_lookup(sender_id, sender_name) if identity_lookup else ""
    if kind:
        return kind, "identity-registry"
    return "", "fallback"


def build_profile_plan(
    *,
    profile: str,
    messages_path: Path,
    entries: Sequence[Mapping[str, Any]],
    identity_lookup,
    username: str = "Master",
) -> ProfilePlan:
    if not messages_path.is_file():
        raise RuntimeError(f"messages.jsonl is missing: {messages_path}")
    original_text = messages_path.read_text(encoding="utf-8")
    output_lines: list[str] = []
    changes: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "channel_prompts": 0,
        "rows_changed": 0,
        "changed": 0,
        "already": 0,
        "unmatched": 0,
        "roster_removed": 0,
        "speaker_added": 0,
        "speaker_updated": 0,
        "kind_sources": {},
    }
    for raw_line in original_text.splitlines(keepends=True):
        body = raw_line.rstrip("\r\n")
        ending = raw_line[len(body) :]
        try:
            row = json.loads(body)
        except ValueError:
            output_lines.append(raw_line)
            continue
        message = row.get("message") if isinstance(row, dict) else None
        if not isinstance(message, dict) or message.get("role") != "user":
            output_lines.append(raw_line)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            output_lines.append(raw_line)
            continue
        row_ms = row_timestamp(row)
        row_changed = False
        for part_index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str) or not text.startswith(
                CHANNEL_PROMPT_HEADER
            ):
                continue
            info = parse_channel_prompt(text)
            if info is None:
                continue
            stats["channel_prompts"] += 1
            if not info.sender_name:
                stats["unmatched"] += 1
                changes.append(
                    {
                        "message_id": str(message.get("id") or ""),
                        "part_index": part_index,
                        "status": "unmatched",
                        "reason": "sender line missing",
                    }
                )
                continue
            kind, source = _kind_and_source(
                text,
                sender_name=info.sender_name,
                sender_id=info.sender_id,
                entries=entries,
                row_ms=row_ms,
                identity_lookup=identity_lookup,
            )
            existing_name, existing_label = existing_speaker(text)
            if not kind and existing_label == "智能体":
                kind, source = "agent", "existing-speaker"
            elif not kind and existing_label:
                source = "existing-speaker"
            stats["kind_sources"][source] = (
                int(stats["kind_sources"].get(source, 0)) + 1
            )
            speaker_name = info.sender_name if kind == "agent" else username
            updated, roster_changed, speaker_changed = migrate_channel_prompt(
                text,
                speaker_name=speaker_name,
                speaker_kind=kind,
                username=username,
            )
            if updated == text:
                stats["already"] += 1
                continue
            content[part_index]["text"] = updated
            row_changed = True
            stats["changed"] += 1
            stats["roster_removed"] += int(roster_changed)
            stats["speaker_added"] += int(
                speaker_changed and "wake-reminder:" not in text
            )
            stats["speaker_updated"] += int(
                speaker_changed and "wake-reminder:" in text
            )
            changes.append(
                {
                    "message_id": str(message.get("id") or ""),
                    "part_index": part_index,
                    "status": "changed",
                    "seq": info.seq,
                    "sender": info.sender_name,
                    "sender_id": info.sender_id,
                    "kind": kind,
                    "kind_source": source,
                    "existing_speaker": existing_name,
                    "roster_removed": roster_changed,
                    "speaker_changed": speaker_changed,
                    "preview": info.body[:100],
                }
            )
        if row_changed:
            stats["rows_changed"] += 1
            output_lines.append(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + ending
            )
        else:
            output_lines.append(raw_line)
    return ProfilePlan(
        profile=profile,
        messages_path=messages_path,
        original_text=original_text,
        output_text="".join(output_lines),
        stats=stats,
        changes=changes,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_profile(plan: ProfilePlan, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    files: dict[str, Any] = {}
    source_dir = plan.messages_path.parent
    for name in (
        "messages.jsonl",
        "conversation.json",
        "system-prompt.json",
        "manifest.json",
    ):
        source = source_dir / name
        if not source.is_file():
            files[name] = {"present": False}
            continue
        target = destination / name
        shutil.copy2(source, target)
        source_hash = _sha256(source)
        target_hash = _sha256(target)
        if source_hash != target_hash:
            raise RuntimeError(f"backup verification failed for {source}")
        files[name] = {
            "present": True,
            "bytes": target.stat().st_size,
            "sha256": target_hash,
        }
    return {"profile": plan.profile, "path": str(destination), "files": files}


def apply_profile_plan(plan: ProfilePlan, backup: Mapping[str, Any]) -> dict[str, Any]:
    if not plan.changed:
        return {"profile": plan.profile, "written": False, "backup": backup}
    atomic_write(plan.messages_path, plan.output_text)
    conversation = plan.messages_path.parent / "conversation.json"
    previous, updated = bump_conversation_updated_at(conversation)
    return {
        "profile": plan.profile,
        "written": True,
        "updated_at_before": previous,
        "updated_at_after": updated,
        "backup": backup,
    }


def _service_name(profile: str) -> str:
    return f"akid@{profile}.service"


def service_is_active(profile: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", _service_name(profile)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def stop_service(profile: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", _service_name(profile)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def start_service(profile: str) -> None:
    subprocess.run(
        ["systemctl", "--user", "start", _service_name(profile)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--no-service-control",
        action="store_true",
        help="do not stop or restart akid@<profile>.service",
    )
    parser.add_argument(
        "--channel-root",
        type=Path,
        default=Path.home() / ".aki" / "channel-history",
    )
    parser.add_argument(
        "--identity-registry",
        type=Path,
        default=Path.home() / ".aki" / "identities.json",
    )
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    profiles = args.profile or ["aki", "fuyu", "haru", "natsu"]
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    backup_root = args.backup_root or (
        Path.home()
        / ".letta"
        / "backups"
        / f"channel-prompt-migration-{stamp}"
    )
    identity_lookup = identity_lookup_from_registry(args.identity_registry)
    plans: list[ProfilePlan] = []
    errors: list[dict[str, str]] = []
    for profile in profiles:
        config_path = Path.home() / ".aki" / f"{profile}.conf"
        try:
            agent_id, conversation, username = profile_settings(config_path)
            messages_path = conversation_messages_path(agent_id, conversation)
            entries = load_channel_entries(args.channel_root, profile)
            plan = build_profile_plan(
                profile=profile,
                messages_path=messages_path,
                entries=entries,
                identity_lookup=identity_lookup,
                username=username,
            )
            plans.append(plan)
        except Exception as exc:  # noqa: BLE001 - report and continue other profiles
            errors.append({"profile": profile, "error": str(exc)})

    active_services: list[str] = []
    backups: dict[str, Any] = {}
    writes: list[dict[str, Any]] = []
    if args.apply:
        if not args.no_service_control:
            for plan in plans:
                if service_is_active(plan.profile):
                    active_services.append(plan.profile)
            try:
                for profile in active_services:
                    stop_service(profile)
            except subprocess.CalledProcessError as exc:
                for profile in active_services:
                    if service_is_active(profile):
                        continue
                    try:
                        start_service(profile)
                    except subprocess.CalledProcessError:
                        pass
                raise SystemExit(f"cannot stop gateway: {exc}")
        try:
            backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(backup_root, 0o700)
            for plan in plans:
                backups[plan.profile] = backup_profile(
                    plan, backup_root / plan.profile
                )
            manifest = {
                "apply": True,
                "backup_root": str(backup_root),
                "profiles": [
                    {
                        "profile": plan.profile,
                        "messages_path": str(plan.messages_path),
                        "stats": plan.stats,
                        "changes": plan.changes,
                    }
                    for plan in plans
                ],
                "errors": errors,
                "backups": backups,
            }
            atomic_write(
                backup_root / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            for plan in plans:
                try:
                    writes.append(apply_profile_plan(plan, backups[plan.profile]))
                except Exception as exc:  # noqa: BLE001 - keep other profiles intact
                    errors.append({"profile": plan.profile, "error": str(exc)})
        finally:
            if not args.no_service_control:
                for profile in active_services:
                    try:
                        start_service(profile)
                    except subprocess.CalledProcessError as exc:
                        errors.append(
                            {"profile": profile, "error": f"restart failed: {exc}"}
                        )

    summary = {
        "apply": args.apply,
        "backup_root": str(backup_root) if args.apply else None,
        "services_stopped": active_services,
        "profiles": [
            {
                "profile": plan.profile,
                "messages_path": str(plan.messages_path),
                "stats": plan.stats,
                "changes": plan.changes,
            }
            for plan in plans
        ],
        "writes": writes,
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
