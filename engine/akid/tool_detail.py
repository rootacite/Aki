"""Render tool arguments and results for a channel tool card."""

from __future__ import annotations

import json
from typing import Any

TOOL_DETAIL_MAX_CHARS = 32_000
_TRUNCATED = "\n… (truncated)"


def render_tool_value(value: Any, *, limit: int = TOOL_DETAIL_MAX_CHARS) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    return _truncate(text, limit)


def render_tool_result(data: dict[str, Any], *, limit: int = TOOL_DETAIL_MAX_CHARS) -> str:
    parts: list[str] = []
    tool_return = data.get("tool_return")
    if tool_return not in (None, ""):
        parts.append(render_tool_value(tool_return, limit=limit))
    for label, key in (("stdout", "stdout"), ("stderr", "stderr")):
        extra = data.get(key)
        if extra in (None, ""):
            continue
        rendered = render_tool_value(extra, limit=limit)
        if not rendered or (parts and rendered == parts[0]):
            continue
        parts.append(f"--- {label} ---\n{rendered}")
    return _truncate("\n".join(parts), limit)


def tool_detail_body(*, arguments: str, result: str | None) -> str:
    payload: dict[str, str] = {"arguments": arguments}
    if result is not None:
        payload["result"] = result
    return json.dumps(payload, ensure_ascii=False)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATED))
    return text[:keep] + _TRUNCATED
