"""Translate OpenAI chat requests to Gemini Interactions requests and back."""

from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping


GEMINI_API_REVISION = "2026-05-20"
GEMINI_SAFETY_SETTINGS = (
    {"type": "harassment", "threshold": "off"},
    {"type": "hate_speech", "threshold": "off"},
    {"type": "sexually_explicit", "threshold": "off"},
    {"type": "dangerous_content", "threshold": "off"},
)
_THINKING_LEVELS = {"minimal", "low", "medium", "high"}


class GeminiProtocolError(ValueError):
    """The upstream Interactions payload cannot be represented safely."""


@dataclass(frozen=True)
class GeminiContinuation:
    interaction_id: str
    results: tuple[dict[str, object], ...]


class GeminiToolContinuationStore:
    """Remember which interaction produced each function call."""

    def __init__(self, max_entries: int = 512) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()

    def remember(self, interaction_id: str, call_ids: list[str]) -> None:
        if not interaction_id:
            return
        for call_id in call_ids:
            if not call_id:
                continue
            self._entries[call_id] = interaction_id
            self._entries.move_to_end(call_id)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def lookup(self, call_id: str) -> str | None:
        value = self._entries.get(call_id)
        if value is not None:
            self._entries.move_to_end(call_id)
        return value


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


def _function_call_text(tool_call: Mapping[str, object]) -> str:
    function = tool_call.get("function")
    function = function if isinstance(function, Mapping) else {}
    name = str(function.get("name") or tool_call.get("name") or "")
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        arguments_text = arguments
    elif arguments is None:
        arguments_text = "{}"
    else:
        arguments_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return (
        f"[tool_call id={tool_call.get('id') or ''} name={name} "
        f"arguments={arguments_text}]"
    )


def _convert_messages(messages: object) -> tuple[str, list[dict[str, object]]]:
    if not isinstance(messages, list):
        raise GeminiProtocolError("messages must be an array")
    system_parts: list[str] = []
    steps: list[dict[str, object]] = []
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
            text = _openai_text(content)
            if text:
                steps.append(
                    {
                        "type": "user_input",
                        "content": [{"type": "text", "text": text}],
                    }
                )
            continue
        if role == "assistant":
            text = _openai_text(content)
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                markers = [
                    _function_call_text(tool_call)
                    for tool_call in tool_calls
                    if isinstance(tool_call, Mapping)
                ]
                if markers:
                    text = (
                        f"{text}\n\n" if text else ""
                    ) + "\n".join(markers)
            if text:
                steps.append(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": text}],
                    }
                )
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            text = _openai_text(content) or "(no tool output)"
            name = str(message.get("name") or "")
            suffix = f" name={name}" if name else ""
            steps.append(
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[tool_result call_id={call_id}{suffix}]\n{text}",
                        }
                    ],
                }
            )
    if not steps:
        raise GeminiProtocolError("messages contain no input")
    return "\n\n".join(system_parts), steps


def _convert_tools(tools: object) -> list[dict[str, object]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = {"type": "object", "properties": {}}
        item: dict[str, object] = {
            "type": "function",
            "name": name,
            "parameters": dict(parameters),
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            item["description"] = description
        converted.append(item)
    return converted


def _convert_tool_choice(value: object) -> object | None:
    if isinstance(value, str):
        return {"auto": "auto", "none": "none", "required": "any"}.get(value)
    if isinstance(value, Mapping):
        function = value.get("function")
        function = function if isinstance(function, Mapping) else {}
        name = str(function.get("name") or "").strip()
        if name:
            return {"allowed_tools": {"mode": "auto", "tools": [name]}}
    return None


def _generation_config(payload: Mapping[str, object]) -> dict[str, object]:
    config: dict[str, object] = {"thinking_summaries": "none"}
    effort = str(payload.get("reasoning_effort") or "").strip().lower()
    if effort in _THINKING_LEVELS:
        config["thinking_level"] = "low" if effort == "minimal" else effort
    else:
        config["thinking_level"] = "medium"
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_completion_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        config["max_output_tokens"] = max_tokens
    return config


def _continuation_result(message: Mapping[str, object]) -> dict[str, object]:
    call_id = str(message.get("tool_call_id") or "")
    text = _openai_text(message.get("content")) or "(no tool output)"
    result: dict[str, object] = {
        "type": "function_result",
        "call_id": call_id,
        "result": text,
    }
    name = str(message.get("name") or "")
    if name:
        result["name"] = name
    return result


def extract_continuation(
    payload: Mapping[str, object],
    store: GeminiToolContinuationStore,
) -> GeminiContinuation | None:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    trailing: list[Mapping[str, object]] = []
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            break
        trailing.append(message)
        index -= 1
    if not trailing:
        return None
    trailing.reverse()
    if index < 0:
        return None
    assistant = messages[index]
    if not isinstance(assistant, Mapping) or assistant.get("role") != "assistant":
        return None
    calls = assistant.get("tool_calls")
    if not isinstance(calls, list):
        return None
    call_ids = {
        str(call.get("id") or "")
        for call in calls
        if isinstance(call, Mapping)
    }
    interaction_ids: set[str] = set()
    results: list[dict[str, object]] = []
    for message in trailing:
        call_id = str(message.get("tool_call_id") or "")
        if not call_id or (call_ids and call_id not in call_ids):
            return None
        interaction_id = store.lookup(call_id)
        if not interaction_id:
            return None
        interaction_ids.add(interaction_id)
        results.append(_continuation_result(message))
    if len(interaction_ids) != 1:
        return None
    return GeminiContinuation(interaction_ids.pop(), tuple(results))


def build_interaction_payload(
    payload: Mapping[str, object],
    upstream_model: str,
    continuation: GeminiContinuation | None = None,
) -> dict[str, object]:
    stream = payload.get("stream") is True
    result: dict[str, object] = {"model": upstream_model}
    tools: list[dict[str, object]] = []
    if continuation is not None:
        result["previous_interaction_id"] = continuation.interaction_id
        result["input"] = list(continuation.results)
        result["store"] = True
    else:
        system_instruction, steps = _convert_messages(payload.get("messages"))
        if system_instruction:
            result["system_instruction"] = system_instruction
        result["input"] = steps
        tools = _convert_tools(payload.get("tools"))
        if tools:
            result["tools"] = tools
            result["store"] = True
            tool_choice = _convert_tool_choice(payload.get("tool_choice"))
            if tool_choice is not None:
                result["tool_choice"] = tool_choice
    if stream:
        result["stream"] = True
    result["generation_config"] = _generation_config(payload)
    result["safety_settings"] = [dict(item) for item in GEMINI_SAFETY_SETTINGS]
    return result


def _usage_to_openai(usage: object) -> dict[str, object] | None:
    if not isinstance(usage, Mapping):
        return None
    def number(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
    prompt = number("total_input_tokens")
    completion = number("total_output_tokens")
    total = number("total_tokens") or prompt + completion
    result: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {
            "cached_tokens": number("total_cached_tokens"),
        },
    }
    reasoning = number("total_thought_tokens")
    if reasoning:
        result["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return result


def _finish_reason(interaction: Mapping[str, object], *, has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    status = str(interaction.get("status") or "")
    if status == "incomplete":
        return "length"
    return "stop"


def _function_call_from_step(step: Mapping[str, object]) -> dict[str, object] | None:
    if step.get("type") != "function_call":
        return None
    name = str(step.get("name") or "")
    if not name:
        return None
    arguments = step.get("arguments")
    if not isinstance(arguments, Mapping):
        arguments = {}
    return {
        "id": str(step.get("id") or ""),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _response_text(interaction: Mapping[str, object]) -> str:
    steps = interaction.get("steps")
    if not isinstance(steps, list):
        return ""
    parts: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return "".join(parts)


def response_from_interaction(
    interaction: Mapping[str, object],
    *,
    model: str,
) -> dict[str, object]:
    steps = interaction.get("steps")
    tool_calls: list[dict[str, object]] = []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            call = _function_call_from_step(step)
            if call is not None:
                tool_calls.append(call)
    message: dict[str, object] = {
        "role": "assistant",
        "content": _response_text(interaction),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    result: dict[str, object] = {
        "id": str(interaction.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(
                    interaction, has_tools=bool(tool_calls)
                ),
            }
        ],
    }
    usage = _usage_to_openai(interaction.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return result


class GeminiStreamTranslator:
    def __init__(self, model: str) -> None:
        self.model = model
        self.created = int(time.time())
        self.interaction_id = ""
        self.status = ""
        self.usage: dict[str, object] | None = None
        self.role_sent = False
        self.text_seen = False
        self.completed = False
        self._tools_by_step: dict[int, dict[str, object]] = {}
        self._tool_order: list[dict[str, object]] = []
        self._seen_call_ids: set[str] = set()

    @property
    def call_ids(self) -> list[str]:
        return [
            str(tool.get("id") or "")
            for tool in self._tool_order
            if tool.get("id")
        ]

    def _chunk(
        self,
        delta: Mapping[str, object],
        *,
        finish_reason: str | None = None,
        include_usage: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.interaction_id or f"chatcmpl-{uuid.uuid4().hex}",
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

    def _content_chunk(self, text: str) -> list[dict[str, object]]:
        if not text:
            return []
        chunks = self._role_chunk()
        self.text_seen = True
        chunks.append(self._chunk({"content": text}))
        return chunks

    def _tool_delta(
        self,
        tool: dict[str, object],
        arguments: str,
    ) -> dict[str, object] | None:
        started = bool(tool.get("started"))
        tool["started"] = True
        function: dict[str, object] = {"arguments": arguments}
        delta: dict[str, object] = {
            "index": int(tool.get("call_index") or 0),
            "function": function,
        }
        if not started:
            call_id = str(tool.get("id") or "")
            if call_id:
                delta["id"] = call_id
                self._seen_call_ids.add(call_id)
            delta["type"] = "function"
            function["name"] = str(tool.get("name") or "")
        return self._chunk({"tool_calls": [delta]})

    def _remember_function_call(self, index: int, step: Mapping[str, object]) -> None:
        call_id = str(step.get("id") or "")
        name = str(step.get("name") or "")
        if not name:
            return
        tool: dict[str, object] = {
            "id": call_id,
            "name": name,
            "call_index": len(self._tool_order),
            "started": False,
            "arguments": "",
        }
        self._tools_by_step[index] = tool
        self._tool_order.append(tool)

    def _ingest_completed_steps(self, steps: object) -> list[dict[str, object]]:
        if not isinstance(steps, list):
            return []
        chunks: list[dict[str, object]] = []
        text = ""
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            if step.get("type") == "model_output":
                content = step.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, Mapping) and item.get("type") == "text":
                            value = item.get("text")
                            if isinstance(value, str):
                                text += value
                continue
            if step.get("type") != "function_call":
                continue
            call_id = str(step.get("id") or "")
            if call_id and call_id in self._seen_call_ids:
                continue
            tool = next(
                (item for item in self._tool_order if item.get("id") == call_id),
                None,
            )
            if tool is None:
                tool = {
                    "id": call_id,
                    "name": str(step.get("name") or ""),
                    "call_index": len(self._tool_order),
                    "started": False,
                    "arguments": "",
                }
                if not tool["name"]:
                    continue
                self._tool_order.append(tool)
            arguments = step.get("arguments")
            if not isinstance(arguments, Mapping):
                arguments = {}
            encoded = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            )
            chunk = self._tool_delta(tool, encoded)
            if chunk is not None:
                chunks.append(chunk)
        if text and not self.text_seen:
            chunks.extend(self._content_chunk(text))
        return chunks

    def feed(self, event: Mapping[str, object]) -> list[dict[str, object]]:
        event_type = str(event.get("event_type") or "")
        if event_type == "error":
            error = event.get("error")
            error = error if isinstance(error, Mapping) else {}
            raise GeminiProtocolError(
                str(error.get("message") or "Gemini interaction failed")
            )
        if event_type == "interaction.created":
            interaction = event.get("interaction")
            if isinstance(interaction, Mapping):
                self.interaction_id = str(interaction.get("id") or "")
            return self._role_chunk()
        if event_type == "step.start":
            step = event.get("step")
            index = event.get("index")
            if isinstance(step, Mapping) and isinstance(index, int):
                if step.get("type") == "function_call":
                    self._remember_function_call(index, step)
            return []
        if event_type == "step.delta":
            delta = event.get("delta")
            index = event.get("index")
            if not isinstance(delta, Mapping) or not isinstance(index, int):
                return []
            kind = delta.get("type")
            if kind == "text":
                text = delta.get("text")
                return self._content_chunk(text if isinstance(text, str) else "")
            if kind == "arguments_delta":
                tool = self._tools_by_step.get(index)
                arguments = delta.get("arguments")
                if tool is None or not isinstance(arguments, str):
                    return []
                tool["arguments"] = str(tool.get("arguments") or "") + arguments
                chunk = self._tool_delta(tool, arguments)
                return [chunk] if chunk is not None else []
            return []
        if event_type == "interaction.status_update":
            usage = event.get("usage")
            self.usage = _usage_to_openai(usage)
            return []
        if event_type == "interaction.completed":
            interaction = event.get("interaction")
            interaction = interaction if isinstance(interaction, Mapping) else {}
            self.interaction_id = str(
                interaction.get("id") or self.interaction_id
            )
            self.status = str(interaction.get("status") or "")
            self.usage = _usage_to_openai(interaction.get("usage"))
            chunks = self._role_chunk()
            chunks.extend(self._ingest_completed_steps(interaction.get("steps")))
            for tool in self._tool_order:
                if tool.get("started"):
                    continue
                arguments = str(tool.get("arguments") or "")
                if not arguments:
                    arguments = "{}"
                chunk = self._tool_delta(tool, arguments)
                if chunk is not None:
                    chunks.append(chunk)
            finish = _finish_reason(
                interaction, has_tools=bool(self._tool_order)
            )
            chunks.append(
                self._chunk(
                    {},
                    finish_reason=finish,
                    include_usage=True,
                )
            )
            self.completed = True
            return chunks
        return []

    def finalize(self) -> list[dict[str, object]]:
        if self.completed:
            return []
        chunks = self._role_chunk()
        chunks.append(
            self._chunk({}, finish_reason="stop", include_usage=True)
        )
        self.completed = True
        return chunks
