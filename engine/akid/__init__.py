"""Local Aki gateway.

Exposes a JSON-RPC 2.0 / NDJSON API on a Unix socket and supervises one
long-lived Letta stream-json process.  Standard library only."""

from __future__ import annotations

from .config import (
    GatewayConfig,
    build_letta_command,
)
from .config_env import (
    _agent_identity,
    _channel_identity,
    build_config_from_env,
)
from .connections import (
    ChannelConnection,
    Connection,
    NullConnection,
)
from .constants import (
    ACQUIRE_EXECUTION_LOCK_TOOL,
    BACKEND_HISTORY_PAGE,
    CAMERA_TOOL,
    CAMERA_TOOL_NAME,
    CAPABILITIES,
    CHANNEL_EVENT_QUEUE_MAX,
    CHANNEL_IMAGE_DIRECTIVE_RE,
    CHANNEL_IMAGE_GROUP_GRACE_S,
    CHANNEL_IMAGE_GROUP_TIMEOUT_S,
    CHANNEL_MEMBER_TIMEOUT,
    CHANNEL_PROBE_RATE_LIMIT,
    CHANNEL_PROBE_SEEN_TTL,
    CHANNEL_PROTOCOL_VERSION,
    CHANNEL_ROSTER_PROBE_INTERVAL,
    CHANNEL_STATUS_INTERVAL,
    CHANNEL_TRANSCRIPT_MAX_RECORDS,
    DEFAULT_AGENT_ID,
    DEFAULT_AGENT_INITIATION_LIMIT,
    DEFAULT_APPROVAL_TIMEOUT,
    DEFAULT_CONVERSATION_ID,
    DEFAULT_LIVE_CONTEXT_HISTORY_LIMIT,
    DEFAULT_LIVE_CONTEXT_PROCESS_LIMIT,
    DEFAULT_LIVE_CONTEXT_TIMEOUT,
    DEFAULT_MCP_NATIVE_MAX_TOOLS,
    DEFAULT_MCP_TOOL_TIMEOUT,
    DEFAULT_QUEUE_MAX,
    EXTERNAL_TOOL_NAMES,
    HIDDEN_TOOL_NAMES,
    HISTORY_IMAGE_PLACEHOLDER,
    HISTORY_MAX_BYTES,
    IDEMPOTENCY_TTL,
    IMAGE_GENERATION_TOOL_NAME,
    LAYER_DISPLAY_RETRY_SECONDS,
    LOGGER,
    MAX_AGENT_IMAGE_DIRECTIVES,
    MAX_CAMERA_DIMENSION,
    MAX_CAMERA_OUTPUT_BYTES,
    MAX_CHANNEL_ATTACHMENTS,
    MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES,
    MAX_CHANNEL_TEXT_CHARS,
    MAX_HISTORY_TEXT_CHARS,
    MAX_IMAGE_CAPTION_CHARS,
    MAX_MCP_TOOL_TEXT_CHARS,
    MAX_MEMORY_FILE,
    MAX_SCREENSHOT_OUTPUT_BYTES,
    MCP_TOOL_PREFIX,
    MIN_CAMERA_DIMENSION,
    SAFE_TOOLS,
    SAFE_TOOL_PREFIXES,
    SCREENSHOT_TOOL,
    SCREENSHOT_TOOL_NAME,
    SEARCH_CHAT_HISTORY_TOOL,
    SEARCH_CHAT_HISTORY_TOOL_NAME,
    TOKIFACTOR_FILE_TOOL,
    TOKIFACTOR_FILE_TOOL_NAME,
    TOKIFACTOR_IMAGE_TOOL,
    TOKIFACTOR_IMAGE_TOOL_NAME,
    WAIT_GUARD_ORIGIN,
    WAIT_OTHER_MAX_REPLY_CHARS,
    WAIT_OTHER_MAX_TOTAL_CHARS,
    WAIT_OTHER_POLL_INTERVAL,
    WAIT_OTHER_START_GRACE,
    WAIT_OTHER_TIMEOUT_MS,
    WAIT_OTHER_TOOL_SCHEMA,
    WAIT_USER_MAX_REPLY_CHARS,
    WAIT_USER_MAX_TIMEOUT_S,
    WAIT_USER_MAX_TOTAL_CHARS,
    WAIT_USER_MIN_TIMEOUT_S,
    WAIT_USER_TOOL_SCHEMA,
    WAIT_USER_TOOL_TIMEOUT_MS,
    _NO_REPLY_MARKER_RE,
)
from .exclusive import (
    ExclusiveToolError,
)
from .history import (
    _compact_history_messages,
    _content_text,
    _format_channel_message_time,
    _mcp_content_part_text,
    _normalize_mcp_result,
    _parse_tool_arguments,
    _sanitize_history_value,
    _truncate_history_text,
    _truncate_mcp_text,
)
from .main import (
    _serve,
    main,
)
from .no_reply import (
    contains_no_reply_marker,
)
from .paths import (
    _b64url,
    _is_video_device,
    default_backend_dir,
    default_camera_dir,
    default_image_generation_dir,
    default_mcp_native_state_path,
    default_memory_dir,
    default_screenshot_dir,
    default_socket_path,
    default_turn_overlay_dir,
    now_iso,
    prepare_private_output_dir,
    resolve_memory_path,
    tool_is_safe,
    unique_capture_path,
)
from .server import (
    GatewayServer,
)
from .types import (
    AgentChannelState,
    Approval,
    ChannelTurnState,
    Turn,
    WaitOtherResult,
)
import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from xml.sax.saxutils import (
    escape as xml_escape,
    quoteattr as xml_quoteattr,
)
from collections import (
    deque,
)
from dataclasses import (
    dataclass,
    field,
    replace,
)
from datetime import (
    datetime,
    timezone,
)
from pathlib import (
    Path,
    PurePosixPath,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from engine.aki_config import (
    AkiConfig,
    ConfigError,
    ImageGeneration,
    ToolPolicy,
    atomic_write,
    default_config_path,
    default_socket_path as profile_default_socket_path,
    default_username_path,
    discover_profiles,
    ensure_config,
    load_config,
    legacy_state_dir,
    read_mcp_excluded,
    resolve_config_path,
    set_key,
    state_dir,
    sync_channel_identity,
    write_mcp_excluded,
)
from engine.image_generation import (
    ASPECT_RATIOS,
    MAX_IMAGES,
    ImageGenerationError,
    generate_minimax_images,
)
from engine.letta_llm import (
    DEFAULT_REASONING_EFFORT,
    prepare_letta,
    supported_reasoning_efforts,
    write_provider_mod,
)
from engine.letta_process import (
    BackendProtocolError,
    BackendUnavailable,
    LettaProcess,
)
from engine.layer_process import (
    LayerProcess,
    default_layer_binary,
    discover_wayland_display,
)
from engine.layer_notify import (
    LayerNotifier,
    character_for_names,
    character_for_profile,
)
from engine.letta_compact import (
    run_letta_compact,
)
from engine.avatar import (
    AvatarAsset,
    AvatarError,
    default_avatar_path,
    load_avatar,
)
from engine.emoji_catalog import (
    EmojiEntry,
    load_emoji_entries,
)
from engine.channel import (
    CONTROL_TOPIC,
    DEFAULT_MAGIC,
    TEXT_TOPIC,
    ChannelUnavailable,
    TokifactorChannel,
)
from engine.channel_prompt import (
    capture_conversation_boundary,
)
from engine.channel_history import (
    channel_history_key,
    channel_history_path,
    read_channel_history,
)
from engine.execution_lock import (
    ExecutionLock,
    default_execution_lock_path,
    default_execution_lock_status_path,
)
from engine.execution_policy import (
    LOCK_ACQUIRE_TOOL,
    LOCK_BUSY_ERROR,
    LOCK_NOT_AUTHORIZED_ERROR,
    LOCK_RELEASE_TOOL,
    LOCK_REQUIRED_ERROR,
    WAIT_OTHER_TOOL,
    WAIT_USER_TOOL,
    is_lock_control_tool,
    requires_execution_lock,
)
from engine.live_context import (
    DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    DEFAULT_BUDGET_TOKENS as DEFAULT_LIVE_CONTEXT_BUDGET_TOKENS,
    CHANNEL_WAKE_ALL,
    CHANNEL_WAKE_MENTIONS,
    CHANNEL_WAKE_USER,
    ChannelMember,
    ChannelImageAttachment,
    ChannelTranscriptEntry,
    LiveContextOptions,
    clamp_budget_tokens,
    collect_live_context,
    desktop_environment,
    match_channel_mentions,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_wake_routing,
    render_turn_control,
    render_channel_transcript,
    render_live_context,
    render_new_message,
    render_solo_memories,
    render_solo_mode,
    render_solo_transcript,
    render_user_insertion,
    render_user_insertions,
    route_channel_reply,
    strip_channel_mentions,
    strip_channel_non_wakes,
)
from engine.solo_memory import (
    FALLBACK_SUMMARY_STATUS,
    SOLO_MEMORY_BUDGET_TOKENS,
    SOLO_SUMMARY_TIMEOUT_SECONDS,
    SoloMemoryManager,
    SoloMemorySession,
    capture_solo_entries,
    default_solo_memory_path,
    fallback_summary,
    strip_runtime_prompt_blocks,
)
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_FAILED,
    SOLO_READY,
    SOLO_START,
    SoloSession,
    clear_solo_session,
    default_solo_state_path,
    derive_solo_magic,
    load_solo_scene,
    load_solo_session,
    now_iso as solo_now_iso,
    save_solo_session,
)
from engine.life_ledger import (
    LedgerError,
    LifeLedger,
    render_ledger_block,
)
from engine.operation_journal import (
    OperationJournal,
)
from engine.presence import (
    EvdevActivityProbe,
    LogindActivityProvider,
    PresenceMonitor,
    PresenceTrigger,
)
from engine.schedule_store import (
    ScheduleError,
    ScheduleRecord,
    ScheduleStore,
    now_utc,
    parse_datetime,
)
from engine.tool_ownership import (
    EXCLUSIVE_TOOL_NAMES,
    apply_tool_ownership,
    authorize_exclusive_tool,
    clear_tool_ownership,
    is_exclusive_tool,
    ownership_denied_message,
    render_tool_ownership_prompt,
    tools_for_profile,
)
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    member_name_key,
    normalize_member_kind,
    truncate_member_description,
)
from engine.member_identity import (
    IdentityConflict,
    IdentityError,
    IdentityRegistry,
    MemberIdentity,
    backup_migration_state,
    canonical_name,
    ensure_manifest,
    is_member_id,
    legacy_member_id,
    read_manifest,
    write_manifest,
)
from engine.mqtt_settings import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    default_mqtt_state_path,
    load_mqtt_server,
    save_mqtt_server,
)
from engine.tokifactor import (
    MAX_ATTACHMENT_NAME_CHARS,
    MAX_IMAGE_BYTES,
    MAX_MESSAGE_ATTACHMENTS,
    MAX_MESSAGE_IMAGE_BYTES,
    ImageEnvelope,
    TextEnvelope,
    detect_image_mime,
)
from engine.writable import (
    WritableRegistry,
)
from engine.protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    ErrorCode,
    FrameError,
    Request,
    RpcError,
    decode_message,
    encode_message,
    make_error,
    make_event,
    make_response,
    optional_bool,
    optional_string,
    parse_request,
    read_message,
    require_int,
    require_string,
)
