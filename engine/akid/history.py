"""Shrink history payloads before they are sent back to a client."""

from __future__ import annotations

import json
import time
from datetime import (
    datetime,
    timezone,
)
from .constants import (
    HISTORY_IMAGE_PLACEHOLDER,
    HISTORY_MAX_BYTES,
    MAX_HISTORY_TEXT_CHARS,
    MAX_MCP_TOOL_TEXT_CHARS,
)



def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""



def _parse_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments



def _truncate_mcp_text(text: str) -> str:
    if len(text) <= MAX_MCP_TOOL_TEXT_CHARS:
        return text
    return text[:MAX_MCP_TOOL_TEXT_CHARS] + "\n...[truncated by akid]"



def _mcp_content_part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    kind = part.get("type")
    if kind == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if kind == "image":
        mime = part.get("mimeType") or part.get("mime_type") or "image"
        data = part.get("data")
        size = len(data) if isinstance(data, str) else 0
        return f"[image omitted: {mime}, {size} base64 chars]"
    if kind == "resource" and isinstance(part.get("resource"), dict):
        resource = part["resource"]
        if isinstance(resource.get("text"), str):
            return resource["text"]
        return json.dumps(resource, ensure_ascii=False)
    return json.dumps(part, ensure_ascii=False)



def _normalize_mcp_result(raw: str) -> tuple[str, bool]:
    text = raw.strip()
    if not text:
        return "MCP tool returned no output", False
    try:
        payload = json.loads(text)
    except ValueError:
        return _truncate_mcp_text(text), False
    if not isinstance(payload, dict):
        return _truncate_mcp_text(json.dumps(payload, ensure_ascii=False)), False

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or json.dumps(error, ensure_ascii=False)
        return _truncate_mcp_text(str(message)), True
    if error:
        return _truncate_mcp_text(str(error)), True

    is_error = bool(payload.get("isError") or payload.get("is_error"))
    parts: list[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        parts.extend(
            text_part
            for part in content
            if (text_part := _mcp_content_part_text(part))
        )
    elif content is not None:
        text_part = _mcp_content_part_text(content)
        if text_part:
            parts.append(text_part)
    if not parts and payload.get("structuredContent") is not None:
        parts.append(json.dumps(payload["structuredContent"], ensure_ascii=False))
    if not parts:
        parts.append(json.dumps(payload, ensure_ascii=False))
    return _truncate_mcp_text("\n".join(parts)), is_error



def _truncate_history_text(text: str) -> str:
    if len(text) <= MAX_HISTORY_TEXT_CHARS:
        return text
    omitted = len(text) - MAX_HISTORY_TEXT_CHARS
    return text[:MAX_HISTORY_TEXT_CHARS] + f"\n…[truncated {omitted} chars]"



def _sanitize_history_value(value: Any) -> Any:
    """Remove heavy payloads that must not travel over the gateway socket."""
    if isinstance(value, str):
        return _truncate_history_text(value)
    if isinstance(value, list):
        return [_sanitize_history_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "text", "text": HISTORY_IMAGE_PLACEHOLDER}
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key in {"data", "base64", "image_url"}
                and isinstance(item, str)
                and len(item) > 256
            ):
                sanitized[key] = HISTORY_IMAGE_PLACEHOLDER
            else:
                sanitized[key] = _sanitize_history_value(item)
        return sanitized
    return value



def _compact_history_messages(messages: Any) -> tuple[list[dict[str, Any]], bool]:
    """Sanitize history and cap the encoded size of one response batch."""
    if not isinstance(messages, list):
        return [], False
    compacted: list[dict[str, Any]] = []
    used = 0
    dropped = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        sanitized = _sanitize_history_value(message)
        try:
            size = len(
                json.dumps(
                    sanitized, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            continue
        if size > HISTORY_MAX_BYTES:
            sanitized = {
                "id": sanitized.get("id"),
                "date": sanitized.get("date"),
                "message_type": sanitized.get("message_type"),
                "role": sanitized.get("role"),
                "content": [{"type": "text", "text": "[message too large to display]"}],
            }
            size = len(
                json.dumps(
                    sanitized, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        if compacted and used + size > HISTORY_MAX_BYTES:
            dropped = True
            break
        compacted.append(sanitized)
        used += size
    return compacted, dropped



def _format_channel_message_time(sent_at: int) -> str:
    """Format a channel message timestamp in the server's local timezone."""
    try:
        seconds = int(sent_at) / 1000 if int(sent_at) > 0 else time.time()
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
