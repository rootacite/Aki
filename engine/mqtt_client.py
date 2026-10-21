"""Minimal asyncio MQTT 3.1.1 client (anonymous, QoS 0/1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Awaitable, Callable

CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
SUBSCRIBE = 8
SUBACK = 9
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

MAX_PACKET_BYTES = 4 * 1024 * 1024


class MqttError(RuntimeError):
    pass


class MqttConnectionLost(MqttError):
    pass


def encode_remaining_length(value: int) -> bytes:
    if value < 0 or value > 268_435_455:
        raise ValueError("invalid MQTT remaining length")
    out = bytearray()
    while True:
        digit = value % 128
        value //= 128
        if value:
            digit |= 0x80
        out.append(digit)
        if not value:
            return bytes(out)


def encode_utf8(value: str) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("MQTT UTF-8 field too long")
    return len(raw).to_bytes(2, "big") + raw


class MqttClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str | None = None,
        keepalive: int = 30,
        clean_session: bool = True,
        on_message: Callable[[str, bytes, bool], Awaitable[None]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id or f"akid-{uuid.uuid4().hex[:12]}"
        self.keepalive = keepalive
        self.clean_session = clean_session
        self.on_message = on_message
        self.logger = logger or logging.getLogger("akid.mqtt")

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._next_packet_id = 0
        self._connack: asyncio.Future[int] | None = None
        self._subacks: dict[int, asyncio.Future[int]] = {}
        self._pubacks: dict[int, asyncio.Future[None]] = {}
        self._closed = asyncio.Event()
        self._closing = False
        self._last_error: str | None = None
        self._connected_at: float | None = None
        self._session_present = False
        self._replaying = False

    @property
    def connected(self) -> bool:
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and not self._closed.is_set()
        )

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def connected_at(self) -> float | None:
        return self._connected_at

    @property
    def session_present(self) -> bool:
        return self._session_present

    def mark_replay_complete(self) -> None:
        self._replaying = False

    async def connect(self, timeout: float = 12.0) -> None:
        self._closed.clear()
        self._closing = False
        self._last_error = None
        self._connack = asyncio.get_running_loop().create_future()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise MqttError(f"connect to {self.host}:{self.port} failed") from exc
        self._reader_task = asyncio.create_task(self._read_loop(), name="mqtt-reader")
        connect_body = (
            encode_utf8("MQTT")
            + bytes([4, 0x02 if self.clean_session else 0x00])
            + self.keepalive.to_bytes(2, "big")
            + encode_utf8(self.client_id)
        )
        await self._send(CONNECT << 4, connect_body)
        try:
            return_code = await asyncio.wait_for(self._connack, timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise MqttError("CONNACK timeout") from exc
        if return_code != 0:
            await self.close()
            raise MqttError(f"broker refused connection: code {return_code}")
        self._connected_at = time.time()
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="mqtt-keepalive"
        )

    async def subscribe(self, topic: str, qos: int = 1, timeout: float = 10.0) -> int:
        if not self.connected:
            raise MqttConnectionLost("not connected")
        packet_id = self._packet_id()
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._subacks[packet_id] = future
        body = packet_id.to_bytes(2, "big") + encode_utf8(topic) + bytes([qos & 0x03])
        await self._send((SUBSCRIBE << 4) | 0x02, body)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MqttError("SUBACK timeout") from exc
        finally:
            self._subacks.pop(packet_id, None)

    async def publish(
        self, topic: str, payload: bytes, qos: int = 1, timeout: float = 10.0
    ) -> None:
        if not self.connected:
            raise MqttConnectionLost("not connected")
        if len(payload) > MAX_PACKET_BYTES:
            raise MqttError("MQTT payload too large")
        packet_id = self._packet_id()
        body = encode_utf8(topic)
        if qos > 0:
            body += packet_id.to_bytes(2, "big")
        body += payload
        header = (PUBLISH << 4) | ((qos & 0x03) << 1)
        if qos == 1:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._pubacks[packet_id] = future
        else:
            future = None
        await self._send(header, body)
        if future is None:
            return
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MqttError("PUBACK timeout") from exc
        finally:
            self._pubacks.pop(packet_id, None)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        self._closing = True
        if self.connected:
            with contextlib.suppress(Exception):
                await self._send(DISCONNECT << 4, b"")
        for task in (self._keepalive_task, self._reader_task):
            if task is not None:
                task.cancel()
        for task in (self._keepalive_task, self._reader_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._keepalive_task = None
        self._reader_task = None
        self._shutdown("closed")

    def _packet_id(self) -> int:
        self._next_packet_id = (self._next_packet_id % 65535) + 1
        return self._next_packet_id

    async def _send(self, header: int, body: bytes) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise MqttConnectionLost("not connected")
        packet = bytes([header]) + encode_remaining_length(len(body)) + body
        async with self._write_lock:
            try:
                writer.write(packet)
                await writer.drain()
            except (OSError, RuntimeError) as exc:
                raise MqttConnectionLost("write failed") from exc

    async def _read_loop(self) -> None:
        reason = "connection closed"
        try:
            while True:
                header, body = await self._read_packet()
                packet_type = header >> 4
                if packet_type == CONNACK:
                    if len(body) < 2:
                        raise MqttError("invalid CONNACK")
                    self._session_present = bool(body[0] & 0x01)
                    self._replaying = self._session_present
                    if self._connack is not None and not self._connack.done():
                        self._connack.set_result(body[1])
                elif packet_type == SUBACK:
                    if len(body) >= 2:
                        packet_id = int.from_bytes(body[:2], "big")
                        future = self._subacks.get(packet_id)
                        if future is not None and not future.done():
                            future.set_result(body[2] if len(body) > 2 else 0x80)
                elif packet_type == PUBACK:
                    if len(body) >= 2:
                        packet_id = int.from_bytes(body[:2], "big")
                        future = self._pubacks.get(packet_id)
                        if future is not None and not future.done():
                            future.set_result(None)
                elif packet_type == PUBLISH:
                    await self._handle_publish(header, body)
                elif packet_type == PINGRESP:
                    continue
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError, MqttError) as exc:
            reason = str(exc) or exc.__class__.__name__
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"mqtt reader failed: {exc}"
        finally:
            if not self._closing:
                self.logger.warning("mqtt connection lost: %s", reason)
            self._shutdown(reason)

    async def _read_packet(self) -> tuple[int, bytes]:
        assert self._reader is not None
        header = (await self._reader.readexactly(1))[0]
        multiplier = 1
        remaining = 0
        for _ in range(4):
            byte = (await self._reader.readexactly(1))[0]
            remaining += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        else:
            raise MqttError("invalid remaining length")
        if remaining > MAX_PACKET_BYTES:
            raise MqttError("incoming MQTT packet too large")
        body = await self._reader.readexactly(remaining) if remaining else b""
        return header, body

    async def _handle_publish(self, header: int, body: bytes) -> None:
        qos = (header >> 1) & 0x03
        if qos == 3:
            raise MqttError("invalid PUBLISH QoS")
        if len(body) < 2:
            raise MqttError("invalid PUBLISH")
        topic_len = int.from_bytes(body[:2], "big")
        cursor = 2
        if len(body) < cursor + topic_len:
            raise MqttError("invalid PUBLISH topic")
        topic = body[cursor : cursor + topic_len].decode("utf-8", "replace")
        cursor += topic_len
        packet_id = None
        if qos > 0:
            if len(body) < cursor + 2:
                raise MqttError("invalid PUBLISH packet id")
            packet_id = int.from_bytes(body[cursor : cursor + 2], "big")
            cursor += 2
        payload = body[cursor:]
        if qos == 1 and packet_id is not None:
            await self._send((PUBACK << 4), packet_id.to_bytes(2, "big"))
        if self.on_message is not None:
            result = self.on_message(topic, payload, self._replaying)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    async def _keepalive_loop(self) -> None:
        interval = max(5, self.keepalive // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                await self._send(PINGREQ << 4, b"")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self.logger.debug("mqtt keepalive failed: %s", exc)
            self._shutdown("keepalive failed")

    def _shutdown(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._last_error = reason if not self._closing else None
        self._closed.set()
        if self._connack is not None and not self._connack.done():
            self._connack.set_exception(MqttConnectionLost(reason))
        for future in list(self._subacks.values()) + list(self._pubacks.values()):
            if not future.done():
                future.set_exception(MqttConnectionLost(reason))
        writer = self._writer
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        self._writer = None
        self._reader = None
