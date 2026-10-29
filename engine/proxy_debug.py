"""Live Unix-socket debug stream for the loopback LLM proxy."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import socket
import stat
import struct
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

LOGGER = logging.getLogger("akid.proxy_debug")

SCHEMA = "akid.proxy-debug.v1"
DEFAULT_SOCKET_DIR = Path("/tmp/akid")
DEFAULT_RING_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_RING_MAX_EVENTS = 2000
DEFAULT_MAX_EVENT_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_CLIENTS = 8
DEFAULT_CLIENT_QUEUE_EVENTS = 512
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
}


def safe_profile(profile: str) -> str:
    return "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in (profile or "proxy")
    )


def default_socket_path(profile: str) -> Path:
    return DEFAULT_SOCKET_DIR / f"proxy-debug-{safe_profile(profile)}.sock"


def resolve_debug_socket(value: str | os.PathLike[str] | None, profile: str) -> Path | None:
    raw = str(value or "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off"}:
        return None
    if raw.lower() in {"1", "true", "yes", "on"}:
        return default_socket_path(profile)
    return Path(raw).expanduser()


def debug_safe(value: object, depth: int = 0) -> object:
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, str):
        return value if len(value) <= 300_000 else value[:300_000] + "\n...[truncated]"
    if isinstance(value, list):
        return [debug_safe(item, depth + 1) for item in value]
    if isinstance(value, dict):
        captured: dict[str, object] = {}
        for key, item in value.items():
            if key == "data" and isinstance(item, str) and len(item) > 1000:
                captured[key] = "[base64 omitted]"
            else:
                captured[key] = debug_safe(item, depth + 1)
        return captured
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(name): ("[redacted]" if str(name).lower() in _SENSITIVE_HEADERS else str(value))
        for name, value in headers.items()
    }


def extract_tool_names(payload: Mapping[str, object] | None) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    names: set[str] = set()
    for key in ("tools", "functions"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            function = entry.get("function")
            if not isinstance(name, str) and isinstance(function, Mapping):
                name = function.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return sorted(names)


class _BodyDecoder:
    def __init__(self, content_encoding: str) -> None:
        encoding = (content_encoding or "identity").strip().lower()
        self._mode = "identity"
        self._decoder: Any = None
        if encoding in {"gzip", "x-gzip"}:
            self._mode = "zlib"
            self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._mode = "zlib"
            self._decoder = zlib.decompressobj()
        elif encoding not in {"", "identity"}:
            self._mode = "base64"

    @staticmethod
    def _text_encoding(data: bytes) -> str:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "base64"
        return "utf-8"

    def feed(self, data: bytes) -> tuple[bytes, str]:
        if self._mode == "zlib" and self._decoder is not None:
            try:
                decoded = self._decoder.decompress(data)
            except zlib.error:
                self._mode = "base64"
                return data, "base64"
            return decoded, self._text_encoding(decoded)
        if self._mode == "base64":
            return data, "base64"
        return data, self._text_encoding(data)

    def flush(self) -> tuple[bytes, str]:
        if self._mode == "zlib" and self._decoder is not None:
            try:
                decoded = self._decoder.flush()
            except zlib.error:
                return b"", "utf-8"
            return decoded, self._text_encoding(decoded)
        return b"", "utf-8"


@dataclass(eq=False)
class _Client:
    writer: asyncio.StreamWriter
    queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_CLIENT_QUEUE_EVENTS)
    )


class ProxyDebugStream:
    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        profile: str,
        *,
        ring_max_bytes: int = DEFAULT_RING_MAX_BYTES,
        ring_max_events: int = DEFAULT_RING_MAX_EVENTS,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        max_clients: int = DEFAULT_MAX_CLIENTS,
    ) -> None:
        self.path = Path(socket_path).expanduser()
        self.profile = profile
        self.ring_max_bytes = ring_max_bytes
        self.ring_max_events = ring_max_events
        self.max_event_bytes = max_event_bytes
        self.max_clients = max_clients
        self._server: asyncio.Server | None = None
        self._clients: set[_Client] = set()
        self._ring: deque[tuple[int, bytes]] = deque()
        self._ring_bytes = 0
        self._seq = 0
        self._socket_inode: int | None = None

    def _timestamp(self) -> str:
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _record(self, event: str, **fields: Any) -> tuple[int, bytes]:
        seq = self._seq + 1
        self._seq = seq
        payload = fields.get("payload")
        if payload is not None:
            fields["payload"] = debug_safe(payload)
        record = {
            "schema": SCHEMA,
            "seq": seq,
            "ts": self._timestamp(),
            "event": event,
            "profile": self.profile,
            "pid": os.getpid(),
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(line) > self.max_event_bytes:
            if "payload" in record:
                record["payload"] = None
                record["payload_truncated"] = True
            if "data" in record and isinstance(record["data"], str):
                record["data"] = record["data"][:100_000]
                record["data_truncated"] = True
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        return seq, line

    def publish(self, event: str, **fields: Any) -> None:
        headers = fields.get("headers")
        if isinstance(headers, Mapping):
            fields["headers"] = redact_headers(headers)
        try:
            seq, line = self._record(event, **fields)
        except Exception:
            LOGGER.exception("debug event serialization failed event=%s", event)
            return
        self._ring.append((seq, line))
        self._ring_bytes += len(line)
        while self._ring and (
            len(self._ring) > self.ring_max_events or self._ring_bytes > self.ring_max_bytes
        ):
            _old_seq, old_line = self._ring.popleft()
            self._ring_bytes -= len(old_line)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(line)
            except asyncio.QueueFull:
                LOGGER.warning("dropping slow proxy debug client profile=%s", self.profile)
                self._drop_client(client)

    def publish_request(
        self,
        request_id: str,
        *,
        path: str,
        requested_model: str,
        upstream_model: str,
        upstream_url: str,
        payload: Mapping[str, object],
        forwarded: bytes,
    ) -> None:
        self.publish(
            "request",
            request_id=request_id,
            path=path,
            requested_model=requested_model,
            upstream_model=upstream_model,
            upstream_url=upstream_url,
            body_bytes=len(forwarded),
            body_sha256=hashlib.sha256(forwarded).hexdigest(),
            tool_names=extract_tool_names(payload),
            payload=payload,
        )

    def begin_response(
        self,
        request_id: str,
        *,
        status: int,
        headers: Mapping[str, str],
    ) -> "DebugResponse":
        self.publish(
            "response_start",
            request_id=request_id,
            status=status,
            headers=redact_headers(headers),
            content_encoding=headers.get("content-encoding", ""),
        )
        return DebugResponse(self, request_id, status, headers.get("content-encoding", ""))

    def publish_error(self, request_id: str, *, status: int, error: str) -> None:
        self.publish(
            "error",
            request_id=request_id,
            status=status,
            error=error,
        )

    async def start(self) -> None:
        parent = self.path.parent
        self._prepare_directory(parent)
        self._prepare_socket(self.path)
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.path))
        self._socket_inode = self.path.stat().st_ino
        os.chmod(self.path, 0o600)
        LOGGER.info("proxy debug stream listening path=%s profile=%s", self.path, self.profile)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        for client in list(self._clients):
            self._drop_client(client)
        self._clients.clear()
        if server is not None:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            info = self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (
                self._socket_inode is None or info.st_ino == self._socket_inode
            ):
                self.path.unlink()
        self._socket_inode = None

    def _prepare_directory(self, parent: Path) -> None:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"debug socket parent is not a directory: {parent}")
        if info.st_uid != os.getuid():
            raise RuntimeError(f"debug socket directory owned by uid {info.st_uid}: {parent}")
        os.chmod(parent, 0o700)

    def _prepare_socket(self, path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError(f"refusing to replace non-socket path {path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"another proxy debug stream is listening on {path}: {exc}") from exc
            else:
                raise RuntimeError(f"another proxy debug stream is listening on {path}")
            finally:
                probe.close()

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            raw = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        except OSError:
            return None

    async def _send(self, client: _Client, line: bytes) -> None:
        client.writer.write(line)
        await client.writer.drain()

    def _hello_line(self, snapshot: list[tuple[int, bytes]]) -> bytes:
        ring_start = snapshot[0][0] if snapshot else self._seq + 1
        ring_end = snapshot[-1][0] if snapshot else self._seq
        record = {
            "schema": SCHEMA,
            "seq": self._seq,
            "ts": self._timestamp(),
            "event": "hello",
            "profile": self.profile,
            "pid": os.getpid(),
            "socket": str(self.path),
            "ring_start_seq": ring_start,
            "ring_end_seq": ring_end,
            "queued_events": len(snapshot),
            "max_event_bytes": self.max_event_bytes,
        }
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        uid = self._peer_uid(writer)
        if uid is not None and uid != os.getuid():
            LOGGER.warning("rejected proxy debug peer uid=%s", uid)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if len(self._clients) >= self.max_clients:
            writer.write(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "event": "error",
                        "profile": self.profile,
                        "error": "too many debug clients",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            with contextlib.suppress(Exception):
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            return
        snapshot = list(self._ring)
        client = _Client(writer=writer)
        self._clients.add(client)
        disconnect = asyncio.create_task(self._wait_disconnect(reader))
        try:
            await self._send(client, self._hello_line(snapshot))
            for _seq, line in snapshot:
                await self._send(client, line)
            while True:
                getter = asyncio.create_task(client.queue.get())
                done, _pending = await asyncio.wait(
                    {getter, disconnect}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect in done:
                    getter.cancel()
                    break
                line = getter.result()
                await self._send(client, line)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            disconnect.cancel()
            with contextlib.suppress(Exception):
                await disconnect
            self._clients.discard(client)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _wait_disconnect(self, reader: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            while await reader.read(4096):
                pass

    def _drop_client(self, client: _Client) -> None:
        self._clients.discard(client)
        with contextlib.suppress(Exception):
            client.writer.close()


class DebugResponse:
    def __init__(
        self,
        stream: ProxyDebugStream,
        request_id: str,
        status: int,
        content_encoding: str,
    ) -> None:
        self.stream = stream
        self.request_id = request_id
        self.status = status
        self._decoder = _BodyDecoder(content_encoding)
        self._started = time.monotonic()
        self._bytes = 0
        self._ended = False

    def chunk(self, data: bytes) -> None:
        if not data or self._ended:
            return
        self._bytes += len(data)
        decoded, encoding = self._decoder.feed(data)
        if not decoded:
            return
        payload = (
            decoded.decode("utf-8", "replace")
            if encoding == "utf-8"
            else base64.b64encode(decoded).decode("ascii")
        )
        self.stream.publish(
            "response_chunk",
            request_id=self.request_id,
            data=payload,
            encoding=encoding,
            bytes=len(data),
        )

    def end(self, *, truncated: bool = False) -> None:
        if self._ended:
            return
        self._ended = True
        decoded, encoding = self._decoder.flush()
        if decoded:
            payload = (
                decoded.decode("utf-8", "replace")
                if encoding == "utf-8"
                else base64.b64encode(decoded).decode("ascii")
            )
            self.stream.publish(
                "response_chunk",
                request_id=self.request_id,
                data=payload,
                encoding=encoding,
                bytes=0,
            )
        self.stream.publish(
            "response_end",
            request_id=self.request_id,
            status=self.status,
            bytes=self._bytes,
            duration_ms=int((time.monotonic() - self._started) * 1000),
            truncated=truncated,
        )

    def error(self, *, status: int, error: str) -> None:
        if self._ended:
            return
        self._ended = True
        self.stream.publish_error(self.request_id, status=status, error=error)
