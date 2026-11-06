from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine.llm_proxy as llm_proxy_module
from engine.aki_config import atomic_write, load_config, set_key, template_text
from engine.aki_config import ModelRoute
from engine.gemini_interactions import (
    GeminiStreamTranslator,
    GeminiToolContinuationStore,
    build_interaction_payload,
    extract_continuation,
)
from engine.llm_proxy import LlmProxy


class GeminiConfigTests(unittest.TestCase):
    def test_api_key_file_and_interactions_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key_file = root / "gemini-key"
            key_file.write_text("gemini-secret\n", encoding="utf-8")
            conf = root / "aki.conf"
            atomic_write(conf, template_text())
            set_key(conf, "proxy", "token", "loopback-token")
            set_key(
                conf,
                "upstream.gemini",
                "base_url",
                "https://generativelanguage.googleapis.com/v1beta",
            )
            set_key(conf, "upstream.gemini", "api_key", "")
            set_key(conf, "upstream.gemini", "api_key_file", str(key_file))
            set_key(conf, "upstream.gemini", "protocol", "gemini-interactions")
            set_key(conf, "model.gemini-3.8-flash", "upstream", "gemini")
            set_key(conf, "model.gemini-3.8-flash", "path", "/interactions")
            set_key(conf, "model.gemini-3.8-flash", "input", "text")
            config = load_config(conf)
            route = next(
                item
                for item in config.enabled_models
                if item.model_id == "gemini-3.8-flash"
            )
            self.assertEqual(route.upstream_protocol, "gemini-interactions")
            self.assertEqual(route.api, "openai-completions")
            self.assertEqual(route.api_key, "gemini-secret")
            self.assertEqual(
                route.url,
                "https://generativelanguage.googleapis.com/v1beta/interactions",
            )
            self.assertNotIn("gemini-secret", conf.read_text(encoding="utf-8"))


class GeminiPayloadTests(unittest.TestCase):
    def test_payload_uses_interactions_schema_and_text_only(self) -> None:
        payload = {
            "model": "gemini-3.8-flash",
            "stream": True,
            "reasoning_effort": "high",
            "max_tokens": 1234,
            "messages": [
                {"role": "system", "content": "system rule"},
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [
                        {
                            "id": "call_old",
                            "type": "function",
                            "function": {
                                "name": "Read",
                                "arguments": '{"path":"a"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_old",
                    "name": "Read",
                    "content": "contents",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "next"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AA=="},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
        }
        body = build_interaction_payload(payload, "gemini-3.8-flash")
        self.assertEqual(body["system_instruction"], "system rule")
        self.assertTrue(body["stream"])
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "Read")
        self.assertEqual(body["generation_config"]["thinking_level"], "high")
        self.assertEqual(body["generation_config"]["max_output_tokens"], 1234)
        self.assertTrue(all("type" in item for item in body["safety_settings"]))
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertIn("[image omitted: Gemini text-only route]", serialized)
        self.assertIn("[tool_result call_id=call_old", serialized)
        self.assertNotIn('"category"', serialized)

    def test_tool_continuation_uses_previous_interaction(self) -> None:
        store = GeminiToolContinuationStore()
        store.remember("interaction-1", ["call_1"])
        payload = {
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "get_weather",
                    "content": "sunny",
                },
            ]
        }
        continuation = extract_continuation(payload, store)
        self.assertIsNotNone(continuation)
        assert continuation is not None
        body = build_interaction_payload(
            payload,
            "gemini-3.8-flash",
            continuation,
        )
        self.assertEqual(body["previous_interaction_id"], "interaction-1")
        self.assertEqual(body["input"][0]["type"], "function_result")
        self.assertEqual(body["input"][0]["call_id"], "call_1")
        self.assertEqual(body["input"][0]["result"], "sunny")


class GeminiStreamTranslatorTests(unittest.TestCase):
    def test_text_events_become_openai_chunks(self) -> None:
        translator = GeminiStreamTranslator("gemini-3.8-flash")
        chunks = translator.feed(
            {
                "event_type": "interaction.created",
                "interaction": {"id": "interaction-1", "status": "in_progress"},
            }
        )
        chunks.extend(
            translator.feed(
                {
                    "event_type": "step.delta",
                    "index": 0,
                    "delta": {"type": "text", "text": "hello"},
                }
            )
        )
        chunks.extend(
            translator.feed(
                {
                    "event_type": "interaction.completed",
                    "interaction": {
                        "id": "interaction-1",
                        "status": "completed",
                        "usage": {
                            "total_input_tokens": 2,
                            "total_output_tokens": 1,
                            "total_tokens": 3,
                        },
                    },
                }
            )
        )
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "hello")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 3)

    def test_function_call_events_become_tool_calls(self) -> None:
        translator = GeminiStreamTranslator("gemini-3.8-flash")
        translator.feed(
            {
                "event_type": "interaction.created",
                "interaction": {"id": "interaction-2", "status": "in_progress"},
            }
        )
        translator.feed(
            {
                "event_type": "step.start",
                "index": 0,
                "step": {
                    "type": "function_call",
                    "id": "call_2",
                    "name": "Read",
                    "arguments": {},
                },
            }
        )
        chunks = translator.feed(
            {
                "event_type": "step.delta",
                "index": 0,
                "delta": {
                    "type": "arguments_delta",
                    "arguments": '{"path":"a"}',
                },
            }
        )
        chunks.extend(
            translator.feed(
                {
                    "event_type": "interaction.completed",
                    "interaction": {"id": "interaction-2", "status": "requires_action"},
                }
            )
        )
        tool_chunk = next(
            chunk
            for chunk in chunks
            if chunk.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
        )
        call = tool_chunk["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["id"], "call_2")
        self.assertEqual(call["function"]["name"], "Read")
        self.assertEqual(call["function"]["arguments"], '{"path":"a"}')
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(translator.call_ids, ["call_2"])


class GeminiProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.captured: dict[str, object] = {}

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            headers: dict[str, str] = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["headers"] = headers
            self.captured["body"] = json.loads(rest[:length])
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            events = [
                {
                    "event_type": "interaction.created",
                    "interaction": {"id": "interaction-3", "status": "in_progress"},
                },
                {
                    "event_type": "step.delta",
                    "index": 0,
                    "delta": {"type": "text", "text": "hello"},
                },
                {
                    "event_type": "interaction.completed",
                    "interaction": {
                        "id": "interaction-3",
                        "status": "completed",
                        "usage": {"total_tokens": 3},
                    },
                },
            ]
            for event in events:
                chunk = (
                    f"event: {event['event_type']}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                ).encode("utf-8")
                writer.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = int(self.upstream.sockets[0].getsockname()[1])
        self.route = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="gemini",
            upstream_model="gemini-3.8-flash",
            api="openai-completions",
            path="/interactions",
            context_window=1_000_000,
            max_tokens=65_536,
            inputs=("text",),
            url=f"http://127.0.0.1:{port}/interactions",
            api_key="gemini-secret",
            use_proxy=False,
            upstream_protocol="gemini-interactions",
        )
        self.proxy = LlmProxy("127.0.0.1", 0, "loopback-token", (self.route,))
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_streaming_proxy_translates_gemini_sse(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        body = json.dumps(
            {
                "model": "gemini-3.8-flash",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode("utf-8")
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"hello", response)
        self.assertIn(b"[DONE]", response)
        headers = self.captured["headers"]
        self.assertEqual(headers["x-goog-api-key"], "gemini-secret")
        self.assertEqual(headers["api-revision"], "2026-05-20")
        captured_body = self.captured["body"]
        self.assertEqual(
            captured_body["safety_settings"][0]["type"], "harassment"
        )
        self.assertEqual(captured_body["safety_settings"][0]["threshold"], "off")
        self.assertEqual(captured_body["input"][0]["type"], "user_input")

    async def test_gemini_route_uses_akid_proxy_priority_list(self) -> None:
        route = ModelRoute(
            **{**self.route.__dict__, "use_proxy": True}
        )
        proxy_priority = (
            "http://192.168.50.100:7890;http://192.168.4.1:7890"
        )
        proxy = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (route,),
            upstream_proxy=proxy_priority,
        )
        await proxy.start()
        seen: dict[str, str] = {}
        original = llm_proxy_module._open_upstream

        async def capture(
            host,
            port,
            scheme,
            path,
            *,
            upstream_proxy,
            ssl_context,
        ):
            seen["upstream_proxy"] = upstream_proxy
            return await original(
                host,
                port,
                scheme,
                path,
                upstream_proxy="",
                ssl_context=ssl_context,
            )

        try:
            with mock.patch.object(
                llm_proxy_module, "_open_upstream", side_effect=capture
            ):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy.port
                )
                body = json.dumps(
                    {
                        "model": "gemini-3.8-flash",
                        "stream": True,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode("utf-8")
                writer.write(
                    (
                        "POST /v1/chat/completions HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{proxy.port}\r\n"
                        "Authorization: Bearer loopback-token\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                    ).encode("ascii")
                    + body
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), timeout=2)
                writer.close()
                await writer.wait_closed()
        finally:
            await proxy.stop()
        self.assertIn(b"hello", response)
        self.assertEqual(seen["upstream_proxy"], proxy_priority)


if __name__ == "__main__":
    unittest.main()
