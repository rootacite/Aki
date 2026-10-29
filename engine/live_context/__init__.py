"""Desktop live-context collection, rendering, and prompt-block helpers."""

from __future__ import annotations

from .blocks import (
    _channel_turn_details,
    apply_channel_identity,
    apply_channel_policy,
    apply_channel_turn,
    apply_channel_wake_routing,
    clear_channel_identity,
    clear_channel_turn,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_turn,
    render_channel_wake_routing,
)
from .desktop import (
    _capture_source,
    _collect_history,
    _collect_processes,
    _collect_window,
    _local_timestamp,
    _render_block,
    _run_command,
    apply_live_context,
    build_kwin_probe_script,
    clear_live_context,
    collect_live_context,
    desktop_environment,
    parse_kwin_journal_line,
    parse_ps_output,
    parse_zsh_history_text,
    read_zsh_history,
    render_live_context,
    screenshot_from_mapping,
)
from .mentions import (
    _iter_non_wake_matches,
    _non_wake_alias_map,
    _valid_mention_boundaries,
    match_channel_mentions,
    match_channel_non_wakes,
    route_channel_reply,
    select_execution_responder,
    strip_channel_mentions,
    strip_channel_non_wakes,
    strip_user_notice_target,
)
from .solo_blocks import (
    _solo_memory_entry_lines,
    _solo_memory_session_lines,
    apply_shared_memory,
    apply_solo_memories,
    apply_solo_mode,
    apply_solo_transcript,
    clear_shared_memory,
    clear_solo_memories,
    clear_solo_mode,
    clear_solo_transcript,
    render_new_message,
    render_shared_memory,
    render_solo_memories,
    render_solo_mode,
    render_solo_transcript,
    render_user_insertion,
    render_user_insertions,
)
from .transcript import (
    _channel_entry_sort_key,
    _channel_entry_time,
    _channel_transcript_entry,
    _render_transcript_profiles,
    _truncate_to_tokens,
    apply_channel_transcript,
    clear_channel_transcript,
    render_channel_transcript,
)
from .types import (
    CHANNEL_IDENTITY_TAG,
    CHANNEL_POLICY_TAG,
    CHANNEL_TRANSCRIPT_TAG,
    CHANNEL_TURN_TAG,
    CHANNEL_WAKE_ALL,
    CHANNEL_WAKE_MENTIONS,
    CHANNEL_WAKE_ROUTING_TAG,
    CHANNEL_WAKE_USER,
    CORE_MEMORY_PLACEHOLDER,
    ChannelImageAttachment,
    ChannelMember,
    ChannelReplyRoute,
    ChannelTranscriptEntry,
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    DEFAULT_SOLO_TRANSCRIPT_BUDGET_TOKENS,
    HISTORY_READ_BYTES,
    HistoryEntry,
    LIVE_CONTEXT_TAG,
    LiveContextOptions,
    LiveContextSnapshot,
    MAX_CHANNEL_MEMBER_DESCRIPTION,
    MAX_HISTORY_COMMAND_CHARS,
    ProcessInfo,
    SHARED_MEMORY_TAG,
    SOLO_MEMORIES_TAG,
    SOLO_MODE_TAG,
    SOLO_TRANSCRIPT_TAG,
    ScreenshotInfo,
    WindowInfo,
    _CHANNEL_IDENTITY_RE,
    _CHANNEL_POLICY_RE,
    _CHANNEL_TRANSCRIPT_RE,
    _CHANNEL_TURN_RE,
    _CHANNEL_WAKE_ROUTING_RE,
    _CONTROL_RE,
    _EMAIL_RE,
    _LIVE_CONTEXT_RE,
    _SHARED_MEMORY_RE,
    _SOLO_MEMORIES_RE,
    _SOLO_MODE_RE,
    _SOLO_TRANSCRIPT_RE,
    _ZSH_EXTENDED_RE,
    _apply_dynamic_block,
    _attr,
    _framework_block_re,
    _sanitize,
    _text,
    clamp_budget_tokens,
    estimate_tokens,
)
import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    timezone,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from xml.sax.saxutils import (
    escape,
    quoteattr,
)
from engine.member_profile import (
    MAX_MEMBER_DESCRIPTION_UNITS,
    MEMBER_KIND_AGENT,
    canonical_name,
    normalize_member_kind,
    truncate_member_description,
)
from engine.emoji_catalog import (
    EmojiEntry,
)
