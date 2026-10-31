"""Recover a visible reply that the model left inside reasoning.

Some local completions stop before they close the think block. The user-facing
answer is then stored only as reasoning, sometimes twice, and the channel has
nothing to publish. This split is a fallback for that shape. It does not change
why the model wrote the answer there.
"""

from __future__ import annotations

import json
import re
from typing import Any

_MIN_REPLY_CHARS = 8
_PLAN_RE = re.compile(
    r"Master|我应该|我先|我简单|我说点|我来调用|不要长篇|保持\s*IM|从截图|从历史|可以提议|看看当前"
)


def recover_visible_reply(reasoning: str) -> tuple[str, str] | None:
    """Return ``(planning, reply)`` when the split is confident.

    ``planning`` is the chain of thought that should stay in reasoning.
    ``reply`` is the text that should have been the assistant content.
    """
    text = reasoning.strip()
    if len(text) < _MIN_REPLY_CHARS:
        return None
    peeled = _peel_repeated_suffix(text)
    if peeled is not None:
        plan, reply = peeled
        if plan.endswith(reply):
            plan = plan[: -len(reply)].rstrip()
        if reply:
            return plan.strip(), reply
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return None
    plan_flags = [_is_plan(paragraph) for paragraph in paragraphs]
    if not any(plan_flags):
        if _is_plan(text):
            return None
        return "", text
    cut = len(paragraphs)
    while cut > 0 and not plan_flags[cut - 1]:
        cut -= 1
    if cut == 0 or cut == len(paragraphs):
        return None
    reply = "\n\n".join(paragraphs[cut:]).strip()
    plan = "\n\n".join(paragraphs[:cut]).strip()
    if len(reply) < _MIN_REPLY_CHARS or not plan:
        return None
    return plan, reply


def _peel_repeated_suffix(text: str) -> tuple[str, str] | None:
    """Return the longest suffix that is written twice at the end of ``text``."""
    length = len(text)
    for size in range(length // 2, _MIN_REPLY_CHARS - 1, -1):
        if text[-size:] != text[-2 * size : -size]:
            continue
        reply = text[-size:].strip()
        if len(reply) < _MIN_REPLY_CHARS:
            continue
        plan = text[:-size].rstrip()
        return plan, reply
    return None


def _paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.strip())
    return [part.strip() for part in parts if part.strip()]


def rewrite_recovered_assistant(
    lines: list[bytes], *, reasoning: str, plan: str, reply: str
) -> list[bytes]:
    """Replace the last thinking-only assistant row that matches ``reasoning``."""
    target: int | None = None
    rows: list[dict[str, Any] | None] = []
    wanted = reasoning.strip()
    for index, line in enumerate(lines):
        if not line.strip():
            rows.append(None)
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"conversation row {index + 1} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"conversation row {index + 1} is not an object")
        rows.append(value)
        message = value.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        thinking = _thinking_text(content)
        visible = _visible_text(content)
        if thinking.strip() == wanted and not visible.strip():
            target = index
    if target is None:
        raise ValueError("no thinking-only assistant message matches this reasoning")
    row = rows[target]
    assert row is not None
    message = row["message"]
    content = message["content"]
    signature = ""
    kept: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "thinking":
            signature = str(part.get("thinkingSignature") or signature)
            continue
        if part.get("type") == "text":
            continue
        kept.append(dict(part))
    rebuilt: list[dict[str, Any]] = []
    if plan.strip():
        thinking_part: dict[str, Any] = {
            "type": "thinking",
            "thinking": plan.strip(),
        }
        if signature:
            thinking_part["thinkingSignature"] = signature
        rebuilt.append(thinking_part)
    rebuilt.append({"type": "text", "text": reply})
    rebuilt.extend(kept)
    message["content"] = rebuilt
    lines = list(lines)
    lines[target] = (
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return lines


def _thinking_text(content: list[Any]) -> str:
    return "".join(
        str(part.get("thinking") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "thinking"
    )


def _visible_text(content: list[Any]) -> str:
    return "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _is_plan(paragraph: str) -> bool:
    if _PLAN_RE.search(paragraph):
        return True
    return any(line.lstrip().startswith("- ") for line in paragraph.splitlines())
