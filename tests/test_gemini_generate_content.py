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
from engine.gemini_generate_content import (
    GenerateContentStreamTranslator,
    GeminiProtocolError,
    build_generate_content_payload,
    response_from_generate_content,
)
from engine.letta_llm import _compat
from engine.llm_proxy import LlmProxy


class GenerateContentConfigTests(unittest.TestCase):
    def test_api_key_file_and_generate_content_protocol(self) -> None:
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
            set_key(
                conf,
                "upstream.gemini",
                "protocol",
                "gemini-generate-content",
            )
            set_key(conf, "model.gemini-3.8-flash", "upstream", "gemini")
            set_key(
                conf,
                "model.gemini-3.8-flash",
                "path",
                "/models/{model}:generateContent",
            )
            set_key(
                conf,
                "model.gemini-3.8-flash",
                "upstream_model",
                "gemini-3.8-flash",
            )
            set_key(
                conf,
                "model.gemini-2.5-flash",
                "path",
                "/models/{model}:generateContent",
            )
            set_key(conf, "model.gemini-3.8-flash", "input", "text")
            config = load_config(conf)
            route = next(
                item
                for item in config.enabled_models
                if item.model_id == "gemini-3.8-flash"
            )
            self.assertEqual(
                route.upstream_protocol, "gemini-generate-content"
            )
            self.assertEqual(route.api, "openai-completions")
            self.assertEqual(route.api_key, "gemini-secret")
            self.assertEqual(
                route.path, "/models/gemini-3.8-flash:generateContent"
            )
            self.assertEqual(
                route.url,
                "https://generativelanguage.googleapis.com/v1beta/"
                "models/gemini-3.8-flash:generateContent",
            )
            self.assertNotIn("gemini-secret", conf.read_text(encoding="utf-8"))


class GenerateContentPayloadTests(unittest.TestCase):
    def test_payload_uses_native_contents_and_tools(self) -> None:
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
                    "reasoning_details": [
                        {
                            "type": "reasoning.encrypted",
                            "id": "call_old",
                            "data": "signature-old",
                        }
                    ],
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
        body = build_generate_content_payload(payload, "gemini-3.8-flash")
        self.assertEqual(
            body["systemInstruction"], {"parts": [{"text": "system rule"}]}
        )
        contents = body["contents"]
        self.assertEqual([item["role"] for item in contents], ["user", "model", "user", "user"])
        function_call = contents[1]["parts"][1]["functionCall"]
        self.assertEqual(function_call["name"], "Read")
        self.assertEqual(function_call["id"], "call_old")
        self.assertEqual(function_call["args"], {"path": "a"})
        self.assertEqual(
            contents[1]["parts"][1]["thoughtSignature"], "signature-old"
        )
        function_response = contents[2]["parts"][0]["functionResponse"]
        self.assertEqual(function_response["name"], "Read")
        self.assertEqual(function_response["id"], "call_old")
        self.assertEqual(function_response["response"], {"output": "contents"})
        self.assertEqual(
            body["tools"][0]["functionDeclarations"][0]["name"], "Read"
        )
        self.assertEqual(
            body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "HIGH"
        )
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 1234)
        self.assertEqual(
            body["safetySettings"][0],
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
        )
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertIn("[image omitted: Gemini text-only route]", serialized)
        self.assertNotIn("[tool_call", serialized)
        self.assertNotIn("previous_interaction_id", serialized)
        self.assertNotIn("function_result", serialized)

    def test_clean_gemini_schema_normalizes_tools(self) -> None:
        payload = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "SendAgentMessage",
                        "description": "Send a message",
                        "parameters": {
                            "$schema": "http://json-schema.org/draft-07/schema#",
                            "additionalProperties": False,
                            "type": "object",
                            "properties": {
                                "message": {"type": "string"},
                                "computer": {
                                    "type": ["string", "null"],
                                    "description": "target computer",
                                },
                                "width": {
                                    "type": "integer",
                                    "minimum": 64,
                                    "maximum": 4096,
                                    "multipleOf": 8,
                                },
                                "metadata": {
                                    "type": "object",
                                    "propertyNames": {"type": "string"},
                                    "additionalProperties": False,
                                    "properties": {
                                        "mode": {"const": "sync"},
                                    },
                                },
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {"name": {"type": "string"}},
                                    },
                                },
                            },
                            "required": ["message"],
                        },
                    },
                }
            ],
        }
        body = build_generate_content_payload(payload, "gemini-3.8-flash")
        decl = body["tools"][0]["functionDeclarations"][0]
        params = decl["parameters"]
        self.assertNotIn("$schema", params)
        self.assertNotIn("additionalProperties", params)
        props = params["properties"]
        self.assertEqual(props["computer"]["type"], "string")
        self.assertTrue(props["computer"]["nullable"])
        self.assertNotIn("multipleOf", props["width"])
        self.assertEqual(props["width"]["minimum"], 64)
        meta = props["metadata"]
        self.assertNotIn("propertyNames", meta)
        self.assertNotIn("additionalProperties", meta)
        self.assertEqual(meta["properties"]["mode"]["enum"], ["sync"])
        item_schema = props["items"]["items"]
        self.assertNotIn("additionalProperties", item_schema)
        self.assertEqual(item_schema["properties"]["name"]["type"], "string")

    def test_tool_result_name_is_derived_from_assistant_call(self) -> None:
        payload = {
            "messages": [
                {"role": "user", "content": "wait"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "wait_other",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "done",
                },
            ]
        }
        body = build_generate_content_payload(payload, "gemini-3.8-flash")
        response = body["contents"][2]["parts"][0]["functionResponse"]
        self.assertEqual(response["name"], "wait_other")

    def test_unresolvable_tool_result_name_is_rejected(self) -> None:
        payload = {
            "messages": [
                {"role": "user", "content": "wait"},
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "done",
                },
            ]
        }
        with self.assertRaises(GeminiProtocolError):
            build_generate_content_payload(payload, "gemini-3.8-flash")

    def test_gemini_compat_requires_tool_result_names(self) -> None:
        route = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="gemini",
            upstream_model="gemini-3.8-flash",
            api="openai-completions",
            path="/models/gemini-3.8-flash:generateContent",
            context_window=1_000_000,
            max_tokens=65_536,
            inputs=("text",),
            url="https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3.8-flash:generateContent",
            api_key="secret",
            upstream_protocol="gemini-generate-content",
        )
        compat = _compat(route)
        self.assertIsNotNone(compat)
        assert compat is not None
        self.assertIs(compat["requiresToolResultName"], True)

    def test_non_stream_response_maps_tools_and_cache_usage(self) -> None:
        response = {
            "responseId": "response-1",
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_1",
                                    "name": "wait_user",
                                    "args": {"timeout_s": 15},
                                },
                                "thoughtSignature": "signature-1",
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 40,
                "candidatesTokenCount": 5,
                "totalTokenCount": 105,
            },
        }
        mapped = response_from_generate_content(
            response, model="gemini-3.8-flash"
        )
        choice = mapped["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "wait_user")
        self.assertEqual(call["function"]["arguments"], '{"timeout_s":15}')
        self.assertEqual(
            choice["message"]["reasoning_details"],
            [
                {
                    "type": "reasoning.encrypted",
                    "id": "call_1",
                    "data": "signature-1",
                }
            ],
        )
        self.assertNotIn("thoughtSignature", call)
        self.assertEqual(
            mapped["usage"]["prompt_tokens_details"]["cached_tokens"], 40
        )


class GenerateContentStreamTranslatorTests(unittest.TestCase):
    def test_text_stream(self) -> None:
        translator = GenerateContentStreamTranslator("gemini-3.8-flash")
        chunks = translator.feed(
            {
                "responseId": "response-1",
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "hello"}],
                        }
                    }
                ],
            }
        )
        chunks.extend(
            translator.feed(
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": " world"}],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 2,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 4,
                    },
                }
            )
        )
        chunks.extend(translator.finalize())
        content = "".join(
            chunk["choices"][0]["delta"].get("content", "")
            for chunk in chunks
        )
        self.assertEqual(content, "hello world")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 4)

    def test_tool_call_stream(self) -> None:
        translator = GenerateContentStreamTranslator("gemini-3.8-flash")
        translator.feed(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "wait_user",
                                        "args": {"timeout_s": 15},
                                    },
                                    "thoughtSignature": "signature-1",
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        )
        chunks = translator.finalize()
        tool_chunk = next(
            chunk
            for chunk in chunks
            if chunk.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
        )
        call = tool_chunk["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "wait_user")
        self.assertEqual(call["function"]["arguments"], '{"timeout_s":15}')
        reasoning_chunk = next(
            chunk
            for chunk in chunks
            if chunk.get("choices", [{}])[0].get("delta", {}).get(
                "reasoning_details"
            )
        )
        self.assertEqual(
            reasoning_chunk["choices"][0]["delta"]["reasoning_details"][0][
                "data"
            ],
            "signature-1",
        )
        self.assertNotIn("thoughtSignature", call)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")


class GenerateContentProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
            self.captured["request_line"] = head.split(b"\r\n", 1)[0].decode()
            self.captured["headers"] = headers
            self.captured["body"] = json.loads(rest[:length])
            events = [
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "hello"}],
                            }
                        }
                    ]
                },
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": " world"}],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"totalTokenCount": 4},
                },
            ]
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            for event in events:
                chunk = (
                    f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
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
            path="/models/gemini-3.8-flash:generateContent",
            context_window=1_000_000,
            max_tokens=65_536,
            inputs=("text",),
            url=f"http://127.0.0.1:{port}/models/gemini-3.8-flash:generateContent",
            api_key="gemini-secret",
            use_proxy=False,
            upstream_protocol="gemini-generate-content",
        )
        self.proxy = LlmProxy("127.0.0.1", 0, "loopback-token", (self.route,))
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_streaming_proxy_translates_generate_content_sse(self) -> None:
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
        self.assertIn(b" world", response)
        self.assertIn(b"[DONE]", response)
        headers = self.captured["headers"]
        self.assertEqual(headers["x-goog-api-key"], "gemini-secret")
        self.assertIn(
            ":streamGenerateContent?alt=sse", self.captured["request_line"]
        )
        captured_body = self.captured["body"]
        self.assertEqual(captured_body["contents"][0]["role"], "user")
        self.assertEqual(
            captured_body["safetySettings"][0]["category"],
            "HARM_CATEGORY_HARASSMENT",
        )

    async def test_generate_content_uses_akid_proxy_priority_list(self) -> None:
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
