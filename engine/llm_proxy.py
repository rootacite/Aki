"""Loopback OpenAI-style proxy. Letta talks only to this process."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import json
import logging
import os
import ssl
import time
import uuid
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from engine.aki_config import ModelRoute, parse_upstream_proxy
from engine.grok_oauth import GrokOAuthError, GrokOAuthProvider
from engine.letta_llm import _compat
from engine.proxy_debug import DebugResponse, ProxyDebugStream, resolve_debug_socket
from engine.solo_mode import SOLO_REASONING_BUDGET_TOKENS, SOLO_REASONING_EFFORT

LOGGER = logging.getLogger("akid.llm_proxy")

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
UPSTREAM_CONNECT_ATTEMPTS = 5
UPSTREAM_RETRY_DELAYS = (0.2, 0.4, 0.8, 1.2)
SOLO_ACTIVITY_HEARTBEAT_SECONDS = 60.0
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
        profile: str = "",
        debug_socket: str | os.PathLike[str] | None = None,
        solo_runtime_path: str | os.PathLike[str] | None = None,
        solo_activity_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ProxyBindError("LLM proxy refuses to bind anything except 127.0.0.1")
        self.host = host
        self.port = port
        self.token = token
        self._configured_routes: dict[str, ModelRoute] = {}
        self.routes: dict[str, ModelRoute] = {}
        self.profile = profile
        self.solo_runtime_path = (
            Path(solo_runtime_path).expanduser()
            if solo_runtime_path is not None
            else None
        )
        self.solo_activity_path = (
            Path(solo_activity_path).expanduser()
            if solo_activity_path is not None
            else None
        )
        self._solo_signature: tuple[int, int] | None = None
        self._solo_route_id = ""
        self.set_configured_routes(routes)
        self.upstream_proxy = parse_upstream_proxy(upstream_proxy)
        self.ssl_context = ssl_context
        needs_grok_oauth = any(route.auth == "grok-oauth" for route in self.routes.values())
        self.grok_oauth = grok_oauth
        if needs_grok_oauth and self.grok_oauth is None:
            self.grok_oauth = GrokOAuthProvider(upstream_proxy=self.upstream_proxy)
        configured_debug = debug_socket or os.environ.get("AKI_PROXY_DEBUG_SOCKET", "")
        self.debug_socket = resolve_debug_socket(configured_debug, profile)
        self.debug: ProxyDebugStream | None = None
        self._server: asyncio.Server | None = None

    def set_configured_routes(
        self, routes: tuple[ModelRoute, ...] | list[ModelRoute]
    ) -> None:
        self._configured_routes = {
            route.model_id: route for route in routes if route.enabled
        }
        self._rebuild_routes()

    def _rebuild_routes(self) -> None:
        self.routes = dict(self._configured_routes)
        self._solo_route_id = ""
        self._solo_signature = None
        self._refresh_solo_route(force=True)

    def _remove_solo_route(self) -> None:
        model_id = self._solo_route_id
        if not model_id:
            return
        configured = self._configured_routes.get(model_id)
        if configured is not None:
            self.routes[model_id] = configured
        else:
            self.routes.pop(model_id, None)
        self._solo_route_id = ""

    def _refresh_solo_route(self, *, force: bool = False) -> None:
        path = self.solo_runtime_path
        if path is None:
            return
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if not force and signature == self._solo_signature:
            return
        self._solo_signature = signature
        self._remove_solo_route()
        if signature is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        route_data = payload.get("route") if isinstance(payload, dict) else None
        if payload.get("running") is not True or not isinstance(route_data, dict):
            return
        try:
            model_id = str(route_data["model_id"])
            upstream_model = str(route_data["upstream_model"])
            endpoint = str(route_data["endpoint"]).rstrip("/")
            context_window = int(route_data["context_window"])
            max_tokens = int(route_data["max_tokens"])
            inputs = tuple(str(item) for item in route_data.get("inputs", ["text"]))
            api = str(route_data.get("api") or "openai-completions")
            route_path = str(route_data.get("path") or "/chat/completions")
        except (KeyError, TypeError, ValueError):
            return
        if (
            not model_id
            or "/" in model_id
            or not upstream_model
            or not endpoint
            or context_window <= 0
            or max_tokens <= 0
            or api != "openai-completions"
            or route_path != "/chat/completions"
        ):
            return
        self.routes[model_id] = ModelRoute(
            model_id=model_id,
            upstream_name="solo-local",
            upstream_model=upstream_model,
            api=api,
            path=route_path,
            context_window=context_window,
            max_tokens=max_tokens,
            inputs=inputs,
            url=endpoint + route_path,
            api_key="local",
            use_proxy=False,
            solo=True,
        )
        self._solo_route_id = model_id

    def _touch_solo_activity(self) -> None:
        path = self.solo_activity_path
        if path is None:
            return
        now = time.time()
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.touch(exist_ok=True)
            os.utime(path, (now, now))

    async def _solo_activity_heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(SOLO_ACTIVITY_HEARTBEAT_SECONDS)
                self._touch_solo_activity()
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        if self.debug_socket is not None:
            try:
                self.debug = ProxyDebugStream(self.debug_socket, self.profile)
                await self.debug.start()
            except Exception as exc:
                LOGGER.error("proxy debug stream unavailable path=%s error=%s", self.debug_socket, exc)
                self.debug = None
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
        if server is not None:
            server.close()
            await server.wait_closed()
            self._server = None
        debug = self.debug
        self.debug = None
        if debug is not None:
            await debug.stop()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        model_id = "-"
        upstream_host = "-"
        status = 500
        try:
            status, model_id, upstream_host = await self._dispatch(reader, writer, request_id)
        except Exception:
            LOGGER.exception("llm proxy request failed")
            if self.debug is not None:
                self.debug.publish_error(request_id, status=502, error="upstream unavailable")
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
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request_id: str,
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
        self._refresh_solo_route()
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
        requested_reasoning_budget = payload.get("reasoning_budget_tokens")
        payload["model"] = route.upstream_model
        if route.extra_body:
            payload.update(route.extra_body)
        if route.solo:
            payload["reasoning_effort"] = SOLO_REASONING_EFFORT
            if (
                isinstance(requested_reasoning_budget, int)
                and not isinstance(requested_reasoning_budget, bool)
                and requested_reasoning_budget > 0
            ):
                payload["reasoning_budget_tokens"] = requested_reasoning_budget
            else:
                payload["reasoning_budget_tokens"] = SOLO_REASONING_BUDGET_TOKENS
        forwarded = json.dumps(payload).encode("utf-8")
        if self.debug is not None:
            self.debug.publish_request(
                request_id,
                path=path,
                requested_model=model_id,
                upstream_model=route.upstream_model,
                upstream_url=route.url,
                payload=payload,
                forwarded=forwarded,
            )
        parsed = urlsplit(route.url)
        upstream_host = parsed.hostname or "-"
        api_key = route.api_key
        extra_headers: dict[str, str] = {}
        if route.auth == "grok-oauth":
            if self.grok_oauth is None:
                if self.debug is not None:
                    self.debug.publish_error(request_id, status=502, error="grok oauth is not configured")
                writer.write(_status_json(502, "grok oauth is not configured"))
                return 502, model_id, upstream_host
            try:
                credentials = await asyncio.to_thread(self.grok_oauth.credentials)
            except GrokOAuthError as exc:
                LOGGER.warning("grok oauth unavailable: %s", exc)
                if self.debug is not None:
                    self.debug.publish_error(request_id, status=502, error="grok oauth unavailable")
                writer.write(_status_json(502, "grok oauth unavailable"))
                return 502, model_id, upstream_host
            api_key = credentials.access_token
            extra_headers = credentials.extra_headers()
        activity_task: asyncio.Task[None] | None = None
        if route.solo:
            self._touch_solo_activity()
            activity_task = asyncio.create_task(
                self._solo_activity_heartbeat(), name="solo-proxy-activity"
            )
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
                debug=self.debug,
                request_id=request_id,
                solo_think_close=(
                    route.solo
                    and path == "/v1/chat/completions"
                    and "continue_final_message" not in payload
                ),
                request_payload=payload,
            )
        except OSError as exc:
            LOGGER.warning(
                "upstream connect failed host=%s type=%s error=%s",
                upstream_host,
                type(exc).__name__,
                exc,
            )
            if self.debug is not None:
                self.debug.publish_error(request_id, status=502, error="upstream unavailable")
            writer.write(_status_json(502, "upstream unavailable"))
            return 502, model_id, upstream_host
        finally:
            if activity_task is not None:
                activity_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await activity_task
                self._touch_solo_activity()
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
    entries: list[dict[str, object]] = []
    for model_id in sorted(routes):
        route = routes[model_id]
        entry: dict[str, object] = {
            "id": model_id,
            "object": "model",
            "owned_by": "aki",
            "name": model_id,
            "api": route.api,
            "context_window": route.context_window,
            "max_tokens": route.max_tokens,
            "input": list(route.inputs),
            "solo": route.solo,
        }
        compat = _compat(route)
        if compat is not None:
            entry["compat"] = compat
        entries.append(entry)
    body = json.dumps(
        {
            "object": "list",
            "data": entries,
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
    debug: ProxyDebugStream | None = None,
    request_id: str = "",
    solo_think_close: bool = False,
    request_payload: dict | None = None,
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
        if solo_think_close and isinstance(request_payload, dict):
            status = await _relay_solo_think_close(
                upstream_reader,
                client,
                parsed,
                request_payload,
                api_key,
                upstream_proxy=upstream_proxy if use_proxy else "",
                ssl_context=ssl_context,
                extra_headers=extra_headers,
                debug=debug,
                request_id=request_id,
            )
        else:
            status = await _relay_response(
                upstream_reader,
                client,
                debug=debug,
                request_id=request_id,
            )
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


def unclosed_think_reasoning(message: Mapping[str, object], finish_reason: object) -> str | None:
    """Return the reasoning when a stop left the think block unclosed.

    A normal stop has visible content. A tool stop is a different shape.
    """
    if finish_reason != "stop":
        return None
    if message.get("tool_calls"):
        return None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return None
    if isinstance(content, list) and any(
        isinstance(part, Mapping) and str(part.get("text") or "").strip()
        for part in content
    ):
        return None
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str) or not reasoning.strip():
        reasoning = message.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        return None
    return reasoning


def continuation_payload(payload: Mapping[str, object], reasoning: str) -> dict:
    """Ask llama.cpp to close the think block and continue the answer."""
    follow = dict(payload)
    messages = list(payload.get("messages") or [])
    messages.append(
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": reasoning,
        }
    )
    follow["messages"] = messages
    follow["continue_final_message"] = "content"
    follow["add_generation_prompt"] = False
    follow["stream"] = False
    return follow


def splice_unclosed_think(original: dict, continuation: dict) -> dict:
    """Put the continuation answer into the original completion."""
    text = _completion_text(continuation)
    if not text.strip():
        return original
    spliced = copy.deepcopy(original)
    choices = spliced.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return original
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
        choice["message"] = message
    message["content"] = text
    follow_choices = continuation.get("choices")
    if isinstance(follow_choices, list) and follow_choices and isinstance(follow_choices[0], dict):
        finish = follow_choices[0].get("finish_reason")
        if isinstance(finish, str) and finish:
            choice["finish_reason"] = finish
    return spliced


def _completion_text(payload: Mapping[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ""
    choice = choices[0]
    message = choice.get("message")
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping)
            )
    text = choice.get("text")
    return text if isinstance(text, str) else ""


class _ThinkCloseState:
    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.tools = False
        self.finish = ""

    def add_source(self, source: Mapping[str, object]) -> None:
        content = source.get("content")
        if isinstance(content, str):
            self.content += content
        reasoning = source.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = source.get("reasoning")
        if isinstance(reasoning, str):
            self.reasoning += reasoning
        if source.get("tool_calls"):
            self.tools = True

    def add_event(self, payload: Mapping[str, object]) -> None:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            self.add_source(delta)
        message = choice.get("message")
        if isinstance(message, Mapping):
            self.add_source(message)
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self.finish = finish

    def reasoning_to_close(self) -> str | None:
        if self.tools or self.finish != "stop" or self.content.strip():
            return None
        if not self.reasoning.strip():
            return None
        return self.reasoning


def _without_finish(payload: dict) -> dict | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    choice = dict(choices[0])
    choice.pop("finish_reason", None)
    delta = choice.get("delta")
    message = choice.get("message")
    has_payload = False
    for source in (delta, message):
        if not isinstance(source, dict):
            continue
        if any(source.get(key) for key in ("content", "reasoning_content", "reasoning", "tool_calls")):
            has_payload = True
    if not has_payload:
        return None
    cloned = dict(payload)
    cloned["choices"] = [choice, *choices[1:]]
    return cloned


def _split_sse(buffer: bytes) -> tuple[list[bytes], bytes]:
    events: list[bytes] = []
    while True:
        marker = buffer.find(b"\n\n")
        if marker < 0:
            break
        events.append(buffer[:marker])
        buffer = buffer[marker + 2 :]
    return events, buffer


def _sse_data(event: bytes) -> str | None:
    lines: list[str] = []
    for line in event.split(b"\n"):
        if line.startswith(b"data:"):
            lines.append(line[5:].strip().decode("utf-8", "replace"))
    if not lines:
        return None
    return "\n".join(lines)


class _UpstreamBody:
    """Read an upstream HTTP body already past its headers."""

    def __init__(self, reader: asyncio.StreamReader, headers: Mapping[str, str], rest: bytes) -> None:
        self.reader = reader
        self.rest = rest
        self.mode = "close"
        self.left = 0
        self.chunk_left: int | None = None
        self.done = False
        if "content-length" in headers:
            self.mode = "length"
            try:
                self.left = int(headers["content-length"])
            except ValueError as exc:
                raise OSError("bad content-length") from exc
        elif "chunked" in headers.get("transfer-encoding", ""):
            self.mode = "chunked"

    async def read(self, size: int = 65536) -> bytes:
        if self.done:
            return b""
        if self.mode == "length":
            return await self._read_length(size)
        if self.mode == "chunked":
            return await self._read_chunked(size)
        if self.rest:
            chunk = self.rest[:size]
            self.rest = self.rest[len(chunk) :]
            return chunk
        return await self._read_some(size)

    async def _read_length(self, size: int) -> bytes:
        if self.left <= 0:
            self.done = True
            return b""
        if not self.rest:
            self.rest = await self._read_some(min(65536, self.left))
            if not self.rest:
                self.done = True
                return b""
        take = min(size, self.left, len(self.rest))
        chunk = self.rest[:take]
        self.rest = self.rest[take:]
        self.left -= take
        if self.left <= 0:
            self.done = True
        return chunk

    async def _read_some(self, size: int) -> bytes:
        try:
            return await self.reader.read(size)
        except (ConnectionResetError, BrokenPipeError):
            return b""

    async def _fill(self, size: int) -> None:
        while len(self.rest) < size:
            chunk = await self._read_some(65536)
            if not chunk:
                return
            self.rest += chunk

    async def _read_chunked(self, size: int) -> bytes:
        while True:
            if self.chunk_left is None:
                await self._fill(2)
                marker = self.rest.find(b"\r\n")
                if marker < 0:
                    self.done = True
                    return b""
                line = self.rest[:marker]
                self.rest = self.rest[marker + 2 :]
                try:
                    self.chunk_left = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError as exc:
                    raise OSError("bad chunk size") from exc
                if self.chunk_left == 0:
                    self.done = True
                    return b""
            if self.chunk_left == 0:
                self.chunk_left = None
                continue
            if not self.rest:
                self.rest = await self._read_some(65536)
                if not self.rest:
                    self.done = True
                    return b""
            take = min(size, self.chunk_left, len(self.rest))
            chunk = self.rest[:take]
            self.rest = self.rest[take:]
            self.chunk_left -= take
            if self.chunk_left == 0:
                await self._fill(2)
                if self.rest.startswith(b"\r\n"):
                    self.rest = self.rest[2:]
                self.chunk_left = None
            return chunk


async def _relay_solo_think_close(
    upstream: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    parsed,
    request_payload: dict,
    api_key: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
    extra_headers: Mapping[str, str] | None,
    debug: ProxyDebugStream | None,
    request_id: str,
) -> int:
    raw_head, headers, rest = await _read_upstream_head(upstream)
    status = _status_code(raw_head)
    if status != 200:
        client.write(raw_head + b"\r\n\r\n")
        await client.drain()
        await _copy_upstream_body(upstream, client, headers, rest)
        return status
    wants_stream = request_payload.get("stream") is True
    if wants_stream:
        return await _relay_solo_stream(
            upstream,
            client,
            parsed,
            request_payload,
            api_key,
            headers,
            rest,
            upstream_proxy=upstream_proxy,
            ssl_context=ssl_context,
            extra_headers=extra_headers,
            debug=debug,
            request_id=request_id,
        )
    body = await _read_upstream_bytes(upstream, headers, rest)
    try:
        payload = json.loads(body)
    except ValueError:
        _write_json_response(client, status, body)
        return status
    if isinstance(payload, dict):
        rewritten = await _maybe_continue_json(
            parsed,
            request_payload,
            payload,
            api_key,
            upstream_proxy=upstream_proxy,
            ssl_context=ssl_context,
            extra_headers=extra_headers,
        )
        body = json.dumps(rewritten, ensure_ascii=False).encode("utf-8")
    _write_json_response(client, status, body)
    _debug_response(debug, request_id, status, {"content-type": "application/json"}, body)
    return status


async def _maybe_continue_json(
    parsed,
    request_payload: dict,
    response_payload: dict,
    api_key: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
    extra_headers: Mapping[str, str] | None,
) -> dict:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return response_payload
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return response_payload
    reasoning = unclosed_think_reasoning(message, choice.get("finish_reason"))
    if reasoning is None:
        return response_payload
    follow = await _continue_unclosed_think(
        parsed,
        request_payload,
        reasoning,
        api_key,
        upstream_proxy=upstream_proxy,
        ssl_context=ssl_context,
        extra_headers=extra_headers,
    )
    if follow is None:
        return response_payload
    return splice_unclosed_think(response_payload, follow)


async def _relay_solo_stream(
    upstream: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    parsed,
    request_payload: dict,
    api_key: str,
    headers: Mapping[str, str],
    rest: bytes,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
    extra_headers: Mapping[str, str] | None,
    debug: ProxyDebugStream | None,
    request_id: str,
) -> int:
    client.write(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Cache-Control: no-cache\r\n"
        b"Connection: close\r\n\r\n"
    )
    await client.drain()
    response_debug = _debug_begin(
        debug,
        request_id,
        200,
        {"content-type": "text/event-stream"},
    )
    state = _ThinkCloseState()
    held: list[str] = []
    holding = False
    body = _UpstreamBody(upstream, headers, rest)
    buffer = b""
    try:
        while True:
            chunk = await body.read(4096)
            if chunk:
                buffer += chunk
            events, buffer = _split_sse(buffer)
            for event in events:
                holding = await _forward_sse_event(
                    client,
                    event,
                    state,
                    held,
                    holding,
                    response_debug,
                )
            if not chunk:
                if buffer.strip():
                    await _forward_sse_event(
                        client, buffer, state, held, holding, response_debug
                    )
                break
        reasoning = state.reasoning_to_close()
        if reasoning is None:
            for item in held:
                await _write_sse(client, item, response_debug)
            return 200
        if held:
            try:
                finished = json.loads(held[0])
            except ValueError:
                finished = None
            if isinstance(finished, dict):
                partial = _without_finish(finished)
                if partial is not None:
                    await _write_sse(
                        client,
                        json.dumps(partial, ensure_ascii=False),
                        response_debug,
                    )
        follow = await _continue_unclosed_think(
            parsed,
            request_payload,
            reasoning,
            api_key,
            upstream_proxy=upstream_proxy,
            ssl_context=ssl_context,
            extra_headers=extra_headers,
        )
        text = _completion_text(follow) if isinstance(follow, dict) else ""
        if not text.strip():
            for item in held:
                await _write_sse(client, item, response_debug)
            if not any(item.strip() == "[DONE]" for item in held):
                await _write_sse(client, "[DONE]", response_debug)
            return 200
        await _write_sse(
            client,
            json.dumps(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": text}, "finish_reason": None}
                    ]
                },
                ensure_ascii=False,
            ),
            response_debug,
        )
        finish = "stop"
        follow_choices = follow.get("choices") if isinstance(follow, dict) else None
        if (
            isinstance(follow_choices, list)
            and follow_choices
            and isinstance(follow_choices[0], dict)
        ):
            follow_finish = follow_choices[0].get("finish_reason")
            if isinstance(follow_finish, str) and follow_finish:
                finish = follow_finish
        await _write_sse(
            client,
            json.dumps(
                {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                ensure_ascii=False,
            ),
            response_debug,
        )
        await _write_sse(client, "[DONE]", response_debug)
        return 200
    finally:
        if response_debug is not None:
            try:
                response_debug.end(truncated=False)
            except Exception:
                LOGGER.exception("proxy debug response end failed")


async def _forward_sse_event(
    client: asyncio.StreamWriter,
    event: bytes,
    state: _ThinkCloseState,
    held: list[str],
    holding: bool,
    response_debug: DebugResponse | None,
) -> bool:
    data = _sse_data(event)
    if data is None:
        return holding
    if holding or data.strip() == "[DONE]":
        held.append(data)
        return True
    try:
        payload = json.loads(data)
    except ValueError:
        await _write_sse(client, data, response_debug)
        return holding
    if not isinstance(payload, dict):
        await _write_sse(client, data, response_debug)
        return holding
    before = state.finish
    state.add_event(payload)
    if state.finish and not before:
        held.append(data)
        return True
    await _write_sse(client, data, response_debug)
    return False


async def _write_sse(
    client: asyncio.StreamWriter,
    data: str,
    response_debug: DebugResponse | None,
) -> None:
    chunk = f"data: {data}\n\n".encode("utf-8")
    client.write(chunk)
    await client.drain()
    if response_debug is not None:
        try:
            response_debug.chunk(chunk)
        except Exception:
            LOGGER.exception("proxy debug response chunk failed")


async def _continue_unclosed_think(
    parsed,
    request_payload: dict,
    reasoning: str,
    api_key: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
    extra_headers: Mapping[str, str] | None,
) -> dict | None:
    follow = continuation_payload(request_payload, reasoning)
    body = json.dumps(follow).encode("utf-8")
    LOGGER.info(
        "solo think-close continuation reasoning_chars=%s",
        len(reasoning),
    )
    try:
        status, payload = await _post_upstream_json(
            parsed,
            body,
            api_key,
            upstream_proxy=upstream_proxy,
            ssl_context=ssl_context,
            extra_headers=extra_headers,
        )
    except (OSError, ValueError) as exc:
        LOGGER.warning(
            "solo think-close continuation failed error=%s",
            exc,
        )
        return None
    if status != 200 or not isinstance(payload, dict):
        LOGGER.warning(
            "solo think-close continuation failed status=%s",
            status,
        )
        return None
    return payload


async def _post_upstream_json(
    parsed,
    body: bytes,
    api_key: str,
    *,
    upstream_proxy: str,
    ssl_context: ssl.SSLContext | None,
    extra_headers: Mapping[str, str] | None,
) -> tuple[int, dict]:
    host = parsed.hostname
    if not host:
        raise OSError("missing upstream host")
    scheme = parsed.scheme
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    reader, writer, request_target = await _open_upstream(
        host,
        port,
        scheme,
        path,
        upstream_proxy=upstream_proxy,
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
        for name, value in (extra_headers or {}).items():
            header_lines.append(f"{name}: {value}")
        writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("iso-8859-1"))
        writer.write(body)
        await writer.drain()
        raw_head, headers, rest = await _read_upstream_head(reader)
        status = _status_code(raw_head)
        raw = await _read_upstream_bytes(reader, headers, rest)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("continuation response is not json") from exc
    if not isinstance(payload, dict):
        raise ValueError("continuation response is not an object")
    return status, payload


async def _read_upstream_head(
    reader: asyncio.StreamReader,
) -> tuple[bytes, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
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
    return head, _header_map(head), rest


async def _read_upstream_bytes(
    reader: asyncio.StreamReader,
    headers: Mapping[str, str],
    rest: bytes,
) -> bytes:
    body = _UpstreamBody(reader, headers, rest)
    chunks: list[bytes] = []
    while True:
        chunk = await body.read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


async def _copy_upstream_body(
    reader: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    headers: Mapping[str, str],
    rest: bytes,
) -> None:
    body = _UpstreamBody(reader, headers, rest)
    while True:
        chunk = await body.read(65536)
        if not chunk:
            return
        client.write(chunk)
        await client.drain()


def _write_json_response(client: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reason = "OK" if status == 200 else "Error"
    client.write(
        (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        + body
    )


def _debug_begin(
    debug: ProxyDebugStream | None,
    request_id: str,
    status: int,
    headers: Mapping[str, str],
) -> DebugResponse | None:
    if debug is None:
        return None
    try:
        return debug.begin_response(request_id, status=status, headers=dict(headers))
    except Exception:
        LOGGER.exception("proxy debug response start failed")
        return None


def _debug_response(
    debug: ProxyDebugStream | None,
    request_id: str,
    status: int,
    headers: Mapping[str, str],
    body: bytes,
) -> None:
    response = _debug_begin(debug, request_id, status, headers)
    if response is None or not body:
        return
    try:
        response.chunk(body)
        response.end(truncated=False)
    except Exception:
        LOGGER.exception("proxy debug response chunk failed")


async def _relay_response(
    upstream: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    *,
    debug: ProxyDebugStream | None = None,
    request_id: str = "",
) -> int:
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
    response_debug: DebugResponse | None = None
    if debug is not None:
        try:
            response_debug = debug.begin_response(
                request_id,
                status=status,
                headers=headers,
            )
        except Exception:
            LOGGER.exception("proxy debug response start failed")

    def emit_chunk(chunk: bytes) -> None:
        if response_debug is None or not chunk:
            return
        try:
            response_debug.chunk(chunk)
        except Exception:
            LOGGER.exception("proxy debug response chunk failed")

    truncated = False
    try:
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
                emit_chunk(pending)
            remaining = length - len(pending)
            while remaining > 0:
                chunk = await upstream.read(min(65536, remaining))
                if not chunk:
                    truncated = True
                    break
                remaining -= len(chunk)
                client.write(chunk)
                await client.drain()
                emit_chunk(chunk)
            return status
        if "chunked" in headers.get("transfer-encoding", ""):
            buffer = rest
            while True:
                while b"\r\n" not in buffer:
                    chunk = await upstream.read(4096)
                    if not chunk:
                        truncated = True
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
                        truncated = True
                        return status
                    buffer += chunk
                client.write(buffer[: size + 2])
                await client.drain()
                if size > 0:
                    emit_chunk(buffer[:size])
                buffer = buffer[size + 2 :]
                if size == 0:
                    if buffer:
                        client.write(buffer)
                        await client.drain()
                    return status
        if rest:
            client.write(rest)
            await client.drain()
            emit_chunk(rest)
        while True:
            chunk = await upstream.read(65536)
            if not chunk:
                return status
            client.write(chunk)
            await client.drain()
            emit_chunk(chunk)
    finally:
        if response_debug is not None:
            try:
                response_debug.end(truncated=truncated)
            except Exception:
                LOGGER.exception("proxy debug response end failed")


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
