"""Persistent tokifactor MQTT channel adapter for akid."""

from __future__ import annotations

from .client import (
    TokifactorChannel,
)
from .constants import (
    AVATAR_HAVE_TTL_SECONDS,
    AVATAR_WANT_TTL_SECONDS,
    CHANNEL_INBOX_MAX,
    ChannelUnavailable,
    ControlHandler,
    DEDUP_MAX_IDS,
    DEFAULT_MAGIC,
    ImageHandler,
    REPLAY_GRACE_SECONDS,
    TEXT_TOPIC,
    TextHandler,
)
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
import uuid
from pathlib import (
    Path,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
)
from engine.aki_config import (
    atomic_write,
)
from engine.mqtt_client import (
    MqttClient,
    MqttError,
)
from engine.tokifactor import (
    AVATAR_TOPIC,
    CONTROL_TOPIC,
    DecodeError,
    ImageChunkAssembler,
    ImageEnvelope,
    MAX_IMAGE_BYTES,
    MAX_MESSAGE_ATTACHMENTS,
    MAX_MESSAGE_IMAGE_BYTES,
    PICTURE_TOPIC,
    TextChunkAssembler,
    TextEnvelope,
    crypto_available,
    decrypt_bytes,
    decode_single_payload,
    detect_image_mime,
    encode_avatar_have,
    encode_avatar_want,
    encode_picture_abort,
    encode_picture_payloads,
    encode_text_payloads,
    encrypt_bytes,
    looks_like_chunk,
    parse_avatar_envelope,
    parse_text_chunk,
)
