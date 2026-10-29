"""Channel topics, limits, and the channel-unavailable error."""

from __future__ import annotations

from typing import (
    Awaitable,
    Callable,
    Mapping,
)
from engine.tokifactor import (
    FileEnvelope,
    ImageEnvelope,
    TextEnvelope,
)


TEXT_TOPIC = "tokifactor/text"

DEFAULT_MAGIC = "akid-default-channel"

DEDUP_MAX_IDS = 4096

CHANNEL_INBOX_MAX = 4096

REPLAY_GRACE_SECONDS = 1.0

AVATAR_WANT_TTL_SECONDS = 30.0

AVATAR_HAVE_TTL_SECONDS = 2.0


TextHandler = Callable[
    [str, str, str, Mapping[str, str], str, int, bool], Awaitable[None]
]

ImageHandler = Callable[[ImageEnvelope, bool], Awaitable[None]]

FileHandler = Callable[[FileEnvelope, bool], Awaitable[None]]

ControlHandler = Callable[[TextEnvelope], Awaitable[None]]



class ChannelUnavailable(RuntimeError):
    pass
