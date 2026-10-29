"""Search a profile's own group and solo chat records."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from engine.live_context import _sanitize


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
) -> str:
    """Return a compact, newest-first search result for one profile."""
    needle = str(query or "").strip().casefold()
    scope = str(scope or "all").strip().lower()
    if scope not in SEARCH_SCOPES:
        scope = "all"
    candidates: list[dict[str, Any]] = []
    if scope in ("all", "group"):
        _append_group(candidates, group_entries)
    if scope in ("all", "solo"):
        _append_solo_history(candidates, solo_entries)
        _append_solo_memory(candidates, solo_memory_sessions)
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
