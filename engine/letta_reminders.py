"""Remove the legacy Letta reminders that Aki now renders itself."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SUPPRESSED_REMINDER_MARKERS = (
    "automated message providing context about the user's environment",
    "automated message providing information about you",
    "MCP servers with available tools:",
    "Permission mode active:",
    "Permission mode changed to:",
)

_REMINDER_BLOCK_RE = re.compile(
    r"<system-reminder>[\s\S]*?</system-reminder>", re.IGNORECASE
)


def _tidy_removed_text(text: str) -> str:
    updated = re.sub(r"^[ \t]*(?:\r?\n)+", "", text)
    updated = re.sub(r"(?:\r?\n)+[ \t]*$", "", updated)
    return updated if updated.strip() else ""


def strip_suppressed_reminders(text: str) -> tuple[str, bool]:
    """Strip only the four suppressed reminder classes from a string."""
    source = str(text or "")
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        block = match.group(0)
        if any(marker in block for marker in SUPPRESSED_REMINDER_MARKERS):
            changed = True
            return ""
        return block

    updated = _REMINDER_BLOCK_RE.sub(replace, source)
    if not changed:
        return source, False
    return _tidy_removed_text(updated), True


def strip_suppressed_content(content: Any) -> tuple[Any, bool]:
    """Strip suppressed reminders from a Letta message content value.

    Returns ``(content, changed)``. ``None`` means the message was emptied and
    should be removed.
    """
    if isinstance(content, str):
        updated, changed = strip_suppressed_reminders(content)
        return (None if not updated else updated), changed
    if not isinstance(content, list):
        return content, False
    output: list[Any] = []
    changed = False
    for part in content:
        if (
            isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            updated, part_changed = strip_suppressed_reminders(str(part["text"]))
            if part_changed:
                changed = True
            if not updated:
                continue
            if part_changed:
                replacement = dict(part)
                replacement["text"] = updated
                output.append(replacement)
            else:
                output.append(part)
            continue
        output.append(part)
    if changed and not output:
        return None, True
    return output, changed


def _line_parts(raw_line: str) -> tuple[str, str]:
    body = raw_line.rstrip("\r\n")
    return body, raw_line[len(body) :]


def _json_line(value: Mapping[str, Any], ending: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + ending
    )


@dataclass
class ConversationMigration:
    messages_path: Path
    conversation_path: Path
    original_text: str
    output_text: str
    removed_message_ids: list[str] = field(default_factory=list)
    removed_rows: int = 0
    changed_rows: int = 0

    @property
    def changed(self) -> bool:
        return self.original_text != self.output_text


@dataclass
class TranscriptMigration:
    transcript_path: Path
    state_path: Path
    original_text: str
    output_text: str
    original_state: dict[str, Any] | None
    output_state: dict[str, Any] | None
    removed_rows: int = 0
    changed_rows: int = 0

    @property
    def changed(self) -> bool:
        return (
            self.original_text != self.output_text
            or (
                self.output_state is not None
                and self.original_state != self.output_state
            )
        )


def build_conversation_migration(messages_path: Path) -> ConversationMigration:
    original = messages_path.read_text(encoding="utf-8", errors="replace")
    output_lines: list[str] = []
    removed_ids: list[str] = []
    removed_parents: dict[str, str | None] = {}
    removed_rows = 0
    changed_rows = 0

    for raw_line in original.splitlines(keepends=True):
        body, ending = _line_parts(raw_line)
        try:
            row = json.loads(body)
        except ValueError:
            output_lines.append(raw_line)
            continue
        if not isinstance(row, dict):
            output_lines.append(raw_line)
            continue

        parent = row.get("parentId")
        replacement = parent
        while isinstance(replacement, str) and replacement in removed_parents:
            replacement = removed_parents[replacement]

        message = row.get("message")
        remove_row = False
        row_changed = False
        if (
            row.get("type") == "message"
            and isinstance(message, dict)
            and message.get("role") == "user"
        ):
            updated, changed = strip_suppressed_content(message.get("content"))
            if changed:
                row_changed = True
                if updated is None:
                    remove_row = True
                    removed_ids.append(str(message.get("id") or ""))
                else:
                    message["content"] = updated
        if remove_row:
            row_id = str(row.get("id") or "")
            if row_id:
                removed_parents[row_id] = (
                    replacement if isinstance(replacement, str) else None
                )
            removed_rows += 1
            continue
        if replacement != parent:
            row["parentId"] = replacement
            row_changed = True
        if row_changed:
            changed_rows += 1
            output_lines.append(_json_line(row, ending))
        else:
            output_lines.append(raw_line)

    return ConversationMigration(
        messages_path=messages_path,
        conversation_path=messages_path.parent / "conversation.json",
        original_text=original,
        output_text="".join(output_lines),
        removed_message_ids=[item for item in removed_ids if item],
        removed_rows=removed_rows,
        changed_rows=changed_rows,
    )


def _next_timestamp(current: object) -> str:
    now = datetime.now(timezone.utc)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(str(current or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = None
    candidate = now
    if parsed is not None and candidate <= parsed:
        candidate = parsed + timedelta(milliseconds=1)
    return candidate.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def updated_conversation_record(
    record: Mapping[str, Any],
    *,
    removed_message_ids: Iterable[str],
) -> dict[str, Any]:
    updated = dict(record)
    removed = set(removed_message_ids)
    ids = updated.get("in_context_message_ids")
    if isinstance(ids, list):
        updated["in_context_message_ids"] = [
            item for item in ids if str(item) not in removed
        ]
    stamp = _next_timestamp(updated.get("updated_at"))
    updated["updated_at"] = stamp
    if removed:
        updated["last_message_at"] = stamp
    return updated


def build_transcript_migration(
    transcript_path: Path,
    state_path: Path,
) -> TranscriptMigration:
    original = transcript_path.read_text(encoding="utf-8", errors="replace")
    output_lines: list[str] = []
    removed_rows = 0
    changed_rows = 0
    for raw_line in original.splitlines(keepends=True):
        body, ending = _line_parts(raw_line)
        try:
            row = json.loads(body)
        except ValueError:
            output_lines.append(raw_line)
            continue
        if not isinstance(row, dict):
            output_lines.append(raw_line)
            continue
        if row.get("kind") == "user" and isinstance(row.get("text"), str):
            updated, changed = strip_suppressed_reminders(str(row["text"]))
            if changed:
                if not updated:
                    removed_rows += 1
                    continue
                row["text"] = updated
                changed_rows += 1
                output_lines.append(_json_line(row, ending))
                continue
        output_lines.append(raw_line)

    original_state: dict[str, Any] | None = None
    output_state: dict[str, Any] | None = None
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            original_state = loaded
    except (OSError, ValueError):
        original_state = None

    output_text = "".join(output_lines)
    if original_state is not None and removed_rows:
        rows: list[dict[str, Any]] = []
        for line in output_text.splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        output_state = recompute_reflection_state(original_state, rows)

    return TranscriptMigration(
        transcript_path=transcript_path,
        state_path=state_path,
        original_text=original,
        output_text=output_text,
        original_state=original_state,
        output_state=output_state,
        removed_rows=removed_rows,
        changed_rows=changed_rows,
    )


def recompute_reflection_state(
    state: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recompute step counters while preserving the original reflection anchor."""
    updated = dict(state)
    total = sum(1 for row in rows if row.get("kind") == "assistant")
    anchor = str(updated.get("reflected_through_message_id") or "")
    reflected = 0
    if anchor:
        for row in rows:
            if row.get("kind") == "assistant":
                reflected += 1
            if anchor in {
                str(row.get("source_message_id") or ""),
                str(row.get("source_line_id") or ""),
            }:
                break
        else:
            reflected = 0
    reflected = min(reflected, total)
    updated["total_completed_steps"] = total
    updated["reflected_completed_steps"] = reflected
    updated["steps_since_last_successful_reflection"] = max(0, total - reflected)
    return updated


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create_backup(
    backup_root: Path,
    files: Sequence[tuple[Path, str]],
) -> dict[str, Any]:
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    manifest: list[dict[str, Any]] = []
    checksum_lines: list[str] = []
    for source, label in files:
        if not source.is_file():
            continue
        target = backup_root / label
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        source_hash = _sha256(source)
        target_hash = _sha256(target)
        if source_hash != target_hash:
            raise RuntimeError(f"backup verification failed for {source}")
        checksum_lines.append(f"{target_hash}  {label}")
        manifest.append(
            {
                "source": str(source),
                "backup": str(target),
                "bytes": target.stat().st_size,
                "sha256": target_hash,
            }
        )
    (backup_root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + ("\n" if checksum_lines else ""),
        encoding="utf-8",
    )
    (backup_root / "manifest.json").write_text(
        json.dumps({"files": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"path": str(backup_root), "files": manifest}


def apply_conversation_migration(plan: ConversationMigration) -> dict[str, Any]:
    if not plan.changed:
        return {"written": False}
    _atomic_write_text(plan.messages_path, plan.output_text)
    record = json.loads(plan.conversation_path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError(f"{plan.conversation_path} is not an object")
    updated = updated_conversation_record(
        record,
        removed_message_ids=plan.removed_message_ids,
    )
    _atomic_write_text(
        plan.conversation_path,
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "written": True,
        "updated_at": updated.get("updated_at"),
        "in_context_message_ids": updated.get("in_context_message_ids"),
    }


def apply_transcript_migration(plan: TranscriptMigration) -> dict[str, Any]:
    if not plan.changed:
        return {"written": False}
    if plan.original_text != plan.output_text:
        _atomic_write_text(plan.transcript_path, plan.output_text)
    if plan.output_state is not None and plan.output_state != plan.original_state:
        _atomic_write_text(
            plan.state_path,
            json.dumps(plan.output_state, ensure_ascii=False, indent=2) + "\n",
        )
    return {"written": True}
