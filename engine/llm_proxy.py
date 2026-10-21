"""Loopback OpenAI-style proxy. Letta talks only to this process."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import ssl
import time
from typing import Mapping
from urllib.parse import urlsplit

from engine.aki_config import ModelRoute, parse_upstream_proxy
from engine.grok_oauth import GrokOAuthError, GrokOAuthProvider

LOGGER = logging.getLogger("akid.llm_proxy")

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
UPSTREAM_CONNECT_ATTEMPTS = 5
UPSTREAM_RETRY_DELAYS = (0.2, 0.4, 0.8, 1.2)
_HOP_BY_HOP = {
    "host",
    "authorization",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
}


class ProxyBindError(RuntimeError):
    pass


def _status_json(status: int, message: str, error_type: str = "proxy") -> bytes:
    body = json.dumps({"error": {"message": message, "type": error_type}}).encode("utf-8")
    reason = {
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        502: "Bad Gateway",
    }.get(status, "Error")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


def _authorized(headers: Mapping[str, str], token: str) -> bool:
    presented = headers.get("authorization", "")
    prefix = "bearer "
    if not presented.lower().startswith(prefix):
        return False
    supplied = presented[len(prefix) :].strip()
    return hmac.compare_digest(supplied, token)


class LlmProxy:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        routes: tuple[ModelRoute, ...] | list[ModelRoute],
        upstream_proxy: str = "",
        ssl_context: ssl.SSLContext | None = None,
        grok_oauth: GrokOAuthProvider | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ProxyBindError("LLM proxy refuses to bind anything except 127.0.0.1")
        self.host = host
        self.port = port
        self.token = token
        self.routes = {route.model_id: route for route in routes if route.enabled}
        self.upstream_proxy = parse_upstream_proxy(upstream_proxy)
        self.ssl_context = ssl_context
        needs_grok_oauth = any(route.auth == "grok-oauth" for route in self.routes.values())
        self.grok_oauth = grok_oauth
        if needs_grok_oauth and self.grok_oauth is None:
            self.grok_oauth = GrokOAuthProvider(upstream_proxy=self.upstream_proxy)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or []
        if not sockets:
            raise ProxyBindError("LLM proxy did not open a socket")
        bound = sockets[0].getsockname()
        if bound[0] != "127.0.0.1":
            self._server.close()
            await self._server.wait_closed()
            raise ProxyBindError(f"LLM proxy bound {bound[0]}, expected 127.0.0.1")
        self.port = int(bound[1])
        via = self.upstream_proxy or "direct"
        LOGGER.info(
            "llm proxy listening on 127.0.0.1:%s models=%s upstream_proxy=%s",
            self.port,
            sorted(self.routes),
            via,
        )

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        model_id = "-"
        upstream_host = "-"
        status = 500
        try:
            status, model_id, upstream_host = await self._dispatch(reader, writer)
        except Exception:
            LOGGER.exception("llm proxy request failed")
            if not writer.is_closing():
                writer.write(_status_json(502, "upstream unavailable"))
                status = 502
        finally:
            with contextlib.suppress(Exception):
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            LOGGER.info(
                "method=proxy path=%s model=%s upstream=%s status=%s ms=%s",
                getattr(self, "_last_path", "-"),
                model_id,
                upstream_host,
                status,
                int((time.monotonic() - started) * 1000),
            )

    async def _dispatch(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[int, str, str]:
        self._last_path = "-"
        try:
            head, leftover = await _read_headers(reader)
        except ValueError:
            writer.write(_status_json(400, "malformed request"))
            return 400, "-", "-"
        try:
            method, target, headers = _parse_head(head)
        except ValueError:
            writer.write(_status_json(400, "malformed request"))
            return 400, "-", "-"
        path = target.split("?", 1)[0]
        self._last_path = path
        if not _authorized(headers, self.token):
            writer.write(_status_json(401, "unauthorized"))
            return 401, "-", "-"
        if method == "GET" and path == "/v1/models":
            writer.write(_models_response(self.routes))
            return 200, "-", "-"
        if method != "POST" or path not in ("/v1/chat/completions", "/v1/responses"):
            writer.write(_status_json(404, "not found"))
            return 404, "-", "-"
        try:
            length = int(headers.get("content-length", ""))
        except ValueError:
            writer.write(_status_json(400, "content-length required"))
            return 400, "-", "-"
        if length < 0 or length > MAX_BODY_BYTES:
            writer.write(_status_json(413, "request too large"))
            return 413, "-", "-"
        body = leftover
        while len(body) < length:
            chunk = await reader.read(min(65536, length - len(body)))
            if not chunk:
                break
            body += chunk
        if len(body) < length:
            writer.write(_status_json(400, "truncated body"))
            return 400, "-", "-"
        body = body[:length]
        try:
            payload = json.loads(body)
        except ValueError:
            writer.write(_status_json(400, "invalid json"))
            return 400, "-", "-"
        if not isinstance(payload, dict):
            writer.write(_status_json(400, "invalid json"))
            return 400, "-", "-"
        model_id = payload.get("model")
        if not isinstance(model_id, str) or not model_id:
            writer.write(_status_json(400, "model is required"))
            return 400, "-", "-"
        route = self.routes.get(model_id)
        expected = "/v1/chat/completions" if route and route.api == "openai-completions" else "/v1/responses"
        if route is None or path != expected:
            writer.write(_status_json(404, "unknown model"))
            return 404, model_id, "-"
        payload["model"] = route.upstream_model
        forwarded = json.dumps(payload).encode("utf-8")
        parsed = urlsplit(route.url)
        upstream_host = parsed.hostname or "-"
        api_key = route.api_key
        extra_headers: dict[str, str] = {}
        if route.auth == "grok-oauth":
            if self.grok_oauth is None:
                writer.write(_status_json(502, "grok oauth is not configured"))
                return 502, model_id, upstream_host
            try:
                credentials = await asyncio.to_thread(self.grok_oauth.credentials)
            except GrokOAuthError as exc:
                LOGGER.warning("grok oauth unavailable: %s", exc)
                writer.write(_status_json(502, "grok oauth unavailable"))
                return 502, model_id, upstream_host
            api_key = credentials.access_token
            extra_headers = credentials.extra_headers()
        try:
            status = await _forward(
                parsed,
                headers,
                forwarded,
                api_key,
                writer,
                upstream_proxy=self.upstream_proxy,
                ssl_context=self.ssl_context,
                use_proxy=route.use_proxy,
                extra_headers=extra_headers,
            )
        except OSError as exc:
            LOGGER.warning(
                "upstream connect failed host=%s type=%s error=%s",
                upstream_host,
                type(exc).__name__,
                exc,
            )
            writer.write(_status_json(502, "upstream unavailable"))
            return 502, model_id, upstream_host
        return status, model_id, upstream_host


async def _read_headers(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise ValueError("headers too large")
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        raise ValueError("incomplete headers")
    return data[:marker], data[marker + 4 :]


def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    text = head.decode("iso-8859-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise ValueError("bad request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return parts[0].upper(), parts[1], headers


def _models_response(routes: dict[str, ModelRoute]) -> bytes:
    body = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "owned_by": "aki"}
                for model_id in sorted(routes)
            ],
        }
    ).encode("utf-8")
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


async def _forward(
    parsed,
    request_headers: Mapping[str, str],
    body: bytes,
    api_key: str,
    client: asyncio.StreamWriter,
    *,
    upstream_proxy: str = "",
    ssl_context: ssl.SSLContext | None = None,
    use_proxy: bool = True,
    extra_headers: Mapping[str, str] | None = None,
) -> int:
    host = parsed.hostname
    if not host:
        raise OSError("missing upstream host")
    scheme = parsed.scheme
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    upstream_reader, upstream_writer, request_target = await _open_upstream(
        host,
        port,
        scheme,
        path,
        upstream_proxy=upstream_proxy if use_proxy else "",
        ssl_context=ssl_context,
    )
    try:
        header_lines = [
            f"POST {request_target} HTTP/1.1",
            f"Host: {host if port in (80, 443) else f'{host}:{port}'}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if api_key:
            header_lines.append(f"Authorization: Bearer {api_key}")
        extra_headers = extra_headers or {}
        for name, value in extra_headers.items():
            header_lines.append(f"{name}: {value}")
        reserved = {name.lower() for name in extra_headers}
        for name, value in request_headers.items():
            if name in _HOP_BY_HOP or name in reserved or name == "content-type":
                continue
            header_lines.append(f"{name}: {value}")
        upstream_writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("iso-8859-1"))
        upstream_writer.write(body)
        await upstream_writer.drain()
        status = await _relay_response(upstream_reader, client)
        return status
    finally:
        upstream_writer.close()
        with contextlib.suppress(Exception):
            await upstream_writer.wait_closed()


def _retryable_upstream_connect(exc: OSError) -> bool:
    message = str(exc)
    if any(
        marker in message
        for marker in (
            "missing upstream host",
            "upstream proxy refused CONNECT",
            "upstream proxy returned a bad CONNECT status",
        )
    ):
        return False
    return isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)) or not message


async def _open_upstream(
    host: str,
    port: int,
    scheme: str,
    path: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    attempts = UPSTREAM_CONNECT_ATTEMPTS if upstream_proxy else 1
    for attempt in range(1, attempts + 1):
        try:
            return await _open_upstream_once(
                host,
                port,
                scheme,
                path,
                upstream_proxy=upstream_proxy,
                ssl_context=ssl_context,
            )
        except OSError as exc:
            if attempt >= attempts or not _retryable_upstream_connect(exc):
                raise
            delay = UPSTREAM_RETRY_DELAYS[min(attempt - 1, len(UPSTREAM_RETRY_DELAYS) - 1)]
            LOGGER.warning(
                "upstream connect retry host=%s attempt=%s/%s type=%s error=%s",
                host,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(delay)
    raise OSError("upstream connection failed")


async def _open_upstream_once(
    host: str,
    port: int,
    scheme: str,
    path: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    use_tls = scheme == "https"
    context = ssl_context if ssl_context is not None else ssl.create_default_context()
    if not upstream_proxy:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=context if use_tls else None,
                server_hostname=host if use_tls else None,
            ),
            timeout=30,
        )
        return reader, writer, path
    proxy = urlsplit(upstream_proxy)
    proxy_host = proxy.hostname or ""
    proxy_port = proxy.port or 80
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(proxy_host, proxy_port),
        timeout=30,
    )
    if not use_tls:
        return reader, writer, f"http://{host}:{port}{path}"
    target = f"{host}:{port}"
    writer.write(
        (
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            "Proxy-Connection: keep-alive\r\n"
            "\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    status = await _read_connect_status(reader)
    if status != 200:
        writer.close()
        raise OSError(f"upstream proxy refused CONNECT ({status})")
    loop = asyncio.get_running_loop()
    protocol = writer.transport.get_protocol()
    tls_transport = await loop.start_tls(
        writer.transport,
        protocol,
        context,
        server_hostname=host,
    )
    writer._transport = tls_transport
    return reader, writer, path


async def _read_connect_status(reader: asyncio.StreamReader) -> int:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            raise OSError("upstream proxy closed during CONNECT")
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise OSError("upstream proxy headers too large")
    line = data.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = line.split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise OSError("upstream proxy returned a bad CONNECT status")
    return int(parts[1])


async def _relay_response(upstream: asyncio.StreamReader, client: asyncio.StreamWriter) -> int:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await upstream.read(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER_BYTES:
            raise OSError("upstream headers too large")
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        raise OSError("upstream closed before headers")
    head = data[:marker]
    rest = data[marker + 4 :]
    client.write(head + b"\r\n\r\n")
    await client.drain()
    status = _status_code(head)
    headers = _header_map(head)
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError as exc:
            raise OSError("bad content-length") from exc
        pending = rest
        if len(pending) > length:
            pending = pending[:length]
        if pending:
            client.write(pending)
            await client.drain()
        remaining = length - len(pending)
        while remaining > 0:
            chunk = await upstream.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            client.write(chunk)
            await client.drain()
        return status
    if "chunked" in headers.get("transfer-encoding", ""):
        buffer = rest
        while True:
            while b"\r\n" not in buffer:
                chunk = await upstream.read(4096)
                if not chunk:
                    return status
                buffer += chunk
            line, buffer = buffer.split(b"\r\n", 1)
            client.write(line + b"\r\n")
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise OSError("bad chunk size") from exc
            while len(buffer) < size + 2:
                chunk = await upstream.read(65536)
                if not chunk:
                    return status
                buffer += chunk
            client.write(buffer[: size + 2])
            await client.drain()
            buffer = buffer[size + 2 :]
            if size == 0:
                if buffer:
                    client.write(buffer)
                    await client.drain()
                return status
    if rest:
        client.write(rest)
        await client.drain()
    while True:
        chunk = await upstream.read(65536)
        if not chunk:
            return status
        client.write(chunk)
        await client.drain()


def _status_code(head: bytes) -> int:
    line = head.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = line.split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        return 502
    return int(parts[1])


def _header_map(head: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        headers[name.decode("iso-8859-1").strip().lower()] = value.decode("iso-8859-1").strip()
    return headers
