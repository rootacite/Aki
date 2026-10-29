"""Cache peer avatars and answer avatar-hash requests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from engine.aki_config import atomic_write
from engine.tokifactor import (
    AVATAR_TOPIC,
    CONTROL_TOPIC,
    DecodeError,
    decrypt_bytes,
    encode_avatar_have,
    encode_avatar_want,
    encrypt_bytes,
    parse_avatar_envelope,
)
from .constants import (
    AVATAR_HAVE_TTL_SECONDS,
    AVATAR_WANT_TTL_SECONDS,
    REPLAY_GRACE_SECONDS,
)

class AvatarsMixin:

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
                self._avatar_wants_received += 1
                self._schedule_avatar_have(envelope.hash)
            return
        if envelope.id == self.sender_id:
            return
        if not envelope.hash or not envelope.data:
            self._peer_avatars.pop(envelope.id, None)
            self._peer_avatar_hashes.pop(envelope.id, None)
            self._save_peer_avatars()
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
        self._peer_avatar_hashes[envelope.id] = envelope.hash.lower()
        self._avatar_haves_received += 1
        self._avatars_received += 1
        self._save_peer_avatars()


    def _avatar_cache_index_path(self) -> Path | None:
        if self.avatar_cache_path is None:
            return None
        return self.avatar_cache_path / "index.json"


    def _avatar_cache_file(self, peer: str) -> Path:
        assert self.avatar_cache_path is not None
        name = hashlib.sha256(peer.encode("utf-8")).hexdigest() + ".img"
        return self.avatar_cache_path / name


    def _load_peer_avatars(self) -> None:
        index = self._avatar_cache_index_path()
        if index is None or not index.is_file():
            return
        try:
            payload = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = payload.get("avatars") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return
        root = self.avatar_cache_path
        assert root is not None
        for peer, item in entries.items():
            if not isinstance(peer, str) or not isinstance(item, dict):
                continue
            digest = str(item.get("hash") or "").strip().lower()
            filename = str(item.get("file") or "").strip()
            if not digest or not filename or Path(filename).name != filename:
                continue
            try:
                data = (root / filename).read_bytes()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                continue
            self._peer_avatars[peer] = data
            self._peer_avatar_hashes[peer] = digest


    def _save_peer_avatars(self) -> None:
        root = self.avatar_cache_path
        index = self._avatar_cache_index_path()
        if root is None or index is None:
            return
        try:
            root.mkdir(parents=True, exist_ok=True)
            entries: dict[str, dict[str, str]] = {}
            for peer, digest in self._peer_avatar_hashes.items():
                data = self._peer_avatars.get(peer)
                if data is None:
                    continue
                target = self._avatar_cache_file(peer)
                temp = target.with_name(target.name + ".tmp")
                temp.write_bytes(data)
                temp.replace(target)
                entries[peer] = {"hash": digest, "file": target.name}
            atomic_write(
                index,
                json.dumps(
                    {"version": 1, "avatars": entries},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        except OSError as exc:
            self.logger.warning("failed to persist peer avatars: %s", exc)


    async def _note_peer_avatar(
        self,
        sender_id: str,
        avatar_hash: str,
        *,
        replayed: bool = False,
    ) -> None:
        peer = str(sender_id or "").strip()
        digest = str(avatar_hash or "").strip().lower()
        if not peer or not digest or peer == self.sender_id:
            return
        known = self._peer_avatar_hashes.get(peer)
        if known == digest and peer in self._peer_avatars:
            return
        if known != digest:
            self._peer_avatars.pop(peer, None)
        self._peer_avatar_hashes[peer] = digest
        if replayed:
            return
        now = time.monotonic()
        key = (peer, digest)
        if now - self._avatar_wants_sent.get(key, 0.0) < AVATAR_WANT_TTL_SECONDS:
            return
        client = self._client
        if client is None or not self._connected:
            return
        self._avatar_wants_sent[key] = now
        try:
            await client.publish(
                AVATAR_TOPIC,
                encode_avatar_want(peer, digest).encode("utf-8"),
                qos=0,
            )
            self._avatar_wants_sent_count += 1
        except Exception as exc:
            self._avatar_wants_sent.pop(key, None)
            self.logger.debug("avatar want failed peer=%s error=%s", peer, exc)


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
            await client.publish(AVATAR_TOPIC, payload.encode("utf-8"), qos=0)
        except Exception as exc:
            self._avatar_last_error = f"avatar publish failed: {exc}"
            self.logger.warning(self._avatar_last_error)
            return False
        self._avatar_published = True
        self._last_published_avatar_hash = self._avatar_hash
        self._avatar_last_error = None
        self.logger.info("avatar published id=%s hash=%s", self.sender_id, self._avatar_hash)
        return True


    def _schedule_avatar_have(self, requested_hash: str = "") -> None:
        key = str(requested_hash or self._avatar_hash).strip().lower()
        now = time.monotonic()
        if key and now - self._avatar_haves_served.get(key, 0.0) < AVATAR_HAVE_TTL_SECONDS:
            return
        if key:
            self._avatar_haves_served[key] = now
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
            "duplicates_dropped": self._duplicates_dropped,
            "replies_sent": self._replies_sent,
            "decode_failures": self._decode_failures,
            "images_sent": self._images_sent,
            "images_received": self._images_received,
            "last_image_at": self._last_image_at,
            "files_sent": self._files_sent,
            "files_received": self._files_received,
            "last_file_at": self._last_file_at,
            "session": {
                "client_id": self.session_client_id,
                "persistent": True,
                "session_present": self._session_present,
                "replay_grace_s": REPLAY_GRACE_SECONDS,
                "dedup_path": str(self.dedup_path) if self.dedup_path else None,
                "dedup_ids": len(self._seen_message_id_set),
            },
            "avatar": {
                "configured": bool(self._avatar_bytes),
                "sender_id": self.sender_id,
                "hash": self._avatar_hash or None,
                "bytes": len(self._avatar_bytes or b""),
                "mime": self._avatar_mime or None,
                "published": self._avatar_published,
                "last_published_hash": self._last_published_avatar_hash,
                "peer_avatars": len(self._peer_avatars),
                "peer_avatar_hashes": len(self._peer_avatar_hashes),
                "cache_path": (
                    str(self.avatar_cache_path) if self.avatar_cache_path else None
                ),
                "avatars_received": self._avatars_received,
                "haves_received": self._avatar_haves_received,
                "wants_sent": self._avatar_wants_sent_count,
                "wants_received": self._avatar_wants_received,
                "last_error": self._avatar_last_error,
            },
            "control": {
                "topic": CONTROL_TOPIC,
                "messages_received": self._control_received,
                "messages_sent": self._control_sent,
            },
        }
