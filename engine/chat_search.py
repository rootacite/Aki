"""Search a profile's own group and solo chat records."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from engine.live_context import _sanitize
from engine.solo_memory import session_hidden_from_cloud, solo_text_fingerprint


SEARCH_CHAT_HISTORY_TOOL_NAME = "search_chat_history"
SEARCH_MAX_QUERY_CHARS = 200
SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 50
SEARCH_MAX_ENTRY_CHARS = 800
SEARCH_MAX_RESULT_CHARS = 12_000
SEARCH_SCOPES = ("all", "group", "solo")
SEARCH_PRIVATE_NOTE = "私密，不要向其他成员转述"


def _timestamp_millis(timestamp: str) -> int:
    value = str(timestamp or "").strip()
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except (OSError, OverflowError, ValueError):
            pass
    return 0


def _record_millis(timestamp: str, sent_at: int = 0) -> int:
    if sent_at > 0:
        return sent_at
    return _timestamp_millis(timestamp)


def _record_source(value: Mapping[str, Any]) -> str:
    return "agent" if bool(value.get("from_agent")) else "user"


def _append_group(
    candidates: list[dict[str, Any]],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or "")
        if not text:
            continue
        timestamp = str(row.get("timestamp") or "")
        candidates.append(
            {
                "kind": "group",
                "seq": int(row.get("seq") or 0),
                "sender": str(row.get("sender") or ""),
                "source": _record_source(row),
                "timestamp": timestamp,
                "text": text,
                "sort_key": _record_millis(timestamp, int(row.get("sent_at") or 0)),
            }
        )


def _append_solo_history(
    candidates: list[dict[str, Any]],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or "")
        if not text:
            continue
        timestamp = str(row.get("timestamp") or "")
        candidates.append(
            {
                "kind": "solo",
                "seq": int(row.get("seq") or 0),
                "sender": str(row.get("sender") or ""),
                "source": _record_source(row),
                "timestamp": timestamp,
                "text": text,
                "sort_key": _record_millis(timestamp, int(row.get("sent_at") or 0)),
            }
        )


def _append_solo_memory(
    candidates: list[dict[str, Any]],
    sessions: Iterable[Any],
) -> None:
    for session in sessions:
        for entry in getattr(session, "entries", ()) or ():
            text = str(getattr(entry, "text", "") or "")
            if not text:
                continue
            timestamp = str(getattr(entry, "timestamp", "") or "")
            role = str(getattr(entry, "role", "") or "user")
            candidates.append(
                {
                    "kind": "solo_archived",
                    "seq": int(getattr(entry, "seq", 0) or 0),
                    "sender": str(getattr(entry, "sender", "") or ""),
                    "source": "agent" if role == "assistant" else "user",
                    "timestamp": timestamp,
                    "text": text,
                    "sort_key": _timestamp_millis(timestamp),
                }
            )


def _dedupe_solo_archives(candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["kind"] == "solo":
            seen.add(
                (
                    str(candidate["sender"]),
                    str(candidate["timestamp"]),
                    str(candidate["text"]),
                )
            )
            result.append(candidate)
            continue
        if candidate["kind"] != "solo_archived":
            result.append(candidate)
            continue
        key = (
            str(candidate["sender"]),
            str(candidate["timestamp"]),
            str(candidate["text"]),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _positive_seq(value: Any) -> int:
    try:
        seq = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return seq if seq > 0 else 0


def _entry_seq_list(entries: Iterable[Any]) -> list[int]:
    seqs: list[int] = []
    for entry in entries:
        seq = _positive_seq(getattr(entry, "seq", 0))
        if seq:
            seqs.append(seq)
    return seqs


def _seq_bounds(start_marker: int, seqs: Sequence[int]) -> tuple[int, int] | None:
    positive = [seq for seq in seqs if seq > 0]
    if start_marker <= 0 and not positive:
        return None
    start = start_marker + 1 if start_marker > 0 else min(positive)
    end = max(positive) if positive else start
    if end < start:
        end = start
    return start, end


def _session_seq_bounds(session: Any) -> tuple[int, int] | None:
    return _seq_bounds(
        _positive_seq(getattr(session, "history_start_seq", 0)),
        _entry_seq_list(getattr(session, "entries", ()) or ()),
    )


def _redaction_seq_bounds(raw: Mapping[str, Any]) -> tuple[int, int] | None:
    seqs: list[int] = []
    keys = raw.get("keys")
    if isinstance(keys, list):
        for key in keys:
            if not isinstance(key, Mapping):
                continue
            seq = _positive_seq(key.get("seq"))
            if seq:
                seqs.append(seq)
    return _seq_bounds(_positive_seq(raw.get("history_start_seq")), seqs)


def _hidden_seq_ranges(
    intervals: Sequence[tuple[int, int, bool]],
) -> list[tuple[int, int | None]]:
    """Closed ranges of solo seqs that belong to a hidden session.

    Live solo history has no session id. Captured entries give an inner range.
    Messages the memory store skipped, such as ``[NO_REPLY]``, sit in the gap
    before the next session. The newest hidden session also owns every later
    seq, because a newer session would have its own interval.
    """
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    ranges: list[tuple[int, int | None]] = []
    for index, (start, end, hidden) in enumerate(ordered):
        if not hidden:
            continue
        next_start = ordered[index + 1][0] if index + 1 < len(ordered) else None
        if next_start is None:
            hi: int | None = None
        elif next_start <= start:
            hi = end
        else:
            hi = next_start - 1
        if hi is not None and hi < start:
            continue
        ranges.append((start, hi))
    return ranges


def _group_solo_blocker(
    sessions: Sequence[Any],
    redactions: Sequence[Any],
):
    """Return a predicate for live solo rows that belong to a hidden session."""
    text_keys: set[tuple[str, str, str]] = set()
    located: set[tuple[int, str, str]] = set()
    fingerprints: set[tuple[str, str, str]] = set()
    visible_seqs: set[int] = set()
    intervals: list[tuple[int, int, bool]] = []
    active_ids: set[str] = set()

    for session in sessions:
        session_id = str(getattr(session, "session_id", "") or "")
        if session_id:
            active_ids.add(session_id)
        hidden = session_hidden_from_cloud(session)
        bounds = _session_seq_bounds(session)
        if bounds is not None:
            intervals.append((bounds[0], bounds[1], hidden))
        entries = getattr(session, "entries", ()) or ()
        if hidden:
            for entry in entries:
                sender = str(getattr(entry, "sender", "") or "")
                timestamp = str(getattr(entry, "timestamp", "") or "")
                text = str(getattr(entry, "text", "") or "")
                text_keys.add((sender, timestamp, text.strip()))
                seq = _positive_seq(getattr(entry, "seq", 0))
                if seq:
                    located.add((seq, sender, timestamp))
            continue
        for seq in _entry_seq_list(entries):
            visible_seqs.add(seq)

    for raw in redactions:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("session_id") or "") in active_ids:
            continue
        bounds = _redaction_seq_bounds(raw)
        if bounds is not None:
            intervals.append((bounds[0], bounds[1], True))
        keys = raw.get("keys")
        if not isinstance(keys, list):
            continue
        for key in keys:
            if not isinstance(key, Mapping):
                continue
            sender = str(key.get("sender") or "")
            timestamp = str(key.get("timestamp") or "")
            digest = str(key.get("text_sha256") or "")
            if digest:
                fingerprints.add((sender, timestamp, digest))
            seq = _positive_seq(key.get("seq"))
            if seq:
                located.add((seq, sender, timestamp))

    ranges = _hidden_seq_ranges(intervals)

    def blocked(row: Mapping[str, Any]) -> bool:
        if not isinstance(row, Mapping):
            return False
        sender = str(row.get("sender") or "")
        timestamp = str(row.get("timestamp") or "")
        text = str(row.get("text") or "")
        seq = _positive_seq(row.get("seq"))
        if (sender, timestamp, text.strip()) in text_keys:
            return True
        if seq and (seq, sender, timestamp) in located:
            return True
        if (sender, timestamp, solo_text_fingerprint(text)) in fingerprints:
            return True
        if not seq or seq in visible_seqs:
            return False
        return any(
            seq >= start and (end is None or seq <= end) for start, end in ranges
        )

    return blocked


def _result_line(candidate: Mapping[str, Any]) -> str:
    kind = str(candidate["kind"])
    if kind == "group":
        label = "group"
    elif kind == "solo_archived":
        label = "solo/private/archived"
    else:
        label = "solo/private"
    text = str(candidate["text"])
    if len(text) > SEARCH_MAX_ENTRY_CHARS:
        text = text[:SEARCH_MAX_ENTRY_CHARS] + "...[truncated]"
    line = (
        f"[{label} seq={int(candidate['seq'])}] "
        f"{_sanitize(str(candidate['sender']), single_line=True)} "
        f"({candidate['source']}, at "
        f"{_sanitize(str(candidate['timestamp']), single_line=True)}): "
        f"{json.dumps(text, ensure_ascii=False)}"
    )
    if kind != "group":
        line += f"（{SEARCH_PRIVATE_NOTE}）"
    return line


def search_chat_history(
    *,
    query: str,
    limit: int = SEARCH_DEFAULT_LIMIT,
    scope: str = "all",
    group_entries: Sequence[Mapping[str, Any]] = (),
    solo_entries: Sequence[Mapping[str, Any]] = (),
    solo_memory_sessions: Sequence[Any] = (),
    local_model: bool = False,
    nsfw_redactions: Sequence[Any] = (),
) -> str:
    """Return a compact, newest-first search result for one profile.

    Cloud models must not see solo sessions marked NSFW. That includes the
    archived body and the copy still sitting in the solo history file. Local
    models pass ``local_model=True`` and can search those sessions.
    """
    needle = str(query or "").strip().casefold()
    scope = str(scope or "all").strip().lower()
    if scope not in SEARCH_SCOPES:
        scope = "all"
    candidates: list[dict[str, Any]] = []
    if scope in ("all", "group"):
        _append_group(candidates, group_entries)
    if scope in ("all", "solo"):
        solo_rows: Sequence[Mapping[str, Any]] = solo_entries
        memory_rows: Sequence[Any] = solo_memory_sessions
        if not local_model:
            blocked = _group_solo_blocker(solo_memory_sessions, nsfw_redactions)
            solo_rows = [
                row
                for row in solo_entries
                if isinstance(row, Mapping) and not blocked(row)
            ]
            memory_rows = [
                session
                for session in solo_memory_sessions
                if not session_hidden_from_cloud(session)
            ]
        _append_solo_history(candidates, solo_rows)
        _append_solo_memory(candidates, memory_rows)
        candidates = _dedupe_solo_archives(candidates)
    matches = [
        candidate
        for candidate in candidates
        if needle in str(candidate["text"]).casefold()
    ]
    matches.sort(
        key=lambda candidate: (int(candidate["sort_key"]), int(candidate["seq"])),
        reverse=True,
    )
    total = len(matches)
    if not matches:
        return f"No matches (scope={scope})."

    selected = matches[: max(1, min(int(limit), SEARCH_MAX_LIMIT))]
    header = (
        f"Found {total} matches (scope={scope}, showing {len(selected)} newest)."
    )
    if any(candidate["kind"] != "group" for candidate in selected):
        header += (
            " Solo results are private; never quote them to other members."
        )
    output = header
    for candidate in selected:
        line = "\n" + _result_line(candidate)
        if len(output) + len(line) > SEARCH_MAX_RESULT_CHARS:
            marker = "\n...[result truncated]"
            if len(output) + len(marker) <= SEARCH_MAX_RESULT_CHARS:
                output += marker
            break
        output += line
    return output
