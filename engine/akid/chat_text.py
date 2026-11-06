"""Sanitize agent chat text before channel publication."""

from __future__ import annotations

import re

_ESCAPE_ARTIFACT_LINE_RE = re.compile(
    r"^[ \t]*(?:['\"`]?(?:\\+n|<br\s*/?>)['\"`]?[ \t]*)+$",
    re.IGNORECASE,
)
_INLINE_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def sanitize_chat_text(text: str) -> str:
    """Clean delimiter and escape artifacts outside fenced code blocks.

    1. Discards standalone lines consisting solely of escape delimiters (e.g. \\n\\n, <br>).
    2. Converts inline <br> tags outside code fences into blank lines (\\n\\n).
    3. Collapses excessive consecutive newlines down to two.
    """
    if not text:
        return text
    lines = text.split("\n")
    processed: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_char = stripped[0]
                fence_len = len(stripped) - len(stripped.lstrip(fence_char))
                processed.append(line)
                continue
            if _ESCAPE_ARTIFACT_LINE_RE.match(stripped):
                processed.append("")
                continue
            line = _INLINE_BR_RE.sub("\n\n", line)
            processed.append(line)
        else:
            if stripped.startswith(fence_char * fence_len) and not stripped.rstrip(fence_char):
                in_fence = False
            processed.append(line)

    result = "\n".join(processed)
    return re.sub(r"\n{3,}", "\n\n", result).strip()
