"""Outbound channel text formatting applied only at delivery time."""

from __future__ import annotations

import re

_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?:(?!(?P=ticks)).)*?(?P=ticks)")


def _opening_fence(line: str) -> tuple[str, int] | None:
    match = _FENCE_OPEN_RE.match(line)
    if match is None:
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _closing_fence(line: str, active: tuple[str, int]) -> bool:
    stripped = line.lstrip()
    character, length = active
    run = len(stripped) - len(stripped.lstrip(character))
    return run >= length and not stripped[run:].strip()


def _break_line(line: str) -> str:
    protected = [
        (match.start(), match.end())
        for match in _INLINE_CODE_RE.finditer(line)
    ]
    result: list[str] = []
    last = 0
    for index, character in enumerate(line):
        if character != "。":
            continue
        lookahead = index + 1
        while lookahead < len(line) and line[lookahead].isspace():
            lookahead += 1
        if lookahead >= len(line):
            continue
        if any(start <= index < end for start, end in protected):
            continue
        result.append(line[last : index + 1])
        result.append("\n\n")
        last = index + 1
    result.append(line[last:])
    return "".join(result)


def insert_chinese_period_breaks(text: str) -> str:
    """Insert blank lines after Chinese full stops outside code.

    This is a delivery-only transform. Callers must keep the original text for
    chat history, transcripts, and model context.
    """
    if "。" not in text:
        return text
    output: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if fence is not None:
            output.append(line)
            if _closing_fence(body, fence):
                fence = None
            continue
        opening = _opening_fence(body)
        if opening is not None:
            output.append(line)
            fence = opening
            continue
        output.append(_break_line(body) + ending)
    return "".join(output)
