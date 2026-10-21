"""Wire compatibility with tokifactor's MQTT text and control channels.

tokifactor publishes UTF-8 Base64 of ``nonce || AES-GCM-SIV(ciphertext)`` on
``tokifactor/text``.  The key is HKDF-SHA256(magic_string, fixed salt,
``P2P_CHANNEL_V1``).  Short texts are a JSON TextEnvelope; longer texts are
TextChunk JSON fragments containing independently encrypted slices.
``tokifactor/control`` uses the same envelope encryption for probe, presence,
status, and offline messages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

try:  # pragma: no cover - availability is covered by the runtime check below
    from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
except ImportError as exc:  # pragma: no cover
    AESGCMSIV = None  # type: ignore[assignment]
    _CRYPTO_IMPORT_ERROR = exc
else:
    _CRYPTO_IMPORT_ERROR = None

HARDCODED_SALT = bytes(
    [
        0x1A,
        0x2B,
        0x3C,
        0x4D,
        0x5E,
        0x6F,
        0x70,
        0x71,
        0x01,
        0x02,
        0x03,
        0x04,
        0x05,
        0x06,
        0x07,
        0x08,
    ]
)
HKDF_INFO = b"P2P_CHANNEL_V1"
NONCE_BYTES = 12
TAG_BITS = 128
TEXT_CHUNK_THRESHOLD_BYTES = 4 * 1024
TEXT_CHUNK_BYTES = 8 * 1024
MAX_INBOUND_CHUNKS = 512
MAX_INBOUND_CHUNK_BYTES = 2 * 1024 * 1024
INCOMPLETE_TIMEOUT_S = 5.0
PICTURE_TOPIC = "tokifactor/picture"
AVATAR_TOPIC = "tokifactor/avatar"
CONTROL_TOPIC = "tokifactor/control"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
STRUCTURED_FIELDS = (
    "schema",
    "kind",
    "turnId",
    "segment",
    "seq",
    "phase",
    "format",
    "wake",
    "wakeIds",
    "toolCallId",
    "toolName",
    "toolStatus",
    "approvalId",
    "requesterId",
    "timeoutS",
    "details",
    "decision",
    "probeId",
    "protocolVersion",
    "agent",
    "agentId",
    "agentName",
    "description",
    "aliases",
    "model",
    "reasoning",
    "capabilities",
    "backendReady",
    "channelReady",
    "sandboxReady",
    "contextLimit",
    "contextTokens",
    "queueDepth",
    "activeTurn",
    "pendingApprovals",
    "uptimeS",
    "memoryTokens",
)


class CryptoUnavailable(RuntimeError):
    pass


class DecodeError(ValueError):
    pass


def crypto_available() -> bool:
    return AESGCMSIV is not None


@dataclass(frozen=True)
class TextEnvelope:
    sender: str
    text: str
    sender_id: str = ""
    avatar_hash: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AvatarEnvelope:
    type: str
    id: str
    hash: str
    data: str = ""


@dataclass
class _InboundTransfer:
    total: int
    parts: dict[int, bytes] = field(default_factory=dict)
    expected_hash: str = ""
    sender: str = "Unknown"
    sender_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    size: int = 0
    last_seen: float = field(default_factory=time.monotonic)


def _require_crypto() -> None:
    if AESGCMSIV is None:
        raise CryptoUnavailable(
            "tokifactor AES-GCM-SIV support requires Python package 'cryptography'"
        ) from _CRYPTO_IMPORT_ERROR


def derive_key(magic_string: str) -> bytes:
    prk = hmac.new(HARDCODED_SALT, magic_string.encode("utf-8"), hashlib.sha256).digest()
    okm = hmac.new(prk, HKDF_INFO + b"\x01", hashlib.sha256).digest()
    return okm[:32]


def encrypt_bytes(plaintext: bytes, magic_string: str) -> bytes:
    _require_crypto()
    nonce = os.urandom(NONCE_BYTES)
    body = AESGCMSIV(derive_key(magic_string)).encrypt(nonce, plaintext, None)
    return nonce + body


def decrypt_bytes(packet: bytes, magic_string: str) -> bytes:
    _require_crypto()
    if len(packet) <= NONCE_BYTES:
        raise DecodeError("ciphertext too short")
    nonce = packet[:NONCE_BYTES]
    body = packet[NONCE_BYTES:]
    try:
        return AESGCMSIV(derive_key(magic_string)).decrypt(nonce, body, None)
    except Exception as exc:  # InvalidTag and malformed ciphertext
        raise DecodeError("decrypt failed") from exc


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _encode_object(pairs: list[tuple[str, str]]) -> str:
    return "{" + ",".join(f'"{_escape(k)}":"{_escape(v)}"' for k, v in pairs) + "}"


def extract_string(payload: str, key: str) -> str | None:
    """Parse strings the same tolerant way as tokifactor's WireJson."""

    needle = f'"{key}"'
    key_index = payload.find(needle)
    if key_index < 0:
        return None
    colon = payload.find(":", key_index + len(needle))
    if colon < 0:
        return None
    index = colon + 1
    while index < len(payload) and payload[index].isspace():
        index += 1
    if index >= len(payload) or payload[index] != '"':
        return None
    index += 1
    out: list[str] = []
    while index < len(payload):
        char = payload[index]
        if char == "\\" and index + 1 < len(payload):
            escaped = payload[index + 1]
            out.append({"n": "\n", "r": "\r", "\\": "\\", '"': '"'}.get(escaped, escaped))
            index += 2
        elif char == '"':
            return "".join(out)
        else:
            out.append(char)
            index += 1
    return None


def encode_envelope(envelope: TextEnvelope) -> str:
    pairs = [
        ("sender", envelope.sender),
        ("text", envelope.text),
        ("id", envelope.sender_id),
        ("avatarHash", envelope.avatar_hash),
    ]
    pairs.extend(
        (key, str(envelope.metadata[key]))
        for key in STRUCTURED_FIELDS
        if key in envelope.metadata
    )
    return _encode_object(pairs)


def decode_envelope(payload: str) -> TextEnvelope | None:
    sender: Any = None
    text: Any = None
    sender_id: Any = ""
    avatar_hash: Any = ""
    metadata: dict[str, str] = {}
    try:
        value = json.loads(payload)
        if isinstance(value, dict):
            sender = value.get("sender")
            text = value.get("text")
            sender_id = value.get("id", "")
            avatar_hash = value.get("avatarHash", "")
            for key in STRUCTURED_FIELDS:
                if key in value and value[key] is not None:
                    metadata[key] = str(value[key])
    except ValueError:
        pass
    if not isinstance(sender, str) or not isinstance(text, str):
        sender = extract_string(payload, "sender")
        text = extract_string(payload, "text")
        sender_id = extract_string(payload, "id") or ""
        avatar_hash = extract_string(payload, "avatarHash") or ""
        metadata = {
            key: value
            for key in STRUCTURED_FIELDS
            if (value := extract_string(payload, key)) is not None
        }
    if not isinstance(sender, str) or not sender or not isinstance(text, str):
        return None
    return TextEnvelope(
        sender=sender,
        text=text,
        sender_id=str(sender_id or ""),
        avatar_hash=str(avatar_hash or ""),
        metadata=metadata,
    )


def encode_avatar_have(sender_id: str, avatar_hash: str, data: str) -> str:
    return _encode_object(
        [
            ("type", "have"),
            ("id", sender_id),
            ("hash", avatar_hash),
            ("data", data),
        ]
    )


def encode_avatar_want(sender_id: str, avatar_hash: str) -> str:
    return _encode_object(
        [
            ("type", "want"),
            ("id", sender_id),
            ("hash", avatar_hash),
            ("data", ""),
        ]
    )


def parse_avatar_envelope(payload: str) -> AvatarEnvelope | None:
    try:
        value = json.loads(payload)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        value = {
            key: extract_string(payload, key)
            for key in ("type", "id", "hash", "data")
        }
    envelope_type = str(value.get("type") or "").lower()
    sender_id = str(value.get("id") or "")
    if envelope_type not in ("have", "want") or not sender_id:
        return None
    return AvatarEnvelope(
        type=envelope_type,
        id=sender_id,
        hash=str(value.get("hash") or ""),
        data=str(value.get("data") or ""),
    )


def looks_like_chunk(payload: str) -> bool:
    return payload.lstrip().startswith("{")


def parse_text_chunk(payload: str) -> dict[str, Any] | None:
    try:
        value = json.loads(payload)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        value = {
            key: extract_string(payload, key)
            for key in (
                "id",
                "hash",
                "encrypted_data",
                "sender",
                "senderId",
                "avatarHash",
                "size",
                "caption",
                "status",
                "reason",
                *STRUCTURED_FIELDS,
            )
        }
    transfer_id = str(value.get("id") or "")
    sender = str(value.get("sender") or "Unknown")
    sender_id = str(value.get("senderId") or "")
    if str(value.get("status") or "").lower() == "abort":
        return {
            "abort": True,
            "transfer_id": transfer_id.split(":", 1)[0],
            "sender": sender,
            "sender_id": sender_id,
            "reason": str(value.get("reason") or "cancel"),
        }
    parts = transfer_id.split(":")
    if len(parts) != 3 or not parts[0]:
        return None
    try:
        index = int(parts[1])
        total = int(parts[2])
    except ValueError:
        return None
    if index < 0 or total <= 0 or index >= total:
        return None
    encrypted = value.get("encrypted_data")
    digest = value.get("hash")
    if not isinstance(encrypted, str) or not encrypted or not isinstance(digest, str) or not digest:
        return None
    try:
        size = int(value.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    metadata = {
        key: str(value[key])
        for key in STRUCTURED_FIELDS
        if key in value and value[key] is not None
    }
    return {
        "abort": False,
        "transfer_id": parts[0],
        "index": index,
        "total": total,
        "hash": digest,
        "encrypted_data": encrypted,
        "sender": sender,
        "sender_id": sender_id,
        "metadata": metadata,
        "size": size,
        "caption": str(value.get("caption") or ""),
    }


class TextChunkAssembler:
    def __init__(self, magic_string: str) -> None:
        self.magic_string = magic_string
        self._transfers: dict[str, _InboundTransfer] = {}

    def add(self, chunk: dict[str, Any]) -> TextEnvelope | None:
        self._expire()
        transfer_id = str(chunk.get("transfer_id") or "")
        if not transfer_id:
            return None
        if chunk.get("abort"):
            self._transfers.pop(transfer_id, None)
            return None
        index = int(chunk.get("index", 0))
        total = int(chunk.get("total", 0))
        if total <= 0 or total > MAX_INBOUND_CHUNKS or index < 0 or index >= total:
            return None
        transfer = self._transfers.get(transfer_id)
        if transfer is None:
            transfer = _InboundTransfer(
                total=total,
                expected_hash=str(chunk.get("hash") or ""),
                sender=str(chunk.get("sender") or "Unknown"),
                sender_id=str(chunk.get("sender_id") or ""),
                metadata=dict(chunk.get("metadata") or {}),
                size=int(chunk.get("size") or 0),
            )
            self._transfers[transfer_id] = transfer
        if transfer.total != total:
            self._transfers.pop(transfer_id, None)
            return None
        try:
            encrypted = base64.b64decode(str(chunk.get("encrypted_data") or ""), validate=True)
            clear = decrypt_bytes(encrypted, self.magic_string)
        except (ValueError, DecodeError):
            return None
        transfer.parts[index] = clear
        transfer.last_seen = time.monotonic()
        if len(transfer.parts) < total:
            return None
        payload = b"".join(transfer.parts[i] for i in range(total))
        self._transfers.pop(transfer_id, None)
        if len(payload) > MAX_INBOUND_CHUNK_BYTES:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != transfer.expected_hash.lower():
            return None
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return TextEnvelope(
            sender=transfer.sender,
            text=text,
            sender_id=transfer.sender_id,
            metadata=transfer.metadata,
        )

    def _expire(self) -> None:
        cutoff = time.monotonic() - INCOMPLETE_TIMEOUT_S
        for key in [key for key, value in self._transfers.items() if value.last_seen < cutoff]:
            self._transfers.pop(key, None)


def decode_single_payload(payload: str, magic_string: str) -> TextEnvelope | None:
    try:
        encrypted = base64.b64decode(payload, validate=True)
        clear = decrypt_bytes(encrypted, magic_string)
    except (ValueError, DecodeError):
        return None
    try:
        text = clear.decode("utf-8")
    except UnicodeDecodeError:
        return None
    envelope = decode_envelope(text)
    if envelope is not None:
        return envelope
    return TextEnvelope(sender="Unknown", text=text)


def encode_text_payloads(
    text: str,
    magic_string: str,
    *,
    sender: str,
    sender_id: str,
    avatar_hash: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> list[str]:
    extra = {
        key: str(metadata[key])
        for key in STRUCTURED_FIELDS
        if metadata is not None and key in metadata and metadata[key] is not None
    }
    raw = text.encode("utf-8")
    if len(raw) <= TEXT_CHUNK_THRESHOLD_BYTES:
        envelope = TextEnvelope(
            sender=sender,
            text=text,
            sender_id=sender_id,
            avatar_hash=avatar_hash,
            metadata=extra,
        )
        encrypted = encrypt_bytes(encode_envelope(envelope).encode("utf-8"), magic_string)
        return [base64.b64encode(encrypted).decode("ascii")]

    transfer_id = str(uuid.uuid4())
    digest = hashlib.sha256(raw).hexdigest()
    total = max(1, (len(raw) + TEXT_CHUNK_BYTES - 1) // TEXT_CHUNK_BYTES)
    payloads: list[str] = []
    for index in range(total):
        part = raw[index * TEXT_CHUNK_BYTES : (index + 1) * TEXT_CHUNK_BYTES]
        encrypted = base64.b64encode(encrypt_bytes(part, magic_string)).decode("ascii")
        pairs = [
            ("id", f"{transfer_id}:{index}:{total}"),
            ("hash", digest),
            ("encrypted_data", encrypted),
            ("sender", sender),
            ("senderId", sender_id),
            ("avatarHash", avatar_hash),
            ("size", str(len(raw))),
        ]
        pairs.extend((key, extra[key]) for key in STRUCTURED_FIELDS if key in extra)
        chunk = _encode_object(pairs)
        payloads.append(chunk)
    return payloads


def detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def encode_picture_payloads(
    data: bytes,
    magic_string: str,
    *,
    sender: str,
    sender_id: str,
    avatar_hash: str = "",
    caption: str = "",
) -> tuple[str, list[str]]:
    if not data:
        raise ValueError("image is empty")
    transfer_id = str(uuid.uuid4())
    digest = hashlib.sha256(data).hexdigest()
    caption_cipher = (
        base64.b64encode(encrypt_bytes(caption.encode("utf-8"), magic_string)).decode("ascii")
        if caption
        else ""
    )
    total = max(1, (len(data) + TEXT_CHUNK_BYTES - 1) // TEXT_CHUNK_BYTES)
    payloads: list[str] = []
    for index in range(total):
        part = data[index * TEXT_CHUNK_BYTES : (index + 1) * TEXT_CHUNK_BYTES]
        encrypted = base64.b64encode(encrypt_bytes(part, magic_string)).decode("ascii")
        pairs = [
            ("id", f"{transfer_id}:{index}:{total}"),
            ("hash", digest),
            ("encrypted_data", encrypted),
            ("sender", sender),
            ("senderId", sender_id),
            ("avatarHash", avatar_hash),
            ("size", str(len(data))),
        ]
        if caption_cipher:
            pairs.append(("caption", caption_cipher))
        chunk = _encode_object(pairs)
        payloads.append(chunk)
    return transfer_id, payloads


def encode_picture_abort(
    transfer_id: str,
    *,
    sender: str,
    sender_id: str,
    reason: str = "fail",
) -> str:
    return _encode_object(
        [
            ("id", f"{transfer_id}:0:1"),
            ("hash", ""),
            ("encrypted_data", ""),
            ("sender", sender),
            ("senderId", sender_id),
            ("status", "abort"),
            ("reason", reason or "fail"),
        ]
    )
