#!/usr/bin/env python3
"""Deterministic Letta stream-json stand-in used by the gateway tests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

WRITE_LOCK = threading.Lock()


def emit(message: dict) -> None:
    message.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with WRITE_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def emit_chunk(chunk: dict) -> None:
    emit({"type": "stream_event", "event": chunk})


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


class FakeLetta:
    def __init__(self) -> None:
        self.pending: dict[str, tuple[threading.Event, dict]] = {}
        self.pending_lock = threading.Lock()
        self.interrupt = threading.Event()
        self.turn_number = 0
        self.turn_thread: threading.Thread | None = None
        self.registered_tools: list[str] = []

    def wait_for_response(self, request_id: str, timeout: float = 15.0) -> dict | None:
        waiter = threading.Event()
        box: dict = {}
        with self.pending_lock:
            self.pending[request_id] = (waiter, box)
        waiter.wait(timeout)
        with self.pending_lock:
            self.pending.pop(request_id, None)
        return box.get("response")

    def resolve(self, request_id: str, response: dict) -> None:
        with self.pending_lock:
            entry = self.pending.get(request_id)
        if entry is not None:
            entry[1]["response"] = response
            entry[0].set()

    def request_tool(self, name: str, tool_input: dict, tool_call_id: str) -> dict | None:
        request_id = f"perm-{tool_call_id}"
        emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": name,
                    "input": tool_input,
                    "tool_call_id": tool_call_id,
                },
            }
        )
        return self.wait_for_response(request_id)

    def run_turn(self, content: str) -> None:
        self.turn_number += 1
        number = self.turn_number
        is_channel = content.startswith("[akid MQTT channel message]")
        if "EXTERNAL_IMAGE:" in content:
            marker = "EXTERNAL_IMAGE:"
            path_value = content.split(marker, 1)[1].splitlines()[0].strip()
            tool_call_id = f"image-tool-{number}"
            self.request_tool(
                "tokifactor_send_image",
                {"path": path_value, "caption": "fake caption"},
                tool_call_id,
            )
            emit(
                {
                    "type": "control_request",
                    "request_id": f"ext-{tool_call_id}",
                    "request": {
                        "subtype": "execute_external_tool",
                        "tool_call_id": tool_call_id,
                        "tool_name": "tokifactor_send_image",
                        "input": {"path": path_value, "caption": "fake caption"},
                    },
                }
            )
            response = self.wait_for_response(f"ext-{tool_call_id}") or {}
            parts = response.get("content") or []
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            emitted = f"external tool result: {text}"
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": emitted}]}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": emitted,
                }
            )
            return
        if "EXTERNAL_CAMERA" in content:
            tool_call_id = f"camera-tool-{number}"
            self.request_tool(
                "camera_take_photo",
                {"device": "/dev/video0", "width": 320, "height": 240},
                tool_call_id,
            )
            emit(
                {
                    "type": "control_request",
                    "request_id": f"ext-{tool_call_id}",
                    "request": {
                        "subtype": "execute_external_tool",
                        "tool_call_id": tool_call_id,
                        "tool_name": "camera_take_photo",
                        "input": {"device": "/dev/video0", "width": 320, "height": 240},
                    },
                }
            )
            response = self.wait_for_response(f"ext-{tool_call_id}") or {}
            parts = response.get("content") or []
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            emitted = f"external tool result: {text}"
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": emitted}]}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": emitted,
                }
            )
            return
        if "EXTERNAL_SCREENSHOT" in content:
            tool_call_id = f"screenshot-tool-{number}"
            self.request_tool(
                "desktop_take_screenshot",
                {"include_cursor": False},
                tool_call_id,
            )
            emit(
                {
                    "type": "control_request",
                    "request_id": f"ext-{tool_call_id}",
                    "request": {
                        "subtype": "execute_external_tool",
                        "tool_call_id": tool_call_id,
                        "tool_name": "desktop_take_screenshot",
                        "input": {"include_cursor": False},
                    },
                }
            )
            response = self.wait_for_response(f"ext-{tool_call_id}") or {}
            parts = response.get("content") or []
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
            )
            emitted = f"external tool result: {text}"
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": emitted}]}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": emitted,
                }
            )
            return
        if is_channel:
            if "dangerous" in content:
                emit_chunk(
                    {"message_type": "assistant_message", "content": [{"type": "text", "text": "checking channel tool\n"}]}
                )
                response = self.request_tool("Bash", {"command": "echo channel-unsafe"}, f"tool-ch-{number}")
                body = (response or {}).get("response") or {}
                allowed = body.get("behavior") == "allow"
                emit(
                    {
                        "type": "message",
                        "message_type": "tool_return_message",
                        "tool_call_id": f"tool-ch-{number}",
                        "status": "success" if allowed else "error",
                        "tool_return": "tool output" if allowed else body.get("message", "denied"),
                    }
                )
                text = "channel tool allowed" if allowed else "channel tool denied"
                emit_chunk(
                    {"message_type": "assistant_message", "content": [{"type": "text", "text": text}]}
                )
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "stop_reason": "end_turn",
                        "result": text,
                    }
                )
                return
            text = "[NO_REPLY]" if "no-reply" in content else "channel reply"
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": text}]}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": text,
                }
            )
            return
        if content == "crash":
            os._exit(7)
        if content == "slow":
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": "working"}]}
            )
            if self.interrupt.wait(20.0):
                emit(
                    {
                        "type": "result",
                        "subtype": "success",
                        "stop_reason": "cancelled",
                        "result": "cancelled",
                    }
                )
            else:
                emit_chunk(
                    {"message_type": "assistant_message", "content": [{"type": "text", "text": "done"}]}
                )
                emit({"type": "result", "subtype": "success", "stop_reason": "end_turn", "result": "done"})
            return
        if content in ("dangerous", "timeout", "safe"):
            tool_name = "Read" if content == "safe" else "Bash"
            tool_input = {"path": "MEMORY.md"} if content == "safe" else {"command": "echo unsafe"}
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": "checking tool\n"}]}
            )
            response = self.request_tool(tool_name, tool_input, f"tool-{number}")
            body = (response or {}).get("response") or {}
            allowed = body.get("behavior") == "allow"
            emit(
                {
                    "type": "message",
                    "message_type": "tool_return_message",
                    "tool_call_id": f"tool-{number}",
                    "status": "success" if allowed else "error",
                    "tool_return": "tool output" if allowed else body.get("message", "denied"),
                }
            )
            text = "tool allowed" if allowed else "tool denied"
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": text}]}
            )
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": text,
                }
            )
            return
        if content == "compact":
            emit_chunk(
                {"message_type": "assistant_message", "content": [{"type": "text", "text": "compacting\n"}]}
            )
            emit_chunk(
                {
                    "message_type": "event_message",
                    "event_type": "compaction",
                    "event_data": {"trigger": "context_pressure"},
                }
            )
            emit_chunk({"message_type": "summary_message", "summary": "older context condensed"})
            emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "result": "compacted",
                }
            )
            return

        emit_chunk(
            {"message_type": "assistant_message", "content": [{"type": "text", "text": "echo: "}]}
        )
        emit_chunk(
            {"message_type": "assistant_message", "content": [{"type": "text", "text": content + "\n"}]}
        )
        emit_chunk(
            {
                "message_type": "usage_statistics",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "context_tokens": 100,
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "stop_reason": "end_turn",
                "result": "echo: " + content,
                "usage": {"total_tokens": 15},
            }
        )

    def handle(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "user":
            content = content_text((message.get("message") or {}).get("content"))
            thread = threading.Thread(target=self.run_turn, args=(content,), daemon=True)
            self.turn_thread = thread
            thread.start()
            return
        if kind == "control_request":
            request = message.get("request") or {}
            subtype = request.get("subtype")
            request_id = str(message.get("request_id") or "")
            if subtype == "interrupt":
                self.interrupt.set()
                emit(
                    {
                        "type": "control_response",
                        "response": {"subtype": "success", "request_id": request_id},
                    }
                )
            elif subtype == "list_messages":
                messages = [
                    {
                        "id": "m1",
                        "message_type": "assistant_message",
                        "content": [{"type": "text", "text": "history item"}],
                    }
                ]
                image_bytes = int(os.environ.get("FAKE_LETTA_HISTORY_IMAGE_BYTES", "0"))
                if image_bytes > 0:
                    messages.insert(
                        0,
                        {
                            "id": "m-image",
                            "message_type": "tool_return_message",
                            "tool_call_id": "call-image",
                            "status": "success",
                            "tool_return": [
                                {"type": "text", "text": "[Image: huge.png]"},
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "A" * image_bytes,
                                    },
                                },
                            ],
                        },
                    )
                emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": {
                                "messages": messages,
                                "next_before": "m1",
                                "has_more": False,
                            },
                        },
                    }
                )
            elif subtype == "register_external_tools":
                tools = request.get("tools") or []
                self.registered_tools = [
                    str(tool.get("name") or "unknown")
                    for tool in tools
                    if isinstance(tool, dict)
                ]
                emit(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": {"registered": len(tools)},
                        },
                    }
                )
            return
        if kind == "control_response":
            response = message.get("response") or {}
            request_id = str(response.get("request_id") or "")
            self.resolve(request_id, response)


def main() -> int:
    fake = FakeLetta()
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "fake-session",
            "agent_id": "fake-agent",
            "conversation_id": "default",
            "model": "fake/model",
            "tools": [],
        }
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        fake.handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
