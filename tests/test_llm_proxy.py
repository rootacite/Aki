from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.aki_config import ModelRoute
from engine.grok_oauth import GrokCredentials
from engine.llm_proxy import (
    LlmProxy,
    ProxyBindError,
    _open_upstream,
    continuation_payload,
    splice_unclosed_think,
    unclosed_think_reasoning,
)
from engine.upstream_proxy import select_upstream_proxy


def _route(model: str, upstream: str, url_path: str) -> ModelRoute:
    return ModelRoute(
        model_id=model,
        upstream_name="fake",
        upstream_model=upstream,
        api="openai-completions",
        path="/chat/completions",
        context_window=1000,
        max_tokens=100,
        inputs=("text",),
        url="",
        api_key="upstream-secret",
    )


class LlmProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.upstream_seen = asyncio.Event()
        self.release_upstream = asyncio.Event()
        self.captured: dict[str, object] = {}
        self.clients: list[asyncio.StreamWriter] = []

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["authorization"] = headers.get("authorization")
            self.captured["body"] = json.loads(rest[:length])
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            first = b"4\r\ndata\r\n"
            writer.write(first)
            await writer.drain()
            self.upstream_seen.set()
            await self.release_upstream.wait()
            writer.write(b"5\r\nmore!\r\n0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        route = _route("local-model", "upstream-model", "/chat/completions")
        route = ModelRoute(
            **{**route.__dict__, "url": f"http://127.0.0.1:{port}/chat/completions"}
        )
        self.proxy = LlmProxy("127.0.0.1", 0, "loopback-token", (route,))
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        self.release_upstream.set()
        for writer in self.clients:
            writer.close()
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def _request(self, token: str, model: str) -> tuple[bytes, asyncio.StreamReader]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        body = json.dumps({"model": model, "messages": []}).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                f"Authorization: Bearer {token}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        self.clients.append(writer)
        return b"", reader

    async def test_rewrites_model_and_authorization_and_streams(self) -> None:
        _head, reader = await self._request("loopback-token", "local-model")
        data = b""
        while b"data" not in data:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=2)
            self.assertTrue(chunk)
            data += chunk
        self.assertFalse(self.release_upstream.is_set())
        self.release_upstream.set()
        rest = await asyncio.wait_for(reader.read(4096), timeout=2)
        self.assertIn(b"more!", data + rest)
        self.assertEqual(self.captured["authorization"], "Bearer upstream-secret")
        self.assertEqual(self.captured["body"]["model"], "upstream-model")

    async def test_rejects_bad_token_and_unknown_model(self) -> None:
        _, reader = await self._request("nope", "local-model")
        payload = await asyncio.wait_for(reader.read(4096), timeout=2)
        self.assertIn(b"401", payload)
        _, reader = await self._request("loopback-token", "missing")
        payload = await asyncio.wait_for(reader.read(4096), timeout=2)
        self.assertIn(b"404", payload)

    async def test_streams_debug_request_and_response_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            route = next(iter(self.proxy.routes.values()))
            socket_path = Path(directory) / "debug.sock"
            proxy = LlmProxy(
                "127.0.0.1",
                0,
                "loopback-token",
                (route,),
                profile="haru",
                debug_socket=socket_path,
            )
            await proxy.start()
            writer = None
            debug_writer = None
            try:
                debug_reader, debug_writer = await asyncio.open_unix_connection(
                    str(socket_path)
                )
                hello = json.loads(
                    await asyncio.wait_for(debug_reader.readline(), timeout=2)
                )
                self.assertEqual(hello["event"], "hello")
                self.assertEqual(hello["profile"], "haru")
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy.port
                )
                body = json.dumps(
                    {
                        "model": "local-model",
                        "messages": [{"role": "user", "content": "hello debug"}],
                        "tools": [
                            {"type": "function", "function": {"name": "Grep"}},
                            {"type": "function", "function": {"name": "Glob"}},
                            {"type": "function", "function": {"name": "LS"}},
                        ],
                    }
                ).encode()
                writer.write(
                    (
                        "POST /v1/chat/completions HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{proxy.port}\r\n"
                        "Authorization: Bearer loopback-token\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
                response = b""
                while b"data" not in response:
                    response += await asyncio.wait_for(reader.read(4096), timeout=2)
                self.release_upstream.set()
                await asyncio.wait_for(reader.read(4096), timeout=2)

                events = []
                while True:
                    line = await asyncio.wait_for(debug_reader.readline(), timeout=2)
                    self.assertTrue(line)
                    event = json.loads(line)
                    events.append(event)
                    if event["event"] == "response_end":
                        break
                request = next(event for event in events if event["event"] == "request")
                self.assertEqual(request["profile"], "haru")
                self.assertEqual(request["requested_model"], "local-model")
                self.assertEqual(request["upstream_model"], "upstream-model")
                self.assertEqual(request["payload"]["model"], "upstream-model")
                self.assertEqual(
                    request["payload"]["messages"][0]["content"], "hello debug"
                )
                self.assertEqual(request["tool_names"], ["Glob", "Grep", "LS"])
                response_start = next(
                    event for event in events if event["event"] == "response_start"
                )
                self.assertEqual(response_start["status"], 200)
                chunks = "".join(
                    event["data"]
                    for event in events
                    if event["event"] == "response_chunk"
                )
                self.assertEqual(chunks, "datamore!")
                response_end = events[-1]
                self.assertEqual(response_end["status"], 200)
                self.assertEqual(response_end["bytes"], 9)
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(socket_path.parent.stat().st_mode & 0o777, 0o700)
            finally:
                if debug_writer is not None:
                    debug_writer.close()
                    await debug_writer.wait_closed()
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                await proxy.stop()

    def test_refuses_non_loopback_bind(self) -> None:
        with self.assertRaises(ProxyBindError):
            LlmProxy("0.0.0.0", 7721, "token", ())

    async def test_retries_transient_upstream_proxy_tls_eof(self) -> None:
        calls = 0
        expected = (mock.sentinel.reader, mock.sentinel.writer, "/v1/responses")

        async def open_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionResetError()
            return expected

        with mock.patch(
            "engine.llm_proxy._open_upstream_once", side_effect=open_once
        ), mock.patch("engine.llm_proxy.asyncio.sleep", new=mock.AsyncMock()):
            result = await _open_upstream(
                "cli-chat-proxy.grok.com",
                443,
                "https",
                "/v1/responses",
                upstream_proxy="http://192.168.4.1:7890",
                ssl_context=None,
            )
        self.assertEqual(calls, 3)
        self.assertIs(result, expected)

    async def test_does_not_retry_permanent_proxy_rejection(self) -> None:
        calls = 0

        async def open_once(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise OSError("upstream proxy refused CONNECT (407)")

        with mock.patch(
            "engine.llm_proxy._open_upstream_once", side_effect=open_once
        ), mock.patch("engine.llm_proxy.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(OSError):
                await _open_upstream(
                    "cli-chat-proxy.grok.com",
                    443,
                    "https",
                    "/v1/responses",
                    upstream_proxy="http://192.168.4.1:7890",
                    ssl_context=None,
                )
        self.assertEqual(calls, 1)


class LocalBypassProxyTests(unittest.IsolatedAsyncioTestCase):
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
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["body"] = json.loads(rest[:length])
            payload = b'{"choices":[{"message":{"content":"OK"}}]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        route = ModelRoute(
            model_id="local-model",
            upstream_name="local-llama",
            upstream_model="gemma.gguf",
            api="openai-completions",
            path="/chat/completions",
            context_window=256_000,
            max_tokens=32_768,
            inputs=("text",),
            url=f"http://127.0.0.1:{port}/chat/completions",
            api_key="local",
            use_proxy=False,
        )
        self.proxy = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (route,),
            upstream_proxy="http://127.0.0.1:1",
        )
        await self.proxy.start()
        self.client: asyncio.StreamWriter | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            self.client.close()
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_use_proxy_false_reaches_loopback_directly(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.client = writer
        body = json.dumps({"model": "local-model", "messages": []}).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(4096), timeout=2)
        self.assertIn(b"200 OK", payload)
        self.assertEqual(self.captured["body"]["model"], "gemma.gguf")


class ExtraBodyProxyTests(unittest.IsolatedAsyncioTestCase):
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
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["body"] = json.loads(rest[:length])
            self.captured["authorization"] = headers.get("authorization")
            payload = b'{"choices":[{"message":{"content":"OK"}}]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        route = ModelRoute(
            model_id="minimax",
            upstream_name="minimax",
            upstream_model="MiniMax-M2.7",
            api="openai-completions",
            path="/chat/completions",
            context_window=204800,
            max_tokens=65536,
            inputs=("text",),
            url=f"http://127.0.0.1:{port}/chat/completions",
            api_key="upstream-secret",
            extra_body={"reasoning_split": True, "thinking": {"type": "adaptive"}},
        )
        self.proxy = LlmProxy("127.0.0.1", 0, "loopback-token", (route,))
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_merges_extra_body_after_model_rewrite(self) -> None:
        _reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        body = json.dumps({"model": "minimax", "messages": [{"role": "user", "content": "hi"}]}).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        payload = await asyncio.wait_for(_reader.read(4096), timeout=2)
        writer.close()
        self.assertIn(b"200 OK", payload)
        captured = self.captured["body"]
        self.assertEqual(captured["model"], "MiniMax-M2.7")
        self.assertIs(captured["reasoning_split"], True)
        self.assertEqual(captured["thinking"], {"type": "adaptive"})
        self.assertEqual(self.captured["authorization"], "Bearer upstream-secret")


class SoloReasoningProxyTests(unittest.IsolatedAsyncioTestCase):
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
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["body"] = json.loads(rest[:length])
            payload = b'{"choices":[{"message":{"content":"OK"}}]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/chat/completions"
        solo = ModelRoute(
            model_id="qwen3.6-35b-a3b",
            upstream_name="qwen-local",
            upstream_model="qwen.gguf",
            api="openai-completions",
            path="/chat/completions",
            context_window=128000,
            max_tokens=32768,
            inputs=("text",),
            url=url,
            api_key="local",
            extra_body={"reasoning_effort": "high", "reasoning_budget_tokens": 8192},
            solo=True,
        )
        group = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="cloud",
            upstream_model="gemini-3.8-flash",
            api="openai-completions",
            path="/chat/completions",
            context_window=1000000,
            max_tokens=65536,
            inputs=("text",),
            url=url,
            api_key="local",
        )
        self.proxy = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (solo, group),
        )
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def _post(self, body: dict) -> dict:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        raw = json.dumps(body).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n\r\n"
            ).encode()
            + raw
        )
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(4096), timeout=2)
        writer.close()
        self.assertIn(b"200 OK", payload)
        return self.captured["body"]

    async def test_solo_model_forces_medium_and_1536(self) -> None:
        captured = await self._post(
            {
                "model": "qwen3.6-35b-a3b",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "low",
            }
        )
        self.assertEqual(captured["model"], "qwen.gguf")
        self.assertEqual(captured["reasoning_effort"], "medium")
        self.assertEqual(captured["reasoning_budget_tokens"], 1536)

    async def test_solo_model_honors_explicit_reasoning_budget(self) -> None:
        captured = await self._post(
            {
                "model": "qwen3.6-35b-a3b",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "low",
                "reasoning_budget_tokens": 15 * 1024,
            }
        )
        self.assertEqual(captured["reasoning_effort"], "medium")
        self.assertEqual(captured["reasoning_budget_tokens"], 15 * 1024)

    async def test_group_model_keeps_its_own_effort(self) -> None:
        captured = await self._post(
            {
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "low",
            }
        )
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertNotIn("reasoning_budget_tokens", captured)


class ThinkCloseHelperTests(unittest.TestCase):
    def test_stop_without_content_is_unclosed(self) -> None:
        self.assertEqual(
            unclosed_think_reasoning(
                {"content": "", "reasoning_content": "想一想"},
                "stop",
            ),
            "想一想",
        )

    def test_visible_content_is_not_unclosed(self) -> None:
        self.assertIsNone(
            unclosed_think_reasoning(
                {"content": "早点睡", "reasoning_content": "想一想"},
                "stop",
            )
        )

    def test_continuation_closes_think_and_does_not_mutate_the_request(self) -> None:
        original = {"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        follow = continuation_payload(original, "想一想")
        self.assertEqual(original["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(follow["continue_final_message"], "content")
        self.assertFalse(follow["add_generation_prompt"])
        self.assertFalse(follow["stream"])
        self.assertEqual(follow["messages"][-1]["reasoning_content"], "想一想")
        self.assertEqual(follow["messages"][-1]["content"], "")

    def test_splice_keeps_original_when_continuation_is_empty(self) -> None:
        original = {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "想一想"},
                    "finish_reason": "stop",
                }
            ]
        }
        self.assertIs(
            splice_unclosed_think(
                original,
                {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]},
            ),
            original,
        )


class SoloThinkCloseProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[dict] = []

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            body = json.loads(rest[:length])
            self.requests.append(body)
            if body.get("continue_final_message") == "content":
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": "早点睡吧。"},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
            elif body.get("stream") is True:
                sse = (
                    'data: {"choices":[{"index":0,"delta":{"reasoning_content":"我应该回答。"},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"index":0,"delta":{"reasoning_content":"嗯。"},"finish_reason":"stop"}]}\n\n'
                    "data: [DONE]\n\n"
                ).encode()
                framed = f"{len(sse):x}\r\n".encode() + sse + b"\r\n0\r\n\r\n"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    + framed
                )
            else:
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "reasoning_content": "我应该回答。",
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                    + payload
                )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        route = ModelRoute(
            model_id="qwen3.6-35b-a3b",
            upstream_name="qwen-local",
            upstream_model="qwen.gguf",
            api="openai-completions",
            path="/chat/completions",
            context_window=128000,
            max_tokens=32768,
            inputs=("text",),
            url=f"http://127.0.0.1:{port}/chat/completions",
            api_key="local",
            solo=True,
        )
        self.proxy = LlmProxy("127.0.0.1", 0, "loopback-token", (route,))
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def _post(self, body: dict) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        raw = json.dumps(body).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n\r\n"
            ).encode()
            + raw
        )
        await writer.drain()
        chunks: list[bytes] = []
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
            if not chunk:
                break
            chunks.append(chunk)
        writer.close()
        return b"".join(chunks)

    async def test_json_stop_without_content_continues_after_think(self) -> None:
        payload = await self._post(
            {
                "model": "qwen3.6-35b-a3b",
                "messages": [{"role": "user", "content": "睡吗"}],
                "stream": False,
            }
        )
        self.assertIn(b"200 OK", payload)
        body = json.loads(payload.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(body["choices"][0]["message"]["content"], "早点睡吧。")
        self.assertEqual(body["choices"][0]["message"]["reasoning_content"], "我应该回答。")
        self.assertEqual(len(self.requests), 2)
        follow = self.requests[1]
        self.assertEqual(follow["continue_final_message"], "content")
        self.assertFalse(follow["add_generation_prompt"])
        self.assertEqual(follow["messages"][-1]["reasoning_content"], "我应该回答。")
        self.assertEqual(follow["messages"][-1]["content"], "")

    async def test_stream_stop_without_content_appends_the_continuation(self) -> None:
        payload = await self._post(
            {
                "model": "qwen3.6-35b-a3b",
                "messages": [{"role": "user", "content": "睡吗"}],
                "stream": True,
            }
        )
        self.assertIn(b"text/event-stream", payload)
        text = payload.split(b"\r\n\r\n", 1)[1].decode()
        self.assertIn("我应该回答。", text)
        self.assertIn("嗯。", text)
        self.assertIn("早点睡吧。", text)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(
            self.requests[1]["messages"][-1]["reasoning_content"],
            "我应该回答。嗯。",
        )


class DynamicSoloRouteProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_path = self.root / "solo.backend.json"
        self.activity_path = self.root / "solo.activity"
        self.captured: dict[str, object] = {}

        async def upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["body"] = json.loads(rest[:length])
            payload = b'{"choices":[{"message":{"content":"OK"}}]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}/chat/completions"
        group = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="cloud",
            upstream_model="gemini-3.8-flash",
            api="openai-completions",
            path="/chat/completions",
            context_window=1000000,
            max_tokens=65536,
            inputs=("text",),
            url=self.upstream_url,
            api_key="local",
        )
        self.proxy = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (group,),
            solo_runtime_path=self.state_path,
            solo_activity_path=self.activity_path,
        )
        await self.proxy.start()

    async def asyncTearDown(self) -> None:
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()
        self.tempdir.cleanup()

    def _write_state(self, *, running: bool) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "running": running,
                    "route": {
                        "model_id": "Qwen-Test-IQ4_XS",
                        "upstream_model": "/models/Qwen-Test-IQ4_XS.gguf",
                        "context_window": 150000,
                        "max_tokens": 32768,
                        "inputs": ["text", "image"],
                        "endpoint": self.upstream_url.removesuffix(
                            "/chat/completions"
                        ),
                        "api": "openai-completions",
                        "path": "/chat/completions",
                    },
                }
            ),
            encoding="utf-8",
        )

    async def _request(self, method: str, path: str, body: dict | None = None) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        raw = json.dumps(body).encode() if body is not None else b""
        writer.write(
            (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                + (f"Content-Length: {len(raw)}\r\n" if body is not None else "")
                + "\r\n"
            ).encode()
            + raw
        )
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(65536), timeout=2)
        writer.close()
        return payload

    async def test_runtime_route_is_published_and_used(self) -> None:
        self._write_state(running=True)
        head, body = (await self._request("GET", "/v1/models")).split(
            b"\r\n\r\n", 1
        )
        self.assertIn(b"200 OK", head)
        models = json.loads(body)["data"]
        solo = next(item for item in models if item["id"] == "Qwen-Test-IQ4_XS")
        self.assertEqual(solo["context_window"], 150000)
        self.assertEqual(solo["max_tokens"], 32768)
        self.assertEqual(solo["input"], ["text", "image"])
        self.assertTrue(solo["solo"])

        response = await self._request(
            "POST",
            "/v1/chat/completions",
            {
                "model": "Qwen-Test-IQ4_XS",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "low",
            },
        )
        self.assertIn(b"200 OK", response)
        self.assertEqual(
            self.captured["body"]["model"], "/models/Qwen-Test-IQ4_XS.gguf"
        )
        self.assertEqual(self.captured["body"]["reasoning_effort"], "medium")
        self.assertTrue(self.activity_path.is_file())

        self._write_state(running=False)
        _head, body = (await self._request("GET", "/v1/models")).split(
            b"\r\n\r\n", 1
        )
        ids = {item["id"] for item in json.loads(body)["data"]}
        self.assertNotIn("Qwen-Test-IQ4_XS", ids)


class GrokOAuthProxyTests(unittest.IsolatedAsyncioTestCase):
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
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.captured["headers"] = headers
            self.captured["body"] = json.loads(rest[:length])
            payload = b'{"id":"resp_1","object":"response","output":[]}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = self.upstream.sockets[0].getsockname()[1]
        route = ModelRoute(
            model_id="grok-4.6",
            upstream_name="xai",
            upstream_model="grok-4.6",
            api="openai-responses",
            path="/responses",
            context_window=500_000,
            max_tokens=65_536,
            inputs=("text", "image"),
            url=f"http://127.0.0.1:{port}/responses",
            api_key="",
            use_proxy=False,
            auth="grok-oauth",
        )
        self.proxy = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (route,),
            grok_oauth=_FakeGrokOAuth(),
        )
        await self.proxy.start()
        self.client: asyncio.StreamWriter | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            self.client.close()
        await self.proxy.stop()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_adds_grok_build_headers_and_bearer(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.client = writer
        body = json.dumps(
            {"model": "grok-4.6", "input": [], "reasoning": {"effort": "low"}}
        ).encode()
        writer.write(
            (
                "POST /v1/responses HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.proxy.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(4096), timeout=2)
        self.assertIn(b"200 OK", payload)
        headers = self.captured["headers"]
        self.assertEqual(headers["authorization"], "Bearer fresh-oauth-token")
        self.assertEqual(headers["x-xai-token-auth"], "xai-grok-cli")
        self.assertEqual(headers["x-authenticateresponse"], "authenticate-response")
        self.assertEqual(headers["x-grok-client-mode"], "headless")
        self.assertEqual(headers["x-userid"], "user-123")
        self.assertEqual(headers["x-email"], "aki@example.test")
        self.assertEqual(headers["x-grok-client-version"], "1.0.62")
        self.assertEqual(self.captured["body"]["model"], "grok-4.6")


class _FakeGrokOAuth:
    def credentials(self) -> GrokCredentials:
        return GrokCredentials(
            access_token="fresh-oauth-token",
            user_id="user-123",
            email="aki@example.test",
            client_version="1.0.62",
        )


class UpstreamProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        cert = root / "cert.pem"
        key = root / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "1", "-nodes",
                "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        self.seen: dict[str, object] = {}

        async def origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            headers = {}
            for line in head.decode("iso-8859-1").split("\r\n")[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            self.seen["body"] = json.loads(rest[:length])
            self.seen["authorization"] = headers.get("authorization")
            payload = b'{"ok":true}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
            writer.close()

        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        self.origin = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=server_ctx)
        origin_port = self.origin.sockets[0].getsockname()[1]

        async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode("ascii")
            self.seen["connect"] = line
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            target = await asyncio.open_connection("127.0.0.1", origin_port)
            await asyncio.gather(_pipe(reader, target[1]), _pipe(target[0], writer))

        self.proxy_server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        proxy_port = self.proxy_server.sockets[0].getsockname()[1]
        client_ctx = ssl.create_default_context()
        client_ctx.load_verify_locations(cert)
        route = ModelRoute(
            model_id="local-model",
            upstream_name="fake",
            upstream_model="upstream-model",
            api="openai-completions",
            path="/chat/completions",
            context_window=1000,
            max_tokens=100,
            inputs=("text",),
            url=f"https://127.0.0.1:{origin_port}/chat/completions",
            api_key="upstream-secret",
        )
        self.llm = LlmProxy(
            "127.0.0.1",
            0,
            "loopback-token",
            (route,),
            upstream_proxy=f"http://127.0.0.1:{proxy_port}",
            ssl_context=client_ctx,
        )
        await self.llm.start()
        self.client: asyncio.StreamWriter | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            self.client.close()
        await self.llm.stop()
        self.proxy_server.close()
        self.origin.close()
        await self.proxy_server.wait_closed()
        await self.origin.wait_closed()
        self.tempdir.cleanup()

    async def test_https_upstream_goes_through_connect(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.llm.port)
        self.client = writer
        body = json.dumps({"model": "local-model", "messages": []}).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.llm.port}\r\n"
                "Authorization: Bearer loopback-token\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        data = b""
        while b'"ok"' not in data:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=3)
            self.assertTrue(chunk)
            data += chunk
        self.assertEqual(self.seen["connect"], f"CONNECT 127.0.0.1:{self.origin.sockets[0].getsockname()[1]} HTTP/1.1")
        self.assertEqual(self.seen["authorization"], "Bearer upstream-secret")
        self.assertEqual(self.seen["body"]["model"], "upstream-model")


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UpstreamProxyPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_rechecks_priority_for_each_new_connection(self) -> None:
        first_seen = asyncio.Event()
        second_seen = asyncio.Event()

        async def first_proxy(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            del reader
            first_seen.set()
            writer.close()

        async def second_proxy(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            del reader
            second_seen.set()
            writer.close()

        first_port = _unused_tcp_port()
        second = await asyncio.start_server(second_proxy, "127.0.0.1", 0)
        second_port = int(second.sockets[0].getsockname()[1])
        proxy_value = (
            f"http://127.0.0.1:{first_port};http://127.0.0.1:{second_port}"
        )
        first = None
        try:
            reader, writer, _target = await _open_upstream(
                "example.test",
                80,
                "http",
                "/v1/chat/completions",
                upstream_proxy=proxy_value,
                ssl_context=None,
            )
            del reader
            await asyncio.wait_for(second_seen.wait(), timeout=1)
            self.assertFalse(first_seen.is_set())
            writer.close()
            await writer.wait_closed()

            first = await asyncio.start_server(first_proxy, "127.0.0.1", first_port)
            reader, writer, _target = await _open_upstream(
                "example.test",
                80,
                "http",
                "/v1/chat/completions",
                upstream_proxy=proxy_value,
                ssl_context=None,
            )
            del reader
            await asyncio.wait_for(first_seen.wait(), timeout=1)
            writer.close()
            await writer.wait_closed()
        finally:
            second.close()
            if first is not None:
                first.close()
            await second.wait_closed()
            if first is not None:
                await first.wait_closed()

    async def test_uses_first_open_proxy_in_priority_order(self) -> None:
        first_seen = asyncio.Event()
        second_seen = asyncio.Event()

        def handler(seen: asyncio.Event):
            async def proxy(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                del reader
                seen.set()
                writer.close()

            return proxy

        first = await asyncio.start_server(handler(first_seen), "127.0.0.1", 0)
        second = await asyncio.start_server(handler(second_seen), "127.0.0.1", 0)
        first_port = int(first.sockets[0].getsockname()[1])
        second_port = int(second.sockets[0].getsockname()[1])
        try:
            reader, writer, _target = await _open_upstream(
                "example.test",
                80,
                "http",
                "/v1/chat/completions",
                upstream_proxy=(
                    f"http://127.0.0.1:{first_port};"
                    f"http://127.0.0.1:{second_port}"
                ),
                ssl_context=None,
            )
            del reader
            await asyncio.wait_for(first_seen.wait(), timeout=1)
            self.assertFalse(second_seen.is_set())
            writer.close()
            await writer.wait_closed()
        finally:
            first.close()
            second.close()
            await first.wait_closed()
            await second.wait_closed()

    async def test_skips_closed_proxy_and_uses_next_one(self) -> None:
        connected = asyncio.Event()

        async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            del reader
            connected.set()
            writer.close()

        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        closed_port = _unused_tcp_port()
        try:
            reader, writer, target = await _open_upstream(
                "example.test",
                80,
                "http",
                "/v1/chat/completions",
                upstream_proxy=(
                    f"http://127.0.0.1:{closed_port};"
                    f"http://127.0.0.1:{port}"
                ),
                ssl_context=None,
            )
            del reader
            self.assertEqual(target, "http://example.test:80/v1/chat/completions")
            await asyncio.wait_for(connected.wait(), timeout=1)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_sync_selector_returns_first_open_proxy(self) -> None:
        connected = asyncio.Event()

        async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            del reader
            connected.set()
            writer.close()

        server = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        closed_port = _unused_tcp_port()
        try:
            selected = await asyncio.to_thread(
                select_upstream_proxy,
                f"http://127.0.0.1:{closed_port};http://127.0.0.1:{port}",
                timeout=1,
            )
            self.assertEqual(selected, f"http://127.0.0.1:{port}")
            await asyncio.wait_for(connected.wait(), timeout=1)
        finally:
            server.close()
            await server.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


if __name__ == "__main__":
    unittest.main()
