"""Channel prompt cleanup, speaker annotation, and persisted-turn transactions.

The live user prompt still carries the channel roster so the current turn can
use it.  Once the turn has been persisted, the roster is removed from the
user-role transcript entry and ``conversation.json`` is touched so Letta drops
its resident message window and reloads the cleaned transcript."""

from __future__ import annotations

from .parse import (
    CHANNEL_CONTEXT_RE,
    CHANNEL_PROMPT_HEADER,
    ChannelPromptInfo,
    ConversationBoundary,
    LEGACY_USERNAME_PLACEHOLDER,
    MEMBER_ATTR_RE,
    MEMBER_TAG_RE,
    MESSAGE_TIME_RE,
    RosterMember,
    SENDER_ID_SUFFIX_RE,
    SENDER_RE,
    SEQ_RE,
    SPEAKER_IDENTITY_RE,
    SPEAKER_PREFIX_RE,
    SPEAKER_SENTENCE_TEMPLATE,
    StripResult,
    USERNAME_PLACEHOLDER,
    WAKE_RE,
    _parse_attrs,
    add_speaker_to_wake_reminder,
    existing_speaker,
    has_channel_context,
    infer_speaker_kind,
    match_history_kind,
    parse_channel_prompt,
    parse_roster_members,
    parse_sender,
    render_username_template,
    roster_kind,
    speaker_kind_label,
    speaker_sentence,
    strip_channel_context,
)
from .store import (
    _atomic_write_bytes,
    _boundary_error,
    _conversation_record,
    _find_message_row,
    _fsync_directory,
    _next_updated_at,
    _replace_prompt_in_line,
    append_journal,
    bump_conversation_updated_at,
    capture_conversation_boundary,
    capture_parts,
    identity_lookup_from_registry,
    migrate_channel_prompt,
    read_journal,
    restore_persisted_channel_prompt,
    strip_persisted_channel_prompt,
)
import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import (
    dataclass,
)
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Callable,
    Mapping,
    Sequence,
)
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    canonical_name,
)
