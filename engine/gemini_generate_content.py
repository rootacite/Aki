"""Translate OpenAI chat requests to Gemini generateContent and back."""

from __future__ import annotations

import json
import time
import uuid
from typing import Mapping


GEMINI_SAFETY_SETTINGS = (
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
)
_THINKING_LEVELS = {
    "minimal": "LOW",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
}
_CONTENT_FILTER_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "IMAGE_SAFETY",
    "SPII",
}


class GeminiProtocolError(ValueError):
    """The OpenAI request cannot be represented as GenerateContent."""


def _openai_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind in ("image_url", "image"):
            parts.append("[image omitted: Gemini text-only route]")
    return "\n".join(part for part in parts if part)


def _parse_arguments(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}


def _clean_gemini_schema(schema: object) -> dict[str, object]:
    """Recursively convert standard JSON Schema to Gemini Schema proto format."""
    if not isinstance(schema, Mapping):
        return {"type": "object", "properties": {}}

    cleaned: dict[str, object] = {}
    for key, value in schema.items():
        if key in (
            "$schema",
            "$id",
            "$defs",
            "definitions",
            "additionalProperties",
            "propertyNames",
            "patternProperties",
            "multipleOf",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "uniqueItems",
            "minProperties",
            "maxProperties",
        ):
            continue

        if key == "const":
            cleaned["enum"] = [value]
            continue

        if key == "type":
            if isinstance(value, list):
                non_null = [item for item in value if item != "null"]
                cleaned["type"] = str(non_null[0]) if non_null else "string"
                if "null" in value:
                    cleaned["nullable"] = True
                continue
            if isinstance(value, str):
                cleaned["type"] = value
            continue

        if key == "properties" and isinstance(value, Mapping):
            cleaned["properties"] = {
                str(prop_name): _clean_gemini_schema(prop_schema)
                for prop_name, prop_schema in value.items()
            }
            continue

        if key == "items":
            if isinstance(value, Mapping):
                cleaned["items"] = _clean_gemini_schema(value)
            elif isinstance(value, list) and value:
                cleaned["items"] = _clean_gemini_schema(value[0])
            continue

        if key in ("anyOf", "allOf", "oneOf") and isinstance(value, list):
            cleaned_list = [
                _clean_gemini_schema(item)
                for item in value
                if isinstance(item, Mapping)
            ]
            if cleaned_list:
                cleaned[key] = cleaned_list
            continue

        cleaned[key] = value

    return cleaned


def _convert_tools(tools: object) -> list[dict[str, object]]:
    if not isinstance(tools, list):
        return []
    declarations: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        cleaned_parameters = _clean_gemini_schema(parameters)
        if cleaned_parameters.get("type") != "object":
            cleaned_parameters["type"] = "object"
        declaration: dict[str, object] = {
            "name": name,
            "parameters": cleaned_parameters,
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            declaration["description"] = description
        declarations.append(declaration)
    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


def _convert_tool_config(value: object) -> dict[str, object] | None:
    if isinstance(value, str):
        mode = {"auto": "AUTO", "none": "NONE", "required": "ANY"}.get(value)
        if mode:
            return {"functionCallingConfig": {"mode": mode}}
    if isinstance(value, Mapping):
        function = value.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "").strip()
        if name:
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [name],
                }
            }
    return None


def _generation_config(payload: Mapping[str, object]) -> dict[str, object]:
    config: dict[str, object] = {}
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_completion_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        config["maxOutputTokens"] = max_tokens
    effort = str(payload.get("reasoning_effort") or "").strip().lower()
    level = _THINKING_LEVELS.get(effort, "MEDIUM")
    config["thinkingConfig"] = {"thinkingLevel": level}
    return config


def _convert_messages(
    messages: object,
) -> tuple[str, list[dict[str, object]]]:
    if not isinstance(messages, list):
        raise GeminiProtocolError("messages must be an array")
    system_parts: list[str] = []
    contents: list[dict[str, object]] = []
    names_by_id: dict[str, str] = {}
    pending_results: list[dict[str, object]] = []

    def flush_results() -> None:
        if not pending_results:
            return
        contents.append({"role": "user", "parts": list(pending_results)})
        pending_results.clear()

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        content = message.get("content")
        if role in ("system", "developer"):
            text = _openai_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            flush_results()
            text = _openai_text(content)
            if text:
                contents.append(
                    {"role": "user", "parts": [{"text": text}]}
                )
            continue
        if role == "assistant":
            flush_results()
            parts: list[dict[str, object]] = []
            signatures_by_id: dict[str, str] = {}
            reasoning_details = message.get("reasoning_details")
            if isinstance(reasoning_details, list):
                for detail in reasoning_details:
                    if not isinstance(detail, Mapping):
                        continue
                    if detail.get("type") != "reasoning.encrypted":
                        continue
                    call_id = str(detail.get("id") or "")
                    signature = detail.get("data")
                    if call_id and isinstance(signature, str) and signature:
                        signatures_by_id[call_id] = signature
            text = _openai_text(content)
            if text:
                parts.append({"text": text})
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, Mapping):
                        continue
                    function = tool_call.get("function")
                    function = function if isinstance(function, Mapping) else {}
                    name = str(function.get("name") or "").strip()
                    if not name:
                        continue
                    call_id = str(tool_call.get("id") or "")
                    if not call_id:
                        call_id = f"call_{uuid.uuid4().hex}"
                    names_by_id[call_id] = name
                    function_call: dict[str, object] = {
                        "name": name,
                        "args": _parse_arguments(function.get("arguments")),
                        "id": call_id,
                    }
                    signature = signatures_by_id.get(call_id)
                    if signature:
                        parts.append(
                            {
                                "functionCall": function_call,
                                "thoughtSignature": signature,
                            }
                        )
                    else:
                        parts.append({"functionCall": function_call})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = str(
                message.get("name") or names_by_id.get(call_id) or ""
            ).strip()
            if not call_id or not name:
                raise GeminiProtocolError(
                    "tool result requires call_id and name"
                )
            text = _openai_text(content) or "(no tool output)"
            function_response: dict[str, object] = {
                "name": name,
                "response": {"output": text},
                "id": call_id,
            }
            pending_results.append({"functionResponse": function_response})
    flush_results()
    if not contents:
        raise GeminiProtocolError("messages contain no input")
    return "\n\n".join(system_parts), contents


def build_generate_content_payload(
    payload: Mapping[str, object],
    upstream_model: str,
) -> dict[str, object]:
    del upstream_model
    system_instruction, contents = _convert_messages(payload.get("messages"))
    result: dict[str, object] = {"contents": contents}
    if system_instruction:
        result["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }
    tools = _convert_tools(payload.get("tools"))
    if tools:
        result["tools"] = tools
        tool_config = _convert_tool_config(payload.get("tool_choice"))
        if tool_config is not None:
            result["toolConfig"] = tool_config
    result["generationConfig"] = _generation_config(payload)
    result["safetySettings"] = [dict(item) for item in GEMINI_SAFETY_SETTINGS]
    return result


def _usage_to_openai(usage: object) -> dict[str, object] | None:
    if not isinstance(usage, Mapping):
        return None

    def number(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    prompt = number("promptTokenCount")
    completion = number("candidatesTokenCount")
    total = number("totalTokenCount") or prompt + completion
    result: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {
            "cached_tokens": number("cachedContentTokenCount"),
        },
    }
    reasoning = number("thoughtsTokenCount")
    if reasoning:
        result["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return result


def _finish_reason(raw: object, *, has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    reason = str(raw or "").upper()
    if reason == "MAX_TOKENS":
        return "length"
    if reason in _CONTENT_FILTER_FINISH_REASONS:
        return "content_filter"
    return "stop"


def _response_parts(response: Mapping[str, object]) -> list[Mapping[str, object]]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return []
    content = candidate.get("content")
    if not isinstance(content, Mapping):
        return []
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, Mapping)]


def _response_finish_reason(response: Mapping[str, object]) -> object:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    return candidate.get("finishReason") if isinstance(candidate, Mapping) else None


def _tool_call_from_part(part: Mapping[str, object]) -> dict[str, object] | None:
    function_call = part.get("functionCall")
    if not isinstance(function_call, Mapping):
        return None
    name = str(function_call.get("name") or "").strip()
    if not name:
        return None
    call_id = str(function_call.get("id") or "")
    if not call_id:
        call_id = f"call_{uuid.uuid4().hex}"
    arguments = function_call.get("args")
    if not isinstance(arguments, Mapping):
        arguments = {}
    call: dict[str, object] = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            ),
        },
    }
    signature = part.get("thoughtSignature")
    if isinstance(signature, str) and signature:
        call["thoughtSignature"] = signature
    return call


def response_from_generate_content(
    response: Mapping[str, object],
    *,
    model: str,
) -> dict[str, object]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    for part in _response_parts(response):
        if part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str):
            text_parts.append(text)
        call = _tool_call_from_part(part)
        if call is not None:
            tool_calls.append(call)
    message: dict[str, object] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
            for call in tool_calls
        ]
        reasoning_details = [
            {
                "type": "reasoning.encrypted",
                "id": str(call["id"]),
                "data": str(call["thoughtSignature"]),
            }
            for call in tool_calls
            if isinstance(call.get("thoughtSignature"), str)
        ]
        if reasoning_details:
            message["reasoning_details"] = reasoning_details
    result: dict[str, object] = {
        "id": str(response.get("responseId") or f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(
                    _response_finish_reason(response),
                    has_tools=bool(tool_calls),
                ),
            }
        ],
    }
    usage = _usage_to_openai(response.get("usageMetadata"))
    if usage is not None:
        result["usage"] = usage
    return result


class GenerateContentStreamTranslator:
    def __init__(self, model: str) -> None:
        self.model = model
        self.created = int(time.time())
        self.response_id = ""
        self.role_sent = False
        self.completed = False
        self.usage: dict[str, object] | None = None
        self.finish_reason: str | None = None
        self._tool_calls: list[dict[str, object]] = []
        self._seen_tool_calls: set[str] = set()

    def _chunk(
        self,
        delta: Mapping[str, object],
        *,
        finish_reason: str | None = None,
        include_usage: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.response_id or f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": dict(delta),
                    "finish_reason": finish_reason,
                }
            ],
        }
        if include_usage and self.usage is not None:
            payload["usage"] = self.usage
        return payload

    def _role_chunk(self) -> list[dict[str, object]]:
        if self.role_sent:
            return []
        self.role_sent = True
        return [self._chunk({"role": "assistant"})]

    def feed(self, response: Mapping[str, object]) -> list[dict[str, object]]:
        response_id = response.get("responseId")
        if isinstance(response_id, str) and response_id:
            self.response_id = response_id
        chunks = self._role_chunk()
        for part in _response_parts(response):
            if part.get("thought") is True:
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(self._chunk({"content": text}))
            call = _tool_call_from_part(part)
            if call is None:
                continue
            signature = json.dumps(call, ensure_ascii=False, sort_keys=True)
            if signature in self._seen_tool_calls:
                continue
            self._seen_tool_calls.add(signature)
            self._tool_calls.append(call)
        usage = _usage_to_openai(response.get("usageMetadata"))
        if usage is not None:
            self.usage = usage
        finish = _response_finish_reason(response)
        if finish is not None:
            self.finish_reason = _finish_reason(
                finish, has_tools=False
            )
        return chunks

    def finalize(self) -> list[dict[str, object]]:
        if self.completed:
            return []
        chunks = self._role_chunk()
        if self._tool_calls:
            reasoning_details = [
                {
                    "type": "reasoning.encrypted",
                    "id": str(call.get("id") or ""),
                    "data": str(call.get("thoughtSignature")),
                }
                for call in self._tool_calls
                if isinstance(call.get("thoughtSignature"), str)
            ]
            if reasoning_details:
                chunks.append(
                    self._chunk({"reasoning_details": reasoning_details})
                )
            chunks.append(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call.get("id"),
                                "type": "function",
                                "function": call.get("function"),
                            }
                            for index, call in enumerate(self._tool_calls)
                        ]
                    }
                )
            )
            finish = "tool_calls"
        else:
            finish = self.finish_reason or "stop"
            if finish == "tool_calls":
                finish = "stop"
        chunks.append(
            self._chunk(
                {},
                finish_reason=finish,
                include_usage=True,
            )
        )
        self.completed = True
        return chunks
