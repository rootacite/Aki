from __future__ import annotations

import asyncio
import json
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.aki_config import ModelRoute
from engine.grok_oauth import GrokCredentials
from engine.llm_proxy import LlmProxy, ProxyBindError, _open_upstream


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

    async def test_captures_raw_forwarded_provider_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            route = next(iter(self.proxy.routes.values()))
            proxy = LlmProxy(
                "127.0.0.1",
                0,
                "loopback-token",
                (route,),
                profile="haru",
                capture_dir=directory,
            )
            await proxy.start()
            writer = None
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", proxy.port
                )
                body = json.dumps(
                    {
                        "model": "local-model",
                        "messages": [{"role": "user", "content": "hello capture"}],
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

                path = Path(directory) / "raw-api-calls-haru.jsonl"
                record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(record["profile"], "haru")
                self.assertEqual(record["model"], "local-model")
                self.assertEqual(record["upstream_model"], "upstream-model")
                self.assertEqual(record["payload"]["model"], "upstream-model")
                self.assertEqual(
                    record["payload"]["messages"][0]["content"], "hello capture"
                )
                self.assertNotIn("authorization", record)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
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
