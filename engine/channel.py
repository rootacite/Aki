"""Persistent tokifactor MQTT channel adapter for akid."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable, Mapping

from engine.mqtt_client import MqttClient, MqttError
from engine.tokifactor import (
    AVATAR_TOPIC,
    CONTROL_TOPIC,
    DecodeError,
    MAX_IMAGE_BYTES,
    PICTURE_TOPIC,
    TextChunkAssembler,
    TextEnvelope,
    crypto_available,
    decrypt_bytes,
    decode_single_payload,
    detect_image_mime,
    encode_avatar_have,
    encode_picture_abort,
    encode_picture_payloads,
    encode_text_payloads,
    encrypt_bytes,
    looks_like_chunk,
    parse_avatar_envelope,
    parse_text_chunk,
)

TEXT_TOPIC = "tokifactor/text"
DEFAULT_MAGIC = "akid-default-channel"

TextHandler = Callable[[str, str, str], Awaitable[None]]
ControlHandler = Callable[[TextEnvelope], Awaitable[None]]


class ChannelUnavailable(RuntimeError):
    pass


class TokifactorChannel:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        magic: str = DEFAULT_MAGIC,
        topic: str = TEXT_TOPIC,
        sender_name: str = "akid",
        sender_id: str = "akid",
        avatar_bytes: bytes | None = None,
        avatar_hash: str = "",
        avatar_mime: str = "",
        on_text: TextHandler,
        on_control: ControlHandler | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.magic = magic
        self.topic = topic
        self.sender_name = sender_name
        self.sender_id = sender_id
        self._avatar_bytes = avatar_bytes
        self._avatar_hash = avatar_hash or (
            hashlib.sha256(avatar_bytes).hexdigest() if avatar_bytes else ""
        )
        self._avatar_mime = avatar_mime
        self.on_text = on_text
        self.on_control = on_control
        self.on_connected = on_connected
        self.logger = logger or logging.getLogger("akid.channel")

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._client: MqttClient | None = None
        self._inbox: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(maxsize=256)
        self._publish_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self._avatar_publish_task: asyncio.Task[None] | None = None
        self._assembler = TextChunkAssembler(magic)
        self._recent: dict[str, float] = {}
        self._state = "stopped"
        self._connected = False
        self._last_error: str | None = None
        self._last_message_at: float | None = None
        self._messages_received = 0
        self._replies_sent = 0
        self._decode_failures = 0
        self._images_sent = 0
        self._last_image_at: float | None = None
        self._avatar_published = False
        self._last_published_avatar_hash: str | None = None
        self._avatar_last_error: str | None = None
        self._avatars_received = 0
        self._peer_avatars: dict[str, bytes] = {}
        self._control_received = 0
        self._control_sent = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._worker = asyncio.create_task(self._worker_loop(), name="akid-channel-worker")
        self._task = asyncio.create_task(self._run(), name="akid-channel")

    async def stop(self) -> None:
        self._stop.set()
        client = self._client
        self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        for task in (self._task, self._worker):
            if task is not None:
                task.cancel()
        for task in (self._task, self._worker):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._avatar_publish_task is not None:
            self._avatar_publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._avatar_publish_task
            self._avatar_publish_task = None
        self._task = None
        self._worker = None
        self._connected = False
        self._state = "stopped"

    async def _run(self) -> None:
        if not crypto_available():
            self._state = "error"
            self._last_error = "tokifactor crypto unavailable: install 'cryptography'"
            self.logger.error(self._last_error)
            return
        backoff = 1.0
        while not self._stop.is_set():
            client = MqttClient(
                self.host,
                self.port,
                on_message=self._on_packet,
                logger=self.logger,
            )
            self._client = client
            self._state = "connecting"
            try:
                await client.connect()
                granted = await client.subscribe(self.topic, qos=1)
                if granted == 0x80:
                    raise MqttError("broker rejected subscription")
                try:
                    avatar_granted = await client.subscribe(AVATAR_TOPIC, qos=1)
                    if avatar_granted == 0x80:
                        raise MqttError("broker rejected avatar subscription")
                except MqttError as exc:
                    self._avatar_last_error = f"avatar subscription failed: {exc}"
                    self.logger.warning(self._avatar_last_error)
                try:
                    control_granted = await client.subscribe(CONTROL_TOPIC, qos=1)
                    if control_granted == 0x80:
                        raise MqttError("broker rejected control subscription")
                except MqttError as exc:
                    self.logger.warning("control subscription failed: %s", exc)
                self._connected = True
                self._state = "connected"
                self._last_error = None
                backoff = 1.0
                self.logger.info(
                    "channel connected %s:%s topic=%s", self.host, self.port, self.topic
                )
                await self._publish_avatar_have(force=True)
                if self.on_connected is not None:
                    try:
                        await self.on_connected()
                    except Exception:
                        self.logger.exception("channel connected callback failed")
                await client.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.warning("channel connection failed: %s", exc)
            finally:
                self._connected = False
                if self._client is client:
                    self._client = None
                with contextlib.suppress(Exception):
                    await client.close()
            if self._stop.is_set():
                break
            self._state = "reconnecting"
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 30.0)
        self._state = "stopped"
        self._connected = False

    async def _on_packet(self, topic: str, payload: bytes) -> None:
        if self._stop.is_set():
            return
        if topic == AVATAR_TOPIC:
            await self._handle_avatar_packet(payload)
            return
        if topic == CONTROL_TOPIC:
            await self._handle_control_packet(payload)
            return
        if topic != self.topic:
            return
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._decode_failures += 1
            return
        envelope: TextEnvelope | None
        if looks_like_chunk(text):
            chunk = parse_text_chunk(text)
            if chunk is None:
                self._decode_failures += 1
                return
            envelope = self._assembler.add(chunk)
        else:
            envelope = decode_single_payload(text, self.magic)
        if envelope is None:
            self._decode_failures += 1
            return
        if envelope.sender_id and envelope.sender_id == self.sender_id:
            return
        if not envelope.text:
            return
        if self._is_duplicate(envelope):
            return
        self._messages_received += 1
        self._last_message_at = time.time()
        try:
            self._inbox.put_nowait(
                (envelope.sender, envelope.sender_id, envelope.text)
            )
        except asyncio.QueueFull:
            self.logger.warning("channel inbox full; dropping message")

    async def _handle_control_packet(self, payload: bytes) -> None:
        if self.on_control is None:
            return
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._decode_failures += 1
            return
        envelope = decode_single_payload(text, self.magic)
        if envelope is None:
            self._decode_failures += 1
            return
        if envelope.sender_id and envelope.sender_id == self.sender_id:
            return
        if not envelope.text:
            return
        self._control_received += 1
        try:
            await self.on_control(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("channel control handler failed")

    def _is_duplicate(self, envelope: TextEnvelope) -> bool:
        now = time.monotonic()
        cutoff = now - 10.0
        self._recent = {key: value for key, value in self._recent.items() if value >= cutoff}
        key = hashlib.sha256(
            (envelope.sender_id + "\x00" + envelope.text).encode("utf-8")
        ).hexdigest()
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    async def _worker_loop(self) -> None:
        while True:
            sender, sender_id, text = await self._inbox.get()
            try:
                await self.on_text(sender, sender_id, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("channel message handler failed")

    async def publish_text(
        self, text: str, metadata: Mapping[str, Any] | None = None
    ) -> None:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        payloads = encode_text_payloads(
            text,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            metadata=metadata,
        )
        async with self._publish_lock:
            for payload in payloads:
                await client.publish(self.topic, payload.encode("utf-8"), qos=1)
        self._replies_sent += 1

    async def publish_structured(
        self, text: str, metadata: Mapping[str, Any]
    ) -> None:
        await self.publish_text(text, metadata)

    async def publish_control(
        self, text: str, metadata: Mapping[str, Any]
    ) -> None:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        payloads = encode_text_payloads(
            text,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            metadata=metadata,
        )
        async with self._publish_lock:
            for payload in payloads:
                await client.publish(CONTROL_TOPIC, payload.encode("utf-8"), qos=1)
        self._control_sent += 1

    async def publish_notice(self, text: str) -> None:
        with contextlib.suppress(ChannelUnavailable):
            await self.publish_text(text)

    async def publish_approval(self, data: dict) -> None:
        approval_id = str(data.get("approval_id") or "")
        tool_name = str(data.get("tool_name") or "unknown")
        tool_input = data.get("input")
        try:
            rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            rendered = str(tool_input)
        if len(rendered) > 1500:
            rendered = rendered[:1500] + "\n... (truncated)"
        timeout_s = data.get("timeout_s")
        lines = [
            "akid approval required",
            f"tool: {tool_name}",
            f"approval_id: {approval_id}",
        ]
        if rendered and rendered != "null":
            lines.extend(["input:", rendered])
        if timeout_s is not None:
            lines.append(f"timeout: {timeout_s}s")
        lines.extend(
            [
                "",
                f"reply: akid-approve {approval_id}",
                f"       akid-deny {approval_id}",
                "or simply: akid-approve / akid-deny (oldest pending)",
            ]
        )
        await self.publish_structured(
            "\n".join(lines),
            {
                "schema": "akid.v1",
                "kind": "approval",
                "approvalId": approval_id,
                "requesterId": str(data.get("requester_id") or ""),
                "toolName": tool_name,
                "toolCallId": str(data.get("tool_call_id") or ""),
                "timeoutS": str(timeout_s if timeout_s is not None else ""),
                "details": rendered if rendered and rendered != "null" else "",
            },
        )

    async def publish_image(
        self,
        data: bytes,
        *,
        caption: str | None = None,
        mime: str | None = None,
    ) -> dict:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        detected = mime or detect_image_mime(data)
        if detected is None:
            raise ValueError("unsupported image format (png/jpeg/gif/webp only)")
        transfer_id, payloads = encode_picture_payloads(
            data,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            caption=caption or "",
        )
        semaphore = asyncio.Semaphore(8)

        async def publish_one(payload: str) -> None:
            async with semaphore:
                await client.publish(PICTURE_TOPIC, payload.encode("utf-8"), qos=1)

        async with self._image_lock:
            try:
                await asyncio.gather(*(publish_one(payload) for payload in payloads))
            except Exception:
                with contextlib.suppress(Exception):
                    abort = encode_picture_abort(
                        transfer_id,
                        sender=self.sender_name,
                        sender_id=self.sender_id,
                        reason="fail",
                    )
                    await client.publish(PICTURE_TOPIC, abort.encode("utf-8"), qos=1)
                raise
        self._images_sent += 1
        self._last_image_at = time.time()
        return {
            "transfer_id": transfer_id,
            "bytes": len(data),
            "chunks": len(payloads),
            "mime": detected,
            "topic": PICTURE_TOPIC,
        }

    async def _handle_avatar_packet(self, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._decode_failures += 1
            return
        envelope = parse_avatar_envelope(text)
        if envelope is None:
            self._decode_failures += 1
            return
        if envelope.type == "want":
            if envelope.id == self.sender_id:
                self._schedule_avatar_have()
            return
        if envelope.id == self.sender_id:
            return
        if not envelope.hash or not envelope.data:
            self._peer_avatars.pop(envelope.id, None)
            return
        try:
            encrypted = base64.b64decode(envelope.data, validate=True)
            clear = decrypt_bytes(encrypted, self.magic)
        except (ValueError, DecodeError) as exc:
            self._avatar_last_error = f"avatar decrypt failed for {envelope.id}: {exc}"
            self.logger.warning(self._avatar_last_error)
            return
        actual = hashlib.sha256(clear).hexdigest()
        if actual.lower() != envelope.hash.lower():
            self._avatar_last_error = f"avatar hash mismatch for {envelope.id}"
            self.logger.warning(self._avatar_last_error)
            return
        self._peer_avatars[envelope.id] = clear
        self._avatars_received += 1

    async def _publish_avatar_have(self, *, force: bool = False) -> bool:
        client = self._client
        if (
            client is None
            or not self._connected
            or not self._avatar_bytes
            or not self._avatar_hash
        ):
            return False
        if not force and self._last_published_avatar_hash == self._avatar_hash:
            return True
        try:
            data = base64.b64encode(
                encrypt_bytes(self._avatar_bytes, self.magic)
            ).decode("ascii")
            payload = encode_avatar_have(self.sender_id, self._avatar_hash, data)
            await client.publish(AVATAR_TOPIC, payload.encode("utf-8"), qos=1)
        except Exception as exc:
            self._avatar_last_error = f"avatar publish failed: {exc}"
            self.logger.warning(self._avatar_last_error)
            return False
        self._avatar_published = True
        self._last_published_avatar_hash = self._avatar_hash
        self._avatar_last_error = None
        self.logger.info("avatar published id=%s hash=%s", self.sender_id, self._avatar_hash)
        return True

    def _schedule_avatar_have(self) -> None:
        if self._avatar_publish_task is not None and not self._avatar_publish_task.done():
            return
        task = asyncio.create_task(self._publish_avatar_have(force=True), name="akid-avatar-have")
        self._avatar_publish_task = task

        def clear(done: asyncio.Task[None]) -> None:
            if self._avatar_publish_task is done:
                self._avatar_publish_task = None

        task.add_done_callback(clear)

    def health(self) -> dict:
        return {
            "enabled": True,
            "state": self._state,
            "connected": self._connected,
            "host": self.host,
            "port": self.port,
            "topic": self.topic,
            "last_error": self._last_error,
            "last_message_at": self._last_message_at,
            "messages_received": self._messages_received,
            "replies_sent": self._replies_sent,
            "decode_failures": self._decode_failures,
            "images_sent": self._images_sent,
            "last_image_at": self._last_image_at,
            "avatar": {
                "configured": bool(self._avatar_bytes),
                "sender_id": self.sender_id,
                "hash": self._avatar_hash or None,
                "bytes": len(self._avatar_bytes or b""),
                "mime": self._avatar_mime or None,
                "published": self._avatar_published,
                "last_published_hash": self._last_published_avatar_hash,
                "peer_avatars": len(self._peer_avatars),
                "avatars_received": self._avatars_received,
                "last_error": self._avatar_last_error,
            },
            "control": {
                "topic": CONTROL_TOPIC,
                "messages_received": self._control_received,
                "messages_sent": self._control_sent,
            },
        }
