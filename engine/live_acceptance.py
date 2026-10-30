"""Analyze passive proxy-debug captures for the final live API acceptance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


CHANNEL_HEADER = "[akid MQTT channel message]"

SUPPRESSED_REMINDER_MARKERS = (
    "automated message providing context about the user's environment",
    "automated message providing information about you",
    "MCP servers with available tools:",
    "Permission mode active:",
    "Permission mode changed to:",
)

BLOCK_ORDER = (
    "solo-memories",
    "channel-transcript",
    "solo-mode",
    "solo-transcript",
    "context",
    "turn-control",
    "format-guard",
)

_BLOCK_RE = re.compile(
    r"(?m)^# (solo-mode|solo-memories|channel-transcript|solo-transcript|"
    r"live-context|runtime-context|turn-control|format-guard)\b[^\n]*$"
)
_SEQ_RE = re.compile(r"(?m)^seq:[ \t]*(\d+)[ \t]*$")
_SOURCE_RE = re.compile(r"(?m)^source:[ \t]*(user|agent)[ \t]*$")
_OLD_ATTR_RE = re.compile(
    r"\b(?:budget_tokens|shown|total|dropped|frozen|from_agent)\s*="
    r"|\bsolo\s*="
)
_OLD_ENVELOPE_RE = re.compile(
    r"<(?:turn-control|channel-transcript|solo-memories|solo-mode|"
    r"solo-transcript|live-context|runtime-context|format-guard)\b"
)


@dataclass
class RequestAnalysis:
    request_id: str
    turn_seq: int
    block_sequence: tuple[str, ...]
    errors: list[str] = field(default_factory=list)


def load_event_lines(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.is_file():
        return events, [f"capture file is missing: {path}"]
    for number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except ValueError as exc:
            errors.append(f"line {number}: malformed JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"line {number}: event is not an object")
            continue
        if not isinstance(value.get("event"), str):
            errors.append(f"line {number}: missing event")
            continue
        events.append(value)
    return events, errors


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if (
            isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            parts.append(str(part["text"]))
    return "\n".join(parts)


def payload_messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if isinstance(messages, list):
        return [message for message in messages if isinstance(message, dict)]
    raw_input = payload.get("input")
    if not isinstance(raw_input, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_input:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, list):
            content = [
                dict(part)
                for part in content
                if isinstance(part, Mapping)
            ]
        normalized.append({"role": role, "content": content})
    return normalized


def last_channel_user_message(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, int] | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = content_text(message.get("content"))
        marker = text.rfind(CHANNEL_HEADER)
        if marker < 0:
            continue
        prompt = text[marker:]
        source = _SOURCE_RE.search(prompt)
        if source is None or source.group(1) != "user":
            continue
        seq_match = _SEQ_RE.search(prompt)
        if seq_match is None:
            continue
        return text, int(seq_match.group(1))
    return None


def parse_overlay(prompt: str) -> tuple[str, list[tuple[str, str]], list[str]]:
    marker = prompt.find(CHANNEL_HEADER)
    if marker < 0:
        return prompt, [], ["channel prompt header is missing"]
    prefix = prompt[:marker]
    matches = list(_BLOCK_RE.finditer(prefix))
    blocks: list[tuple[str, str]] = []
    errors: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prefix)
        raw_name = match.group(1)
        name = "context" if raw_name in ("live-context", "runtime-context") else raw_name
        blocks.append((name, prefix[match.start() : end]))
    return prefix, blocks, errors


def _balanced(text: str, tag: str) -> bool:
    opens = len(re.findall(rf"<{re.escape(tag)}(?:\s|>)", text))
    closes = text.count(f"</{tag}>")
    return opens == closes and opens > 0


def _validate_block(name: str, block: str, errors: list[str]) -> None:
    if name == "context":
        if "## MemFS" not in block:
            errors.append("live/runtime context is missing ## MemFS")
        if "## MCP" not in block:
            errors.append("live/runtime context is missing ## MCP")
    elif name in ("channel-transcript", "solo-transcript"):
        for required in ("## Note", "## Content", "<messages>", "</messages>"):
            if required not in block:
                errors.append(f"{name} is missing {required}")
        if block.count("<messages>") != 1 or block.count("</messages>") != 1:
            errors.append(f"{name} has malformed <messages> boundaries")
    elif name == "solo-memories":
        for required in ("## Note", "## Content"):
            if required not in block:
                errors.append(f"solo-memories is missing {required}")
        if not _balanced(block, "solo"):
            errors.append("solo-memories has malformed <solo> boundaries")
    elif name == "turn-control":
        for required in (
            "## Turn Action & Permissions",
            "## Initiation Budget",
            "## Wait User",
        ):
            if required not in block:
                errors.append(f"turn-control is missing {required}")
        if not _balanced(block, "wait-user"):
            errors.append("turn-control has malformed <wait-user> boundaries")
        if "<wait-other" in block and not _balanced(block, "wait-other"):
            errors.append("turn-control has malformed <wait-other> boundaries")
    elif name == "format-guard":
        if (
            "LENGTH MIRRORING (CRITICAL)" not in block
            or "ZERO MARKDOWN BLOAT" not in block
        ):
            errors.append("format-guard body is missing")


def analyze_request(
    event: Mapping[str, Any],
    terminal_ids: set[str],
) -> RequestAnalysis | None:
    request_id = str(event.get("request_id") or "")
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    messages = payload_messages(payload)
    channel = last_channel_user_message(messages)
    if channel is None:
        return None
    prompt, seq = channel
    prefix, blocks, parse_errors = parse_overlay(prompt)
    errors = list(parse_errors)
    if not request_id:
        errors.append("request_id is missing")
    if request_id and request_id not in terminal_ids:
        errors.append(f"request {request_id} has no response_end or error")
    names = tuple(name for name, _block in blocks)
    if len(set(names)) != len(names):
        errors.append("overlay block appears more than once")
    ranks = [BLOCK_ORDER.index(name) for name in names if name in BLOCK_ORDER]
    if ranks != sorted(ranks) or len(ranks) != len(names):
        errors.append(f"overlay block order is invalid: {names}")
    if names.count("context") != 1:
        errors.append("live-context/runtime-context must appear exactly once")
    if "turn-control" not in names:
        errors.append("turn-control is missing")
    if not names or names[-1] != "format-guard":
        errors.append("format-guard must be the last overlay block")
    if len(names) < 2 or names[-2] != "turn-control":
        errors.append("turn-control must be the second-last overlay block")
    if prompt.count(CHANNEL_HEADER) != 1:
        errors.append("actual user message must appear exactly once")
    if _OLD_ATTR_RE.search(prefix):
        errors.append("overlay contains legacy framework attributes")
    envelope = _OLD_ENVELOPE_RE.search(prefix)
    if envelope is not None:
        errors.append(f"overlay contains legacy XML envelope {envelope.group(0)!r}")
    for name, block in blocks:
        _validate_block(name, block, errors)
    all_text = "\n".join(content_text(message.get("content")) for message in messages)
    for marker in SUPPRESSED_REMINDER_MARKERS:
        if marker in all_text:
            errors.append(f"suppressed Letta reminder is present: {marker}")
            break
    return RequestAnalysis(
        request_id=request_id,
        turn_seq=seq,
        block_sequence=names,
        errors=errors,
    )


def analyze_events(
    events: Sequence[Mapping[str, Any]],
    *,
    expected_turns: int = 5,
    parse_errors: Sequence[str] = (),
) -> dict[str, Any]:
    errors = list(parse_errors)
    terminal_ids = {
        str(event.get("request_id") or "")
        for event in events
        if str(event.get("event") or "") in ("response_end", "error")
        and str(event.get("request_id") or "")
    }
    seen_request_ids: set[str] = set()
    duplicate_request_ids: set[str] = set()
    requests: list[RequestAnalysis] = []
    for event in events:
        kind = str(event.get("event") or "")
        request_id = str(event.get("request_id") or "")
        if kind != "request":
            continue
        if request_id:
            if request_id in seen_request_ids:
                duplicate_request_ids.add(request_id)
            seen_request_ids.add(request_id)
        analysis = analyze_request(event, terminal_ids)
        if analysis is not None:
            requests.append(analysis)

    if duplicate_request_ids:
        errors.append(
            "duplicate request_id values: "
            + ", ".join(sorted(duplicate_request_ids))
        )

    by_turn: dict[int, list[RequestAnalysis]] = {}
    for request in requests:
        by_turn.setdefault(request.turn_seq, []).append(request)
    if len(by_turn) != expected_turns:
        errors.append(
            f"expected {expected_turns} distinct user turns, received {len(by_turn)}"
        )
    turn_reports: list[dict[str, Any]] = []
    pending = False
    for seq, turn_requests in sorted(by_turn.items()):
        turn_errors: list[str] = []
        if not turn_requests:
            turn_errors.append("turn has no provider request")
        sequences = {request.block_sequence for request in turn_requests}
        if len(sequences) > 1:
            turn_errors.append("overlay differs across requests in the same turn")
        for request in turn_requests:
            turn_errors.extend(request.errors)
            if any("no response_end or error" in item for item in request.errors):
                pending = True
        turn_reports.append(
            {
                "seq": seq,
                "request_ids": [request.request_id for request in turn_requests],
                "overlay": list(turn_requests[0].block_sequence),
                "errors": sorted(set(turn_errors)),
            }
        )
    if any(turn.get("errors") for turn in turn_reports):
        errors.extend(
            f"turn {turn['seq']}: {error}"
            for turn in turn_reports
            for error in turn["errors"]
        )
    if parse_errors:
        status = "FAIL"
    elif pending or len(by_turn) < expected_turns:
        status = "INCOMPLETE"
    elif errors:
        status = "FAIL"
    else:
        status = "PASS"
    return {
        "status": status,
        "expected_turns": expected_turns,
        "received_turns": len(by_turn),
        "request_count": len(requests),
        "user_seqs": sorted(by_turn),
        "turns": turn_reports,
        "errors": sorted(dict.fromkeys(errors)),
    }
