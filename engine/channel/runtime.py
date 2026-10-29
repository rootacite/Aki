"""Connect to the broker, receive packets, and drop duplicates."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from engine.aki_config import atomic_write
from engine.mqtt_client import (
    MqttClient,
    MqttError,
)
from engine.tokifactor import (
    AVATAR_TOPIC,
    CONTROL_TOPIC,
    ImageChunkAssembler,
    PICTURE_TOPIC,
    TextChunkAssembler,
    crypto_available,
    decode_single_payload,
    looks_like_chunk,
    parse_text_chunk,
)
from .constants import (
    CHANNEL_INBOX_MAX,
    DEDUP_MAX_IDS,
    DEFAULT_MAGIC,
    REPLAY_GRACE_SECONDS,
    TEXT_TOPIC,
)

class RuntimeMixin:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        magic: str = DEFAULT_MAGIC,
        topic: str = TEXT_TOPIC,
        sender_name: str = "akid",
        sender_id: str = "akid",
        session_magic: str | None = None,
        avatar_bytes: bytes | None = None,
        avatar_hash: str = "",
        avatar_mime: str = "",
        dedup_path: Path | None = None,
        avatar_cache_path: Path | None = None,
        on_text: TextHandler,
        on_image: ImageHandler | None = None,
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
        self.session_magic = session_magic or magic
        material = (
            f"{sender_id}\x00{host}\x00{port}\x00{topic}\x00{self.session_magic}"
        )
        self.session_client_id = "akid-" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:16]
        self._avatar_bytes = avatar_bytes
        self._avatar_hash = avatar_hash or (
            hashlib.sha256(avatar_bytes).hexdigest() if avatar_bytes else ""
        )
        self._avatar_mime = avatar_mime
        self.on_text = on_text
        self.on_image = on_image
        self.on_control = on_control
        self.on_connected = on_connected
        self.logger = logger or logging.getLogger("akid.channel")

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._client: MqttClient | None = None
        self._inbox: asyncio.Queue[
            tuple[str, str, str, dict[str, str], str, int, bool]
        ] = asyncio.Queue(maxsize=CHANNEL_INBOX_MAX)
        self._publish_lock = asyncio.Lock()
        self._image_lock = asyncio.Lock()
        self._avatar_publish_task: asyncio.Task[None] | None = None
        self._assembler = TextChunkAssembler(magic)
        self._image_assembler = ImageChunkAssembler(magic)
        self._recent: dict[str, float] = {}
        self.dedup_path = dedup_path
        self.avatar_cache_path = avatar_cache_path
        self._seen_message_ids: list[str] = []
        self._seen_message_id_set: set[str] = set()
        self._duplicates_dropped = 0
        self._load_dedup()
        self._state = "stopped"
        self._connected = False
        self._session_present = False
        self._last_error: str | None = None
        self._last_message_at: float | None = None
        self._messages_received = 0
        self._replies_sent = 0
        self._decode_failures = 0
        self._images_sent = 0
        self._images_received = 0
        self._seen_image_transfers: list[str] = []
        self._seen_image_transfer_set: set[str] = set()
        self._last_image_at: float | None = None
        self._avatar_published = False
        self._last_published_avatar_hash: str | None = None
        self._avatar_last_error: str | None = None
        self._avatars_received = 0
        self._peer_avatars: dict[str, bytes] = {}
        self._peer_avatar_hashes: dict[str, str] = {}
        self._avatar_wants_sent: dict[tuple[str, str], float] = {}
        self._avatar_haves_served: dict[str, float] = {}
        self._avatar_wants_sent_count = 0
        self._avatar_haves_received = 0
        self._avatar_wants_received = 0
        self._control_received = 0
        self._control_sent = 0
        self._load_peer_avatars()


    @property
    def connected(self) -> bool:
        return self._connected


    @property
    def state(self) -> str:
        return self._state


    def set_active_magic(self, magic: str) -> None:
        """Switch the Tokifactor encryption identity without reconnecting MQTT."""
        if not magic:
            raise ValueError("active magic must not be empty")
        self.magic = magic
        self._assembler = TextChunkAssembler(magic)
        self._image_assembler = ImageChunkAssembler(magic)


    def discard_inbox(self) -> int:
        dropped = 0
        while True:
            try:
                self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._inbox.task_done()
            dropped += 1
        return dropped


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
                client_id=self.session_client_id,
                clean_session=False,
                on_message=self._on_packet,
                logger=self.logger,
            )
            self._client = client
            self._state = "connecting"
            try:
                await client.connect()
                self._session_present = client.session_present
                granted = await client.subscribe(self.topic, qos=1)
                if granted == 0x80:
                    raise MqttError("broker rejected subscription")
                try:
                    avatar_granted = await client.subscribe(AVATAR_TOPIC, qos=0)
                    if avatar_granted == 0x80:
                        raise MqttError("broker rejected avatar subscription")
                except MqttError as exc:
                    self._avatar_last_error = f"avatar subscription failed: {exc}"
                    self.logger.warning(self._avatar_last_error)
                try:
                    picture_granted = await client.subscribe(PICTURE_TOPIC, qos=1)
                    if picture_granted == 0x80:
                        raise MqttError("broker rejected picture subscription")
                except MqttError as exc:
                    self.logger.warning("picture subscription failed: %s", exc)
                try:
                    control_granted = await client.subscribe(CONTROL_TOPIC, qos=0)
                    if control_granted == 0x80:
                        raise MqttError("broker rejected control subscription")
                except MqttError as exc:
                    self.logger.warning("control subscription failed: %s", exc)
                if client.session_present:
                    await asyncio.sleep(REPLAY_GRACE_SECONDS)
                client.mark_replay_complete()
                self._connected = True
                self._state = "connected"
                self._last_error = None
                backoff = 1.0
                self.logger.info(
                    "channel connected %s:%s topic=%s", self.host, self.port, self.topic
                )
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


    async def _on_packet(self, topic: str, payload: bytes, replayed: bool = False) -> None:
        if self._stop.is_set():
            return
        if topic == AVATAR_TOPIC:
            await self._handle_avatar_packet(payload)
            return
        if topic == CONTROL_TOPIC:
            await self._handle_control_packet(payload)
            return
        if topic == PICTURE_TOPIC:
            await self._handle_picture_packet(payload, replayed)
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
        await self._note_peer_avatar(
            envelope.sender_id,
            envelope.avatar_hash,
            replayed=replayed,
        )
        if not envelope.text:
            return
        if self._is_duplicate(envelope):
            self._duplicates_dropped += 1
            return
        self._messages_received += 1
        self._last_message_at = time.time()
        try:
            metadata = {
                str(key): str(value)
                for key, value in envelope.metadata.items()
                if value is not None
            }
            self._inbox.put_nowait(
                (
                    envelope.sender,
                    envelope.sender_id,
                    envelope.text,
                    metadata,
                    envelope.message_id,
                    envelope.sent_at,
                    replayed,
                )
            )
        except asyncio.QueueFull:
            self.logger.warning("channel inbox full; dropping message")


    async def _handle_picture_packet(self, payload: bytes, replayed: bool = False) -> None:
        if self.on_image is None:
            return
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._decode_failures += 1
            return
        chunk = parse_text_chunk(text)
        if chunk is None:
            self._decode_failures += 1
            return
        if chunk.get("abort"):
            self._image_assembler.add(chunk)
            return
        if str(chunk.get("sender_id") or "") == self.sender_id:
            return
        await self._note_peer_avatar(
            str(chunk.get("sender_id") or ""),
            str(chunk.get("avatar_hash") or ""),
            replayed=replayed,
        )
        image = self._image_assembler.add(chunk)
        if image is None:
            return
        if image.transfer_id and image.transfer_id in self._seen_image_transfer_set:
            return
        if image.transfer_id:
            self._seen_image_transfers.append(image.transfer_id)
            self._seen_image_transfer_set.add(image.transfer_id)
            if len(self._seen_image_transfers) > DEDUP_MAX_IDS:
                removed = self._seen_image_transfers[:-DEDUP_MAX_IDS]
                self._seen_image_transfers = self._seen_image_transfers[-DEDUP_MAX_IDS:]
                for value in removed:
                    self._seen_image_transfer_set.discard(value)
        self._images_received += 1
        try:
            await self.on_image(image, replayed)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("channel image handler failed")


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
        await self._note_peer_avatar(envelope.sender_id, envelope.avatar_hash)
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
        if envelope.message_id:
            if envelope.message_id in self._seen_message_id_set:
                return True
            self._remember_message_id(envelope.message_id)
            return False
        now = time.monotonic()
        cutoff = now - 10.0
        self._recent = {key: value for key, value in self._recent.items() if value >= cutoff}
        key = hashlib.sha256(
            (
                envelope.sender_id
                + "\x00"
                + str(envelope.sent_at)
                + "\x00"
                + envelope.text
            ).encode("utf-8")
        ).hexdigest()
        if key in self._recent:
            return True
        self._recent[key] = now
        return False


    def _load_dedup(self) -> None:
        if self.dedup_path is None or not self.dedup_path.is_file():
            return
        try:
            payload = json.loads(self.dedup_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        values = payload.get("ids") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return
        for value in values[-DEDUP_MAX_IDS:]:
            if isinstance(value, str) and value and value not in self._seen_message_id_set:
                self._seen_message_ids.append(value)
                self._seen_message_id_set.add(value)


    def _remember_message_id(self, message_id: str) -> None:
        self._seen_message_ids.append(message_id)
        self._seen_message_id_set.add(message_id)
        if len(self._seen_message_ids) > DEDUP_MAX_IDS:
            removed = self._seen_message_ids[:-DEDUP_MAX_IDS]
            self._seen_message_ids = self._seen_message_ids[-DEDUP_MAX_IDS:]
            for value in removed:
                self._seen_message_id_set.discard(value)
        if self.dedup_path is None:
            return
        try:
            atomic_write(
                self.dedup_path,
                json.dumps(
                    {"version": 1, "ids": self._seen_message_ids},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        except OSError as exc:
            self.logger.warning("failed to persist channel dedup state: %s", exc)
