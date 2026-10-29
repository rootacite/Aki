"""Journal, strip, and restore persisted channel prompts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    canonical_name,
)
from .access import pkg
from .parse import (
    ConversationBoundary,
    StripResult,
    add_speaker_to_wake_reminder,
    has_channel_context,
    strip_channel_context,
)



def migrate_channel_prompt(
    text: str,
    *,
    speaker_name: str,
    speaker_kind: str,
    username: str = "",
) -> tuple[str, bool, bool]:
    """Strip a roster and add speaker info.  Returns text, roster, speaker."""
    stripped, roster_changed = strip_channel_context(text)
    updated, speaker_changed = add_speaker_to_wake_reminder(
        stripped,
        speaker_name=speaker_name,
        speaker_kind=speaker_kind,
        username=username,
    )
    return updated, roster_changed, speaker_changed



def capture_conversation_boundary(
    messages_path: Path, *, prefix_length: int = 4096
) -> ConversationBoundary:
    try:
        size = messages_path.stat().st_size
    except OSError:
        return ConversationBoundary(size=0)
    length = min(max(0, int(prefix_length)), size)
    digest = ""
    if length:
        try:
            with messages_path.open("rb") as handle:
                handle.seek(size - length)
                digest = hashlib.sha256(handle.read(length)).hexdigest()
        except OSError:
            return ConversationBoundary(size=size)
    return ConversationBoundary(
        size=size, prefix_sha256=digest, prefix_length=length
    )



def _boundary_error(data: bytes, boundary: ConversationBoundary) -> str:
    if len(data) < boundary.size:
        return "conversation transcript shrank after the turn boundary"
    if boundary.size > 0 and data[boundary.size - 1 : boundary.size] != b"\n":
        return "conversation turn boundary is not at a line ending"
    if boundary.prefix_length:
        start = boundary.size - boundary.prefix_length
        actual = hashlib.sha256(data[start : boundary.size]).hexdigest()
        if actual != boundary.prefix_sha256:
            return "conversation transcript prefix changed after the turn boundary"
    return ""



def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)



def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass



def append_journal(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)



def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records



def _next_updated_at(current: str) -> str:
    now = datetime.now(timezone.utc)
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(str(current or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = None
    candidate = now.astimezone(timezone.utc)
    if parsed is not None and candidate <= parsed:
        candidate = parsed.astimezone(timezone.utc) + timedelta(milliseconds=1)
    return candidate.isoformat(timespec="milliseconds").replace("+00:00", "Z")



def _conversation_record(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("conversation.json is not an object")
    return loaded



def bump_conversation_updated_at(
    conversation_path: Path, *, expected_previous: str | None = None
) -> tuple[str, str]:
    """Rewrite only ``updated_at`` so Letta evicts its resident messages."""
    record = _conversation_record(conversation_path)
    previous = str(record.get("updated_at") or "")
    if expected_previous is not None and previous != expected_previous:
        raise ValueError("conversation.json changed while the prompt was cleaned")
    updated = _next_updated_at(previous)
    record["updated_at"] = updated
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    mode = conversation_path.stat().st_mode & 0o777
    _atomic_write_bytes(conversation_path, text.encode("utf-8"), mode=mode)
    return previous, updated



def capture_parts(
    data: bytes, boundary: ConversationBoundary, prompt: str
) -> tuple[list[bytes], list[tuple[int, int, dict[str, Any]]]] | None:
    """Return suffix lines and matching (line, part, row) candidates."""
    suffix = data[boundary.size :]
    lines = suffix.splitlines(keepends=True)
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for line_index, raw_line in enumerate(lines):
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and part.get("text") == prompt
            ):
                matches.append((line_index, part_index, row))
    return lines, matches



def _replace_prompt_in_line(
    raw_line: bytes, row: dict[str, Any], part_index: int, updated_text: str
) -> bytes:
    decoded = raw_line.decode("utf-8")
    ending = ""
    body = decoded
    if body.endswith("\r\n"):
        body, ending = body[:-2], "\r\n"
    elif body.endswith("\n"):
        body, ending = body[:-1], "\n"
    content = row["message"]["content"]
    old_text = str(content[part_index]["text"])
    old_literal = json.dumps(old_text, ensure_ascii=False)
    new_literal = json.dumps(updated_text, ensure_ascii=False)
    if body.count(old_literal) == 1:
        return (body.replace(old_literal, new_literal, 1) + ending).encode("utf-8")
    content[part_index]["text"] = updated_text
    return (
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + ending
    ).encode("utf-8")



def strip_persisted_channel_prompt(
    *,
    conversation_dir: Path,
    boundary: ConversationBoundary,
    expected_prompt: str,
    turn_id: str,
    profile: str,
    journal_path: Path,
    speaker_name: str = "",
    speaker_kind: str = "",
) -> StripResult:
    """Strip one persisted user prompt and force Letta to reload it."""
    if not has_channel_context(expected_prompt):
        return StripResult(status="no_change")
    messages_path = conversation_dir / "messages.jsonl"
    conversation_path = conversation_dir / "conversation.json"
    try:
        data = messages_path.read_bytes()
    except OSError as exc:
        return StripResult(status="error", error=f"cannot read transcript: {exc}")
    boundary_error = _boundary_error(data, boundary)
    if boundary_error:
        return StripResult(status="boundary_invalid", error=boundary_error)
    try:
        conversation = _conversation_record(conversation_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return StripResult(status="error", error=f"cannot read conversation: {exc}")

    captured = capture_parts(data, boundary, expected_prompt)
    if captured is None:
        return StripResult(status="error", error="cannot parse transcript suffix")
    lines, matches = captured
    if not matches:
        return StripResult(status="no_target")
    if len(matches) > 1:
        return StripResult(
            status="ambiguous",
            error=f"matched {len(matches)} user prompt records",
        )
    line_index, part_index, row = matches[0]
    stripped, changed = strip_channel_context(expected_prompt)
    if not changed:
        return StripResult(status="no_change")
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    message_id = str(message.get("id") or "")
    entry_id = str(row.get("id") or "")
    timestamp = str(row.get("timestamp") or "")
    record = {
        "schema": "aki.channel-prompt-journal.v1",
        "kind": "strip",
        "profile": profile,
        "turn_id": turn_id,
        "message_id": message_id,
        "entry_id": entry_id,
        "timestamp": timestamp,
        "conversation_dir": str(conversation_dir),
        "part_index": part_index,
        "speaker_name": speaker_name,
        "speaker_kind": speaker_kind,
        "original_text": expected_prompt,
        "stripped_text": stripped,
        "conversation_updated_at_before": str(conversation.get("updated_at") or ""),
        "stripped_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        pkg().append_journal(journal_path, record)
    except OSError as exc:
        return StripResult(status="error", error=f"cannot write rollback journal: {exc}")

    replacement = _replace_prompt_in_line(
        lines[line_index], row, part_index, stripped
    )
    updated_lines = list(lines)
    updated_lines[line_index] = replacement
    new_data = data[: boundary.size] + b"".join(updated_lines)
    mode = messages_path.stat().st_mode & 0o777
    try:
        _atomic_write_bytes(messages_path, new_data, mode=mode)
    except OSError as exc:
        return StripResult(status="error", error=f"cannot rewrite transcript: {exc}")

    try:
        _, updated_at = pkg().bump_conversation_updated_at(
            conversation_path,
            expected_previous=record["conversation_updated_at_before"],
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        try:
            _atomic_write_bytes(messages_path, data, mode=mode)
        except OSError:
            pass
        return StripResult(
            status="error", error=f"cannot invalidate conversation cache: {exc}"
        )
    return StripResult(
        status="applied",
        message_id=message_id,
        entry_id=entry_id,
        updated_at=updated_at,
    )



def _find_message_row(
    data: bytes, message_id: str
) -> tuple[list[bytes], int, dict[str, Any], int] | None:
    lines = data.splitlines(keepends=True)
    matches: list[tuple[int, dict[str, Any]]] = []
    for index, raw_line in enumerate(lines):
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        message = row.get("message")
        if isinstance(message, dict) and str(message.get("id") or "") == message_id:
            matches.append((index, row))
    if len(matches) != 1:
        return None
    line_index, row = matches[0]
    content = row["message"].get("content")
    if not isinstance(content, list):
        return None
    return lines, line_index, row, len(content)



def restore_persisted_channel_prompt(
    *,
    conversation_dir: Path,
    message_id: str,
    original_text: str,
    stripped_text: str,
    apply: bool = False,
) -> StripResult:
    """Restore one journaled prompt and invalidate Letta's cache."""
    messages_path = conversation_dir / "messages.jsonl"
    conversation_path = conversation_dir / "conversation.json"
    try:
        data = messages_path.read_bytes()
        conversation = _conversation_record(conversation_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return StripResult(status="error", error=str(exc))
    found = _find_message_row(data, message_id)
    if found is None:
        return StripResult(status="no_target")
    lines, line_index, row, _count = found
    content = row["message"]["content"]
    indices = [
        index
        for index, part in enumerate(content)
        if isinstance(part, dict)
        and part.get("type") == "text"
        and part.get("text") in (stripped_text, original_text)
    ]
    if not indices:
        return StripResult(status="conflict", message_id=message_id)
    if len(indices) > 1:
        return StripResult(status="ambiguous", message_id=message_id)
    part_index = indices[0]
    if content[part_index].get("text") == original_text:
        return StripResult(
            status="already",
            message_id=message_id,
            updated_at=str(conversation.get("updated_at") or ""),
        )
    if not apply:
        return StripResult(status="would_restore", message_id=message_id)

    replacement = _replace_prompt_in_line(
        lines[line_index], row, part_index, original_text
    )
    updated_lines = list(lines)
    updated_lines[line_index] = replacement
    mode = messages_path.stat().st_mode & 0o777
    try:
        _atomic_write_bytes(messages_path, b"".join(updated_lines), mode=mode)
    except OSError as exc:
        return StripResult(status="error", message_id=message_id, error=str(exc))
    try:
        _, updated_at = pkg().bump_conversation_updated_at(conversation_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        try:
            _atomic_write_bytes(messages_path, data, mode=mode)
        except OSError:
            pass
        return StripResult(status="error", message_id=message_id, error=str(exc))
    return StripResult(
        status="restored", message_id=message_id, updated_at=updated_at
    )



def identity_lookup_from_registry(
    path: Path,
) -> Callable[[str, str], str]:
    """Build a kind lookup from ``~/.aki/identities.json``-style data."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return lambda _member_id, _name: ""
    if not isinstance(loaded, dict):
        return lambda _member_id, _name: ""
    members = loaded.get("members")
    labels = loaded.get("labels")
    members = members if isinstance(members, dict) else {}
    labels = labels if isinstance(labels, dict) else {}

    def lookup(member_id: str, name: str) -> str:
        if member_id and isinstance(members.get(member_id), dict):
            kind = str(members[member_id].get("kind") or "").strip().casefold()
            if kind in (MEMBER_KIND_USER, MEMBER_KIND_AGENT):
                return kind
        key = canonical_name(name)
        owner = str(labels.get(key) or "")
        if owner and isinstance(members.get(owner), dict):
            kind = str(members[owner].get("kind") or "").strip().casefold()
            if kind in (MEMBER_KIND_USER, MEMBER_KIND_AGENT):
                return kind
        matches: set[str] = set()
        for candidate in members.values():
            if not isinstance(candidate, dict):
                continue
            names = [candidate.get("name"), *(candidate.get("aliases") or [])]
            if key and any(canonical_name(item) == key for item in names):
                kind = str(candidate.get("kind") or "").strip().casefold()
                if kind in (MEMBER_KIND_USER, MEMBER_KIND_AGENT):
                    matches.add(kind)
        return matches.pop() if len(matches) == 1 else ""

    return lookup
