"""TokifactorChannel, assembled from the channel mixins."""

from __future__ import annotations

from .avatars import AvatarsMixin
from .publish import PublishMixin
from .runtime import RuntimeMixin


class TokifactorChannel(RuntimeMixin, PublishMixin, AvatarsMixin):
    pass
