"""Local Aki gateway.

Exposes a JSON-RPC 2.0 / NDJSON API on a Unix socket and supervises one
long-lived Letta stream-json process.  Standard library only.
"""

from __future__ import annotations

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
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from engine.letta_process import BackendProtocolError, BackendUnavailable, LettaProcess
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
from engine.letta_compact import run_letta_compact
from engine.avatar import AvatarAsset, AvatarError, default_avatar_path, load_avatar
from engine.emoji_catalog import EmojiEntry, load_emoji_entries
from engine.channel import (
    CONTROL_TOPIC,
    DEFAULT_MAGIC,
    TEXT_TOPIC,
    ChannelUnavailable,
    TokifactorChannel,
)
from engine.channel_prompt import capture_conversation_boundary
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
    clear_execution_lock_policy,
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
    apply_channel_identity,
    apply_channel_policy,
    apply_channel_wake_routing,
    apply_shared_memory,
    apply_solo_memories,
    apply_solo_mode,
    clamp_budget_tokens,
    clear_channel_identity,
    clear_channel_turn,
    clear_channel_transcript,
    clear_live_context,
    clear_shared_memory,
    clear_solo_memories,
    clear_solo_mode,
    clear_solo_transcript,
    collect_live_context,
    desktop_environment,
    match_channel_mentions,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_turn,
    render_channel_wake_routing,
    render_channel_transcript,
    render_live_context,
    render_new_message,
    render_shared_memory,
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
from engine.public_memory import PublicMemoryStore, default_public_memory_path
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
from engine.operation_journal import OperationJournal
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
from engine.writable import WritableRegistry
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

LOGGER = logging.getLogger("akid")

DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
DEFAULT_CONVERSATION_ID = "default"
DEFAULT_QUEUE_MAX = 32
DEFAULT_APPROVAL_TIMEOUT = 60.0
IDEMPOTENCY_TTL = 600.0
MAX_MEMORY_FILE = 1024 * 1024
MAX_CHANNEL_TEXT_CHARS = 64 * 1024
_NO_REPLY_MARKER_RE = re.compile(
    r"(?:[\[\(【（{]\s*NO[\s_-]*REPLY\s*[\]\)】）}]|\bNO[\s_-]*REPLY\b)",
    re.IGNORECASE,
)


def contains_no_reply_marker(text: str) -> bool:
    """Return true when text contains a tolerated NO_REPLY marker anywhere."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return bool(_NO_REPLY_MARKER_RE.search(normalized))
TOKIFACTOR_IMAGE_TOOL_NAME = "tokifactor_send_image"
CAMERA_TOOL_NAME = "camera_take_photo"
SCREENSHOT_TOOL_NAME = "desktop_take_screenshot"
IMAGE_GENERATION_TOOL_NAME = "generate_image"
EXTERNAL_TOOL_NAMES = {
    TOKIFACTOR_IMAGE_TOOL_NAME,
    CAMERA_TOOL_NAME,
    SCREENSHOT_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    LOCK_ACQUIRE_TOOL,
    WAIT_OTHER_TOOL,
    WAIT_USER_TOOL,
} | set(EXCLUSIVE_TOOL_NAMES)
HIDDEN_TOOL_NAMES = frozenset({WAIT_OTHER_TOOL, WAIT_USER_TOOL})
MCP_TOOL_PREFIX = "mcp__"
MAX_MCP_TOOL_TEXT_CHARS = 128 * 1024
DEFAULT_MCP_NATIVE_MAX_TOOLS = 64
DEFAULT_MCP_TOOL_TIMEOUT = 300.0
MAX_IMAGE_CAPTION_CHARS = 200
MAX_CHANNEL_ATTACHMENTS = MAX_MESSAGE_ATTACHMENTS
MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES = MAX_MESSAGE_IMAGE_BYTES
MAX_AGENT_IMAGE_DIRECTIVES = MAX_MESSAGE_ATTACHMENTS
CHANNEL_IMAGE_DIRECTIVE_RE = re.compile(r"#\[(/[^\]\r\n]+)\]")
MAX_CAMERA_DIMENSION = 4096
MIN_CAMERA_DIMENSION = 160
MAX_CAMERA_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_SCREENSHOT_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_HISTORY_TEXT_CHARS = 32 * 1024
HISTORY_MAX_BYTES = 768 * 1024
HISTORY_IMAGE_PLACEHOLDER = "[image data omitted from history]"
DEFAULT_LIVE_CONTEXT_TIMEOUT = 3.0
DEFAULT_LIVE_CONTEXT_HISTORY_LIMIT = 20
DEFAULT_LIVE_CONTEXT_PROCESS_LIMIT = 5
CHANNEL_EVENT_QUEUE_MAX = 256
CHANNEL_TRANSCRIPT_MAX_RECORDS = 512
CHANNEL_IMAGE_GROUP_GRACE_S = 0.75
CHANNEL_IMAGE_GROUP_TIMEOUT_S = 5.0
BACKEND_HISTORY_PAGE = 32
CHANNEL_PROTOCOL_VERSION = 1
CHANNEL_STATUS_INTERVAL = 10.0
CHANNEL_ROSTER_PROBE_INTERVAL = 30.0
CHANNEL_MEMBER_TIMEOUT = 30.0
CHANNEL_PROBE_SEEN_TTL = 300.0
CHANNEL_PROBE_RATE_LIMIT = 1.0
DEFAULT_AGENT_INITIATION_LIMIT = 3
WAIT_OTHER_START_GRACE = 3.0
WAIT_OTHER_POLL_INTERVAL = 0.2
WAIT_OTHER_MAX_REPLY_CHARS = 16 * 1024
WAIT_OTHER_MAX_TOTAL_CHARS = 32 * 1024
WAIT_OTHER_TIMEOUT_MS = 24 * 60 * 60 * 1000
WAIT_GUARD_ORIGIN = "wait_guard"
WAIT_USER_MIN_TIMEOUT_S = 1
WAIT_USER_MAX_TIMEOUT_S = 25
WAIT_USER_MAX_REPLY_CHARS = 8 * 1024
WAIT_USER_MAX_TOTAL_CHARS = 16 * 1024
WAIT_USER_TOOL_TIMEOUT_MS = 30 * 1000
LAYER_DISPLAY_RETRY_SECONDS = 2.0

TOKIFACTOR_IMAGE_TOOL = {
    "name": TOKIFACTOR_IMAGE_TOOL_NAME,
    "label": "Send tokifactor image",
    "description": (
        "Send a local PNG/JPEG/GIF/WebP image as a standalone image-only "
        "message to the shared tokifactor MQTT channel. Do not use this tool "
        "when the image should share one message with text: write "
        "`#[/absolute/path/to/image]` in the final reply instead. The image path must be absolute and under /tmp, "
        "~/.local/share/tmp, or a user-registered writable directory. Use this "
        "only when the user asks to share an image on the tokifactor channel or "
        "it clearly adds value. This tool does not require the execution lock. "
        "Returns the transfer id and chunk count."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to an existing image file.",
            },
            "caption": {
                "type": "string",
                "description": (
                    "Optional terse label sent right after the image, for example "
                    "'lab desk'. This becomes the image attachment name."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 180000,
}

CAMERA_TOOL = {
    "name": CAMERA_TOOL_NAME,
    "label": "Take a camera photo",
    "description": (
        "Capture one photo from the local camera and save it as a JPEG under "
        "/tmp/aki/camera. This tool always requires explicit user approval. Use "
        "it only when the user asks Aki to look through the camera, take a "
        "photo, or inspect the current physical surroundings. Returns only the "
        "absolute image path, size, dimensions, device, and MIME type; image "
        "bytes are never attached to this result. Call Read or ViewImage on the "
        "returned path only if you need to inspect the photo. Files under /tmp "
        "are ephemeral and may disappear after reboot. Each capture uses a unique "
        "timestamped filename."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "device": {
                "type": "string",
                "description": "Optional V4L2 video device such as /dev/video0.",
            },
            "width": {
                "type": "integer",
                "description": "Optional requested capture width in pixels.",
            },
            "height": {
                "type": "integer",
                "description": "Optional requested capture height in pixels.",
            },
        },
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 60000,
}

SCREENSHOT_TOOL = {
    "name": SCREENSHOT_TOOL_NAME,
    "label": "Take a desktop screenshot",
    "description": (
        "Capture the current desktop as a PNG and save it under "
        "/tmp/aki/screenshots. On KDE it uses Spectacle; on other desktops it "
        "uses grim. Use this tool instead of Bash when the user asks for a "
        "screenshot or wants to see the desktop. It tries the current Wayland "
        "display first and falls back to other available wayland-* sockets. "
        "Returns only the absolute image path, size, dimensions, display, and "
        "capture backend; image bytes are never attached to this result. Call "
        "Read or ViewImage on the returned path only if you need to inspect the "
        "image. Files under /tmp are ephemeral and may disappear after reboot. "
        "Each capture uses a unique timestamped filename."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "output": {
                "type": "string",
                "description": "Optional Wayland output name to capture, such as DP-1.",
            },
            "include_cursor": {
                "type": "boolean",
                "description": "Include the mouse cursor in the screenshot.",
            },
        },
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 60000,
}

SAFE_TOOLS = {"Read", "ReadLSP", "Grep", "Glob", "LS", "Memory", "ViewImage"}
SAFE_TOOL_PREFIXES = ("mcp__tavily__",)

ACQUIRE_EXECUTION_LOCK_TOOL = {
    "name": LOCK_ACQUIRE_TOOL,
    "label": "Acquire execution lock",
    "description": (
        "Acquire the device-wide exclusive lock required for non-read-only tools. "
        "Call this before Bash, Memory, Agent, file writes, MCP tools not explicitly "
        "classified read-only, or any other tool marked as requiring the lock. If acquisition "
        "fails, do not retry; end the turn. The lock is released automatically when "
        "the turn ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 10000,
}

WAIT_OTHER_TOOL_SCHEMA = {
    "name": WAIT_OTHER_TOOL,
    "label": "Wait for earlier agents",
    "description": (
        "You MUST call this once before your final answer when the channel prompt "
        "requires it. It waits until the configured earlier agents finish their "
        "current public channel reply, then returns their final replies. It is not "
        "in compliance to speak before those targets have finished. This tool is "
        "hidden from the user. It returns immediately for idle agents and reports "
        "any target that goes offline. Do not mention or emphasize whom you waited "
        "for in your reply. Do not retry after an offline or unknown target result."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": WAIT_OTHER_TIMEOUT_MS,
}

WAIT_USER_TOOL_SCHEMA = {
    "name": WAIT_USER_TOOL,
    "label": "Wait for the user to continue",
    "description": (
        "Wait silently for the next message from the human user who triggered "
        "this turn in the current tokifactor channel. Use it only when that user "
        "may still be in the middle of speaking and is likely to send another "
        "message. It returns immediately when that same user sends a new message, "
        "or returns a timeout result after timeout_s seconds (an integer from 1 "
        "to 25). You may call it again if you still expect more follow-up. Only "
        "available on human-triggered channel turns. Never mention, explain, or "
        "announce this tool, the wait, or its result in your reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timeout_s": {
                "type": "integer",
                "minimum": WAIT_USER_MIN_TIMEOUT_S,
                "maximum": WAIT_USER_MAX_TIMEOUT_S,
                "description": "Seconds to wait for the next message from the same user.",
            }
        },
        "required": ["timeout_s"],
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": WAIT_USER_TOOL_TIMEOUT_MS,
}

CAPABILITIES = {
    "streaming": True,
    "approvals": True,
    "history": True,
    "memory_readonly": True,
    "multiclient": True,
    "writable_dirs": True,
    "tokifactor_image_tool": True,
    "camera_tool": True,
    "screenshot_tool": True,
    "image_generation_tool": True,
    "mcp_native_tools": True,
    "avatar": True,
    "mqtt_manage": True,
    "conversation_reset": True,
    "username_manage": True,
    "exclusive_tools": True,
    "schedule_tools": True,
    "life_ledger": True,
    "proactive_triggers": True,
    "queue_max": DEFAULT_QUEUE_MAX,
    "protocol_version": PROTOCOL_VERSION,
}


class ExclusiveToolError(ValueError):
    """A failed exclusive-tool operation with a model-facing code."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_socket_path(profile: str = "aki") -> Path:
    return profile_default_socket_path(profile)


def default_camera_dir() -> Path:
    return Path("/tmp") / "aki" / "camera"


def default_screenshot_dir() -> Path:
    return Path("/tmp") / "aki" / "screenshots"


def default_image_generation_dir() -> Path:
    return Path("/tmp") / "aki" / "images"


def unique_capture_path(directory: Path, prefix: str, suffix: str) -> Path:
    """Return a timestamped path with a random suffix to avoid capture collisions."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"


def prepare_private_output_dir(path: Path, label: str) -> None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"{label} output directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} output path is not a directory")
        if info.st_uid != os.getuid() or parent_info.st_uid != os.getuid():
            raise ValueError(
                f"{label} output directory is not owned by the current user"
            )
        os.chmod(path, 0o700)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"cannot prepare {label} output directory: {exc}") from exc


def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def default_turn_overlay_dir(backend_dir: Path, agent_id: str) -> Path:
    return backend_dir / "aki-turn-overlays" / (agent_id or "agent")


def default_mcp_native_state_path() -> Path:
    return default_config_path()


def default_memory_dir(agent_id: str) -> Path:
    configured = os.environ.get("AKI_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return default_backend_dir() / "memfs" / agent_id / "memory"


def tool_is_safe(name: str) -> bool:
    return name in SAFE_TOOLS or any(name.startswith(p) for p in SAFE_TOOL_PREFIXES)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def resolve_memory_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RpcError(ErrorCode.INVALID_PATH, "path must be a non-empty string")
    if "\x00" in relative:
        raise RpcError(ErrorCode.INVALID_PATH, "path contains a NUL byte")
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        raise RpcError(ErrorCode.INVALID_PATH, "absolute paths are not allowed")
    for part in pure.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise RpcError(ErrorCode.INVALID_PATH, "parent traversal is not allowed")
        if part.startswith("."):
            raise RpcError(ErrorCode.INVALID_PATH, "hidden paths are not allowed")
    try:
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "memory root is unavailable") from exc
    candidate = root_real.joinpath(*pure.parts)
    try:
        real = candidate.resolve(strict=False)
    except OSError as exc:
        raise RpcError(ErrorCode.INVALID_PATH, "invalid path") from exc
    if real != root_real and root_real not in real.parents:
        raise RpcError(ErrorCode.INVALID_PATH, "path escapes the memory root")
    return real


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _is_video_device(path: Path) -> bool:
    name = path.name
    return (
        path.is_char_device()
        and name.startswith("video")
        and name[5:].isdigit()
    )


def _parse_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def _truncate_mcp_text(text: str) -> str:
    if len(text) <= MAX_MCP_TOOL_TEXT_CHARS:
        return text
    return text[:MAX_MCP_TOOL_TEXT_CHARS] + "\n...[truncated by akid]"


def _mcp_content_part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    kind = part.get("type")
    if kind == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if kind == "image":
        mime = part.get("mimeType") or part.get("mime_type") or "image"
        data = part.get("data")
        size = len(data) if isinstance(data, str) else 0
        return f"[image omitted: {mime}, {size} base64 chars]"
    if kind == "resource" and isinstance(part.get("resource"), dict):
        resource = part["resource"]
        if isinstance(resource.get("text"), str):
            return resource["text"]
        return json.dumps(resource, ensure_ascii=False)
    return json.dumps(part, ensure_ascii=False)


def _normalize_mcp_result(raw: str) -> tuple[str, bool]:
    text = raw.strip()
    if not text:
        return "MCP tool returned no output", False
    try:
        payload = json.loads(text)
    except ValueError:
        return _truncate_mcp_text(text), False
    if not isinstance(payload, dict):
        return _truncate_mcp_text(json.dumps(payload, ensure_ascii=False)), False

    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or json.dumps(error, ensure_ascii=False)
        return _truncate_mcp_text(str(message)), True
    if error:
        return _truncate_mcp_text(str(error)), True

    is_error = bool(payload.get("isError") or payload.get("is_error"))
    parts: list[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        parts.extend(
            text_part
            for part in content
            if (text_part := _mcp_content_part_text(part))
        )
    elif content is not None:
        text_part = _mcp_content_part_text(content)
        if text_part:
            parts.append(text_part)
    if not parts and payload.get("structuredContent") is not None:
        parts.append(json.dumps(payload["structuredContent"], ensure_ascii=False))
    if not parts:
        parts.append(json.dumps(payload, ensure_ascii=False))
    return _truncate_mcp_text("\n".join(parts)), is_error


def _truncate_history_text(text: str) -> str:
    if len(text) <= MAX_HISTORY_TEXT_CHARS:
        return text
    omitted = len(text) - MAX_HISTORY_TEXT_CHARS
    return text[:MAX_HISTORY_TEXT_CHARS] + f"\n…[truncated {omitted} chars]"


def _sanitize_history_value(value: Any) -> Any:
    """Remove heavy payloads that must not travel over the gateway socket."""
    if isinstance(value, str):
        return _truncate_history_text(value)
    if isinstance(value, list):
        return [_sanitize_history_value(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "text", "text": HISTORY_IMAGE_PLACEHOLDER}
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key in {"data", "base64", "image_url"}
                and isinstance(item, str)
                and len(item) > 256
            ):
                sanitized[key] = HISTORY_IMAGE_PLACEHOLDER
            else:
                sanitized[key] = _sanitize_history_value(item)
        return sanitized
    return value


def _compact_history_messages(messages: Any) -> tuple[list[dict[str, Any]], bool]:
    """Sanitize history and cap the encoded size of one response batch."""
    if not isinstance(messages, list):
        return [], False
    compacted: list[dict[str, Any]] = []
    used = 0
    dropped = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        sanitized = _sanitize_history_value(message)
        try:
            size = len(
                json.dumps(
                    sanitized, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            continue
        if size > HISTORY_MAX_BYTES:
            sanitized = {
                "id": sanitized.get("id"),
                "date": sanitized.get("date"),
                "message_type": sanitized.get("message_type"),
                "role": sanitized.get("role"),
                "content": [{"type": "text", "text": "[message too large to display]"}],
            }
            size = len(
                json.dumps(
                    sanitized, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        if compacted and used + size > HISTORY_MAX_BYTES:
            dropped = True
            break
        compacted.append(sanitized)
        used += size
    return compacted, dropped


def _format_channel_message_time(sent_at: int) -> str:
    """Format a channel message timestamp in the server's local timezone."""
    try:
        seconds = int(sent_at) / 1000 if int(sent_at) > 0 else time.time()
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass
class GatewayConfig:
    socket_path: Path
    profile: str = "aki"
    agent_id: str = DEFAULT_AGENT_ID
    conversation_id: str = DEFAULT_CONVERSATION_ID
    letta_bin: str = "letta"
    cwd: Path = field(default_factory=Path.cwd)
    emoji_dir: Path | None = None
    userdata_dir: Path | None = None
    memory_dir: Path | None = None
    backup_dir: Path | None = None
    backend_dir: Path = field(default_factory=default_backend_dir)
    queue_max: int = DEFAULT_QUEUE_MAX
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    idempotency_ttl: float = IDEMPOTENCY_TTL
    mqtt_enabled: bool = False
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    mqtt_topic: str = TEXT_TOPIC
    mqtt_magic: str = DEFAULT_MAGIC
    mqtt_sender_name: str = "akid"
    mqtt_sender_id: str = "akid"
    channel_id: str = ""
    channel_member_id: str = ""
    channel_name: str = ""
    channel_description: str = ""
    channel_aliases: tuple[str, ...] = ()
    channel_wait_for: tuple[str, ...] = ()
    channel_transcript_budget_tokens: int = DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS
    agent_initiation_limit: int = DEFAULT_AGENT_INITIATION_LIMIT
    mqtt_state: Path | None = None
    channel_history_dir: Path | None = None
    channel_media_dir: Path | None = None
    identity_manifest_path: Path | None = None
    identity_registry_path: Path | None = None
    sandbox_enabled: bool = False
    bwrap_bin: str = "bwrap"
    writable_state: Path | None = None
    letta_home: Path | None = None
    tokifactor_image_require_approval: bool = False
    camera_enabled: bool = True
    camera_require_approval: bool = True
    camera_device: str = "/dev/video0"
    camera_width: int = 1280
    camera_height: int = 720
    camera_output_dir: Path | None = None
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str | None = "ffprobe"
    screenshot_enabled: bool = True
    screenshot_require_approval: bool = False
    grim_bin: str = "grim"
    spectacle_bin: str = "spectacle"
    screenshot_output_dir: Path | None = None
    screenshot_wayland_display: str | None = None
    image_generation: ImageGeneration | None = None
    live_context_enabled: bool = True
    live_context_budget_tokens: int = DEFAULT_LIVE_CONTEXT_BUDGET_TOKENS
    zsh_history_path: Path | None = None
    live_context_history_limit: int = DEFAULT_LIVE_CONTEXT_HISTORY_LIMIT
    live_context_process_limit: int = DEFAULT_LIVE_CONTEXT_PROCESS_LIMIT
    live_context_timeout: float = DEFAULT_LIVE_CONTEXT_TIMEOUT
    ps_bin: str = "ps"
    qdbus_bin: str | None = None
    layer_enabled: bool = False
    layer_bin: Path = field(default_factory=default_layer_binary)
    layer_notify_enabled: bool = False
    layer_socket: Path | None = None
    layer_preview_chars: int = 80
    avatar_path: Path | None = field(default_factory=default_avatar_path)
    username: str = "Master"
    username_source: str = "default"
    username_path: Path | None = None
    profile_config_path: Path | None = None
    aki: AkiConfig | None = None
    mcp_native_tools_enabled: bool = True
    mcp_native_servers: tuple[str, ...] = ()
    mcp_native_excluded_servers: tuple[str, ...] = ("local",)
    mcp_native_state: Path | None = None
    mcp_native_max_tools: int = DEFAULT_MCP_NATIVE_MAX_TOOLS
    mcp_tool_timeout: float = DEFAULT_MCP_TOOL_TIMEOUT
    execution_lock_path: Path | None = None
    execution_lock_status_path: Path | None = None
    schedule_enabled: bool = True
    schedule_tick_seconds: int = 15
    schedule_missed_grace_seconds: int = 600
    schedule_max_records: int = 256
    schedule_state: Path | None = None
    presence_enabled: bool = True
    presence_session_id: str = ""
    presence_loginctl_bin: str = "loginctl"
    presence_poll_seconds: int = 15
    presence_idle_reset_seconds: int = 300
    presence_work_warn_seconds: int = 7200
    presence_work_repeat_seconds: int = 3600
    presence_night_start: str = "00:30"
    presence_night_end: str = "05:30"
    presence_night_history_days: int = 30
    presence_return_idle_seconds: int = 2700
    presence_return_debounce_seconds: int = 10
    solo_state: Path | None = None
    solo_memory_path: Path | None = None
    presence_state: Path | None = None
    ledger_state: Path | None = None
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)


def build_letta_command(config: GatewayConfig) -> list[str]:
    """Build the stream-json backend command for one profile.

    Profiles without an explicit ``[tools]`` section keep the historical
    command exactly.  Haru's explicit policy is enforced at schema level with
    ``--tools`` (and again by the permission mods at call time).
    """
    command = [
        config.letta_bin,
        "--backend",
        "local",
        "--agent",
        config.agent_id,
        "--conversation",
        config.conversation_id,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--permission-mode",
        "standard",
    ]
    policy = config.tool_policy
    if policy.explicit:
        command.extend(["--toolset", policy.toolset])
        if policy.allow:
            command.extend(["--tools", ",".join(policy.allow)])
        if policy.deny:
            command.extend(["--disallowedTools", ",".join(policy.deny)])
    return command


@dataclass(eq=False)
class Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connection_id: str
    uid: int | None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    handshaken: bool = False
    client_name: str = ""
    client_version: str = ""
    closed: bool = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    async def send(self, message: Mapping[str, Any]) -> None:
        if self.closed:
            return
        payload = encode_message(message)
        async with self.send_lock:
            if self.closed or self.writer.is_closing():
                return
            self.writer.write(payload)
            await self.writer.drain()


class NullConnection:
    """Event sink for internal proactive turns that must not publish themselves."""

    connection_id = "proactive"
    client_name = "akid-proactive"
    client_version = "1"
    uid: int | None = None
    closed = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = {}

    async def send(self, message: Mapping[str, Any]) -> None:
        return


@dataclass(eq=False)
class ChannelConnection:
    """Synthetic initiator for turns originating on the MQTT channel."""

    server: "GatewayServer"
    connection_id: str = "mqtt-channel"
    client_name: str = "mqtt-channel"
    client_version: str = "1"
    uid: int | None = None
    closed: bool = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    _queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CHANNEL_EVENT_QUEUE_MAX)
    )
    _worker: asyncio.Task[None] | None = None
    _pending_progress: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    _turns: dict[str, "ChannelTurnState"] = field(default_factory=dict)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(
            self._publish_loop(), name="akid-channel-events"
        )

    async def stop(self) -> None:
        self.closed = True
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        self._pending_progress.clear()
        self._turns.clear()

    def _turn_state(self, turn_id: str) -> "ChannelTurnState":
        state = self._turns.get(turn_id)
        if state is None:
            state = ChannelTurnState()
            self._turns[turn_id] = state
        return state

    def _enqueue(self, item: dict[str, Any], *, key: tuple[str, int] | None = None) -> None:
        if key is not None:
            pending = self._pending_progress.get(key)
            if pending is not None:
                pending.update(item)
                return
            item["_key"] = key
            self._pending_progress[key] = item
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if key is not None:
                self._pending_progress.pop(key, None)
            LOGGER.warning("channel event queue full; dropping type=%s", item.get("kind"))
            return
        self._ensure_worker()

    def _enqueue_text(
        self,
        text: str,
        metadata: Mapping[str, Any],
        *,
        key: tuple[str, int] | None = None,
        images: list[dict[str, Any]] | None = None,
        client_message_id: str = "",
    ) -> None:
        values = {key: str(value) for key, value in metadata.items()}
        if client_message_id and "clientMessageId" not in values:
            values["clientMessageId"] = client_message_id
        self._enqueue(
            {
                "kind": str(values.get("kind") or "event"),
                "text": text,
                "metadata": values,
                "images": list(images or []),
            },
            key=key,
        )

    def _enqueue_turn(
        self,
        turn_id: str,
        seq: int,
        phase: str,
        text: str,
        *,
        segment: int = 0,
        wake: str = "",
        wake_ids: str = "",
        images: list[dict[str, Any]] | None = None,
        client_message_id: str = "",
        ignored: bool = False,
    ) -> None:
        metadata = {
            "schema": "akid.v1",
            "kind": "turn",
            "turnId": turn_id,
            "segment": segment,
            "seq": seq,
            "phase": phase,
            "format": "markdown",
        }
        if phase == "final":
            metadata["wake"] = wake or CHANNEL_WAKE_ALL
            if wake_ids:
                metadata["wakeIds"] = wake_ids
        if ignored:
            metadata["ignored"] = "true"
        self._enqueue_text(
            text,
            metadata,
            key=(turn_id, segment) if phase == "progress" else None,
            images=images,
            client_message_id=client_message_id,
        )

    async def _flush_progress(
        self,
        turn_id: str,
        seq: int,
        state: "ChannelTurnState",
        *,
        force: bool = False,
        phase: str = "progress",
    ) -> None:
        members = self.server._channel_member_views()
        raw_text = state.text.strip()
        if contains_no_reply_marker(raw_text):
            state.no_reply = True
            state.text = ""
            return
        cleaned_text, images = await self.server._extract_agent_channel_images(
            raw_text, load_data=phase == "final"
        )
        wake = ""
        wake_ids = ""
        if phase == "final":
            if self.server._solo_session is not None:
                text = strip_channel_mentions(cleaned_text, members)
                wake = CHANNEL_WAKE_USER
                wake_ids = ""
            else:
                route = route_channel_reply(
                    cleaned_text,
                    members,
                    user_name=self.server.config.username,
                )
                text = route.text.strip()
                mentioned = match_channel_mentions(text, members)
                blocked = {
                    member_id: self.server._solo_targets.get(member_id, {})
                    for member_id in mentioned
                    if member_id in self.server._solo_targets
                }
                if blocked:
                    target_ids = tuple(
                        member_id
                        for member_id in route.target_ids
                        if member_id not in blocked
                    )
                    if route.wake == CHANNEL_WAKE_MENTIONS and not target_ids:
                        route_wake = CHANNEL_WAKE_USER
                        route_target_ids: tuple[str, ...] = ()
                    else:
                        route_wake = route.wake
                        route_target_ids = target_ids
                    names = [
                        str(value.get("target_name") or member_id)
                        for member_id, value in blocked.items()
                    ]
                    self.server._enqueue_solo_notice(", ".join(names))
                else:
                    route_wake = route.wake
                    route_target_ids = route.target_ids
                wake = route_wake if text else CHANNEL_WAKE_USER
                wake_ids = ",".join(route_target_ids) if text else ""
            if not text and not images:
                return
            state.wake = wake
            state.wake_ids = wake_ids
        else:
            text, _excluded = strip_channel_non_wakes(cleaned_text, members)
            if self.server._solo_session is not None:
                text = strip_channel_mentions(text, members)
            if not text:
                return
        if not force and text == state.last_text:
            return
        self._enqueue_turn(
            turn_id,
            seq,
            phase,
            text,
            segment=state.segment,
            wake=wake,
            wake_ids=wake_ids,
            images=images,
            client_message_id=state.client_message_id,
        )
        state.last_text = text

    async def _publish_loop(self) -> None:
        while True:
            item = await self._queue.get()
            key = item.pop("_key", None)
            if key is not None:
                self._pending_progress.pop(key, None)
            channel = self.server.channel
            if channel is None:
                self._queue.task_done()
                continue
            try:
                if item.get("images"):
                    published = await channel.publish_message(
                        item["text"], item.get("images") or (), item["metadata"]
                    )
                else:
                    await channel.publish_text(item["text"], item["metadata"])
                    published = {}
                phase = str(item["metadata"].get("phase") or "")
                if item.get("kind") == "turn" and phase in ("progress", "final"):
                    attachments: list[ChannelImageAttachment] = []
                    for index, source in enumerate(item.get("images") or []):
                        stored = self.server._store_channel_image(
                            ImageEnvelope(
                                sender=self.server.config.channel_name,
                                data=bytes(source["data"]),
                                sender_id=self.server._channel_self_key(),
                                message_id=str(published.get("message_id") or ""),
                                name=str(source.get("name") or ""),
                                attachment_index=index,
                                attachment_count=len(item.get("images") or []),
                                sent_at=int(time.time() * 1000),
                                mime=str(source.get("mime") or ""),
                                transfer_id=str(
                                    (published.get("images") or [{}])[index].get("transfer_id")
                                    if index < len(published.get("images") or ())
                                    else ""
                                ),
                            ),
                            name=str(source.get("name") or f"图片{index + 1}"),
                            index=index + 1,
                        )
                        if stored is not None:
                            attachments.append(stored)
                    self.server._record_channel_message(
                        sender=self.server.config.channel_name,
                        sender_id=self.server._channel_self_key(),
                        text=str(item["text"]),
                        from_agent=True,
                        phase=phase,
                        attachments=tuple(attachments),
                    )
                    if phase == "final":
                        self.server._notify_layer(
                            self.server._layer_own_character(),
                            str(item["text"]),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "channel publish failed type=%s error=%s", item.get("kind"), exc
                )
            finally:
                self._queue.task_done()

    async def send(self, message: Mapping[str, Any]) -> None:
        if self.closed or message.get("method") != "aki.event":
            return
        params = message.get("params") or {}
        event_type = params.get("type")
        data = params.get("data") or {}
        if self.server.channel is None:
            return
        turn_id = str(params.get("turn_id") or "")
        seq_value = params.get("seq")
        seq = int(seq_value) if isinstance(seq_value, int) else 0
        if not turn_id:
            return
        state = self._turn_state(turn_id)
        if state.suppressed and event_type == "turn_end":
            self._turns.pop(turn_id, None)
            return
        if state.suppressed:
            return
        if state.guard_blocked:
            if event_type == "turn_end":
                self._enqueue_turn(
                    turn_id,
                    seq,
                    "ended",
                    "",
                    client_message_id=state.client_message_id,
                    ignored=True,
                )
                self._turns.pop(turn_id, None)
            return
        if event_type == "turn_started":
            state.client_message_id = str(data.get("client_message_id") or "")
            state.origin_is_agent = bool(data.get("origin_is_agent"))
            if state.origin_is_agent and not state.initiation_charged:
                state.initiation_charged = self.server._claim_agent_initiation(turn_id)
                if not state.initiation_charged:
                    state.suppressed = True
                    return
            self._enqueue_turn(
                turn_id,
                seq,
                "started",
                "Aki is working…",
                client_message_id=state.client_message_id,
            )
            state.last_emit = asyncio.get_running_loop().time()
            return
        if event_type == "assistant_delta":
            if data.get("reasoning"):
                return
            delta = str(data.get("text") or "")
            if not delta:
                return
            state.text += delta
            return
        if event_type == "assistant_segment":
            await self._flush_progress(turn_id, seq, state, force=True)
            state.segment += 1
            state.text = ""
            state.last_text = ""
            state.finalized = False
            return
        if event_type == "assistant_message":
            full = str(data.get("content") or data.get("text") or "")
            if contains_no_reply_marker(full):
                state.no_reply = True
                state.text = ""
                return
            if state.segment == 0 and not state.text.strip() and full:
                state.text = full
            await self._flush_progress(turn_id, seq, state, force=True, phase="final")
            state.finalized = True
            return
        if event_type == "tool_call":
            await self._flush_progress(turn_id, seq, state, force=True)
            tool_name = str(data.get("name") or "unknown")
            tool_call_id = str(data.get("tool_call_id") or "")
            if tool_call_id:
                state.tool_names[tool_call_id] = tool_name
            self._enqueue_text(
                f"tool: {tool_name} running",
                {
                    "schema": "akid.v1",
                    "kind": "tool",
                    "turnId": turn_id,
                    "seq": seq,
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "toolStatus": "running",
                },
                client_message_id=state.client_message_id,
            )
            state.segment += 1
            state.text = ""
            state.last_text = ""
            state.finalized = False
            return
        if event_type == "tool_result":
            status = str(data.get("status") or "success").lower()
            tool_name = str(state.tool_names.get(str(data.get("tool_call_id") or ""), "tool"))
            self._enqueue_text(
                f"tool: {tool_name} {status}",
                {
                    "schema": "akid.v1",
                    "kind": "tool",
                    "turnId": turn_id,
                    "seq": seq,
                    "toolCallId": str(data.get("tool_call_id") or ""),
                    "toolName": tool_name,
                    "toolStatus": "error" if status == "error" else "success",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "approval_request":
            await self._flush_progress(turn_id, seq, state, force=True)
            approval_id = str(data.get("approval_id") or "")
            tool_name = str(data.get("tool_name") or "unknown")
            tool_call_id = str(data.get("tool_call_id") or "")
            if tool_call_id:
                state.tool_names[tool_call_id] = tool_name
            try:
                rendered = json.dumps(data.get("input"), ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered = str(data.get("input"))
            if len(rendered) > 2000:
                rendered = rendered[:2000] + "\n... (truncated)"
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
            lines.extend([f"reply: akid-approve {approval_id}", f"       akid-deny {approval_id}"])
            self._enqueue_text(
                "\n".join(lines),
                {
                    "schema": "akid.v1",
                    "kind": "approval",
                    "turnId": turn_id,
                    "seq": seq,
                    "approvalId": approval_id,
                    "requesterId": str(data.get("requester_id") or ""),
                    "toolName": tool_name,
                    "toolCallId": tool_call_id,
                    "timeoutS": str(timeout_s if timeout_s is not None else ""),
                    "details": rendered if rendered and rendered != "null" else "",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "approval_resolved":
            approval_id = str(data.get("approval_id") or "")
            decision = str(data.get("decision") or "deny")
            tool_name = str(data.get("tool_name") or "tool")
            self._enqueue_text(
                f"akid: {decision} {approval_id} (tool={tool_name})",
                {
                    "schema": "akid.v1",
                    "kind": "approval_resolved",
                    "turnId": turn_id,
                    "seq": seq,
                    "approvalId": approval_id,
                    "toolName": tool_name,
                    "decision": decision,
                    "details": str(data.get("message") or ""),
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "error":
            text = str(data.get("message") or data.get("code") or "unknown error")
            self._enqueue_text(
                f"akid error: {text[:1000]}",
                {
                    "schema": "akid.v1",
                    "kind": "error",
                    "turnId": turn_id,
                    "seq": seq,
                    "phase": "ended",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "turn_end":
            if not state.no_reply and not state.finalized:
                await self._flush_progress(
                    turn_id, seq, state, force=True, phase="final"
                )
            self._enqueue_turn(
                turn_id,
                seq,
                "no_reply" if state.no_reply else "ended",
                "",
                client_message_id=state.client_message_id,
            )
            self._turns.pop(turn_id, None)
            return

    def discard_turn_output(self, turn_id: str) -> None:
        """Drop buffered/public output for a turn rejected by the wait guard."""
        state = self._turn_state(turn_id)
        state.text = ""
        state.last_text = ""
        state.finalized = True
        state.guard_blocked = True


@dataclass
class ChannelTurnState:
    segment: int = 0
    text: str = ""
    last_text: str = ""
    finalized: bool = False
    no_reply: bool = False
    origin_is_agent: bool = False
    initiation_charged: bool = False
    suppressed: bool = False
    guard_blocked: bool = False
    tool_names: dict[str, str] = field(default_factory=dict)
    wake: str = ""
    wake_ids: str = ""
    client_message_id: str = ""


@dataclass
class Approval:
    approval_id: str
    turn: "Turn"
    future: asyncio.Future[tuple[bool, str | None]]
    tool_name: str
    tool_call_id: str


@dataclass
class Turn:
    turn_id: str
    conn: Connection
    message: str
    include_reasoning: bool
    client_message_id: str | None
    origin: str = "mqtt"
    origin_sender_id: str = ""
    origin_is_agent: bool = False
    lock_authorized: bool = True
    lock_authorization: str = "local-turn"
    lock_held: bool = False
    channel_message_seq: int | None = None
    channel_participants: frozenset[str] = frozenset()
    channel_sender: str = ""
    channel_text: str = ""
    channel_attachments: tuple[ChannelImageAttachment, ...] = ()
    conversation_snapshot: dict[str, Any] | None = None
    bridge_snapshot_id: str | None = None
    bridge_turn_started: bool = False
    busy_messages: list[dict[str, Any]] = field(default_factory=list)
    user_message_event: asyncio.Event = field(default_factory=asyncio.Event)
    wait_user_call_active: bool = False
    overlay_files: dict[int, Path] = field(default_factory=dict)
    overlay_consumed_seqs: set[int] = field(default_factory=set)
    wait_user_eligible_seqs: set[int] = field(default_factory=set)
    waited_user_seqs: set[int] = field(default_factory=set)
    wait_user_used: bool = False
    merged_user_messages: list[dict[str, Any]] = field(default_factory=list)
    user_role_entry_id: str = ""
    waited_agent_ids: set[str] = field(default_factory=set)
    wait_guard_targets: tuple[tuple[str, str, str], ...] = ()
    wait_guard_attempt: int = 0
    wait_guard_blocked: bool = False
    tool_result_events: int = 0
    pending_tool_results: int = 0
    seq: int = 0
    state: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False
    end_sent: bool = False
    assistant_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls_seen: set[str] = field(default_factory=set)
    tool_names: dict[str, str] = field(default_factory=dict)
    tool_results_seen: set[str] = field(default_factory=set)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    final_result: dict[str, Any] = field(default_factory=dict)
    trigger_kind: str = ""
    trigger_facts: dict[str, Any] = field(default_factory=dict)
    proactive_id: str = ""


@dataclass
class AgentChannelState:
    active: bool = False
    active_turn_id: str = ""
    queue_depth: int = 0
    last_final_text: str = ""
    last_final_turn_id: str = ""
    last_completion_seq: int = 0
    last_completion_phase: str = ""
    last_completion_sent_at: int = 0
    last_status_seq: int = 0
    last_status_sent_at: int = 0
    last_turn_event_sent_at: int = 0


@dataclass
class WaitOtherResult:
    member_id: str
    name: str
    status: str
    text: str = ""
    turn_id: str = ""


class GatewayServer:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        backend_factory: Callable[
            [Callable[[dict[str, Any]], Awaitable[None]], Callable[[dict[str, Any]], Awaitable[None]], Callable[[int | None, str | None], Awaitable[None]]],
            Any,
        ]
        | None = None,
    ) -> None:
        self.config = config
        if not config.channel_name:
            config.channel_name = "Aki" if config.profile == "aki" else config.profile
        if not config.channel_member_id:
            config.channel_member_id = (
                config.channel_id if is_member_id(config.channel_id) else ""
            ) or legacy_member_id(config.channel_name)
        if not config.channel_id:
            config.channel_id = config.channel_member_id
        elif not is_member_id(config.channel_id):
            config.channel_id = config.channel_member_id
        config.mqtt_sender_id = config.channel_member_id
        if config.mqtt_sender_name == "akid":
            config.mqtt_sender_name = config.channel_name
        if not config.channel_aliases:
            config.channel_aliases = (
                ("Aki", "aki", "秋", "小秋", "秋（aki）")
                if config.profile == "aki"
                else (config.channel_name,)
            )
        self._identity_registry = (
            IdentityRegistry(config.identity_registry_path)
            if config.identity_registry_path is not None
            else None
        )
        self._identity_manifest_path = config.identity_manifest_path
        if self._identity_registry is not None:
            existing = self._identity_registry.get(config.channel_member_id)
            created_at = existing.created_at if existing is not None else ""
            identity = MemberIdentity(
                id=config.channel_member_id,
                name=config.channel_name,
                aliases=config.channel_aliases,
                kind=MEMBER_KIND_AGENT,
                created_at=created_at or "",
            )
            claimed, conflicts = self._identity_registry.observe(
                identity, source=f"profile:{config.profile}"
            )
            if not claimed:
                for conflict in conflicts:
                    LOGGER.warning(
                        "local identity label conflict label=%s owner=%s member=%s",
                        conflict.get("label"),
                        conflict.get("owner"),
                        conflict.get("member_id"),
                    )
        self._channel_members: dict[str, dict[str, Any]] = {}
        self._channel_agent_states: dict[str, AgentChannelState] = {}
        self._channel_waiting_user: dict[str, float] = {}
        self._wait_user_active = False
        self._wait_other_active = False
        self._wait_other_targets: tuple[str, ...] = ()
        self._channel_event_seq = 0
        self._channel_history: list[ChannelTranscriptEntry] = []
        self._channel_history_seq = 0
        self._channel_transcript_status: dict[str, Any] = {
            "budget_tokens": self.config.channel_transcript_budget_tokens,
            "entries": 0,
            "last_seq": 0,
            "ephemeral_turns": 0,
        }
        self._channel_transcript_lock = asyncio.Lock()
        self._solo_lock = asyncio.Lock()
        self.channel_history_dir = config.channel_history_dir or (
            (config.profile_config_path.parent / "channel-history")
            if config.profile_config_path is not None
            else (config.backend_dir / "channel-history")
        )
        self.channel_media_dir = config.channel_media_dir or (
            self.channel_history_dir / "media"
        )
        self.public_memory = PublicMemoryStore(
            default_public_memory_path(
                config.backend_dir,
                channel_history_key(
                    host=config.mqtt_host,
                    port=config.mqtt_port,
                    topic=config.mqtt_topic,
                    magic=config.mqtt_magic,
                ),
            ),
            logger=logging.getLogger("akid.public-memory"),
        )
        self.solo_state_path = config.solo_state or default_solo_state_path(
            config.profile
        )
        loaded_solo = load_solo_session(self.solo_state_path)
        self._solo_session = (
            loaded_solo
            if loaded_solo is not None
            and loaded_solo.target_id == self._channel_self_key()
            and loaded_solo.profile == config.profile
            else None
        )
        self._solo_targets: dict[str, dict[str, str]] = {}
        self._solo_scene = ""
        self._frozen_group_history: list[ChannelTranscriptEntry] = []
        self._frozen_group_history_seq = 0
        self.group_channel_history_path = self._channel_history_path(solo=False)
        self.solo_channel_history_path = self._channel_history_path(solo=True)
        self.channel_history_path = (
            self.solo_channel_history_path
            if self._solo_session is not None
            else self.group_channel_history_path
        )
        self._channel_pending_images: dict[str, dict[str, Any]] = {}
        self._channel_attachment_tasks: set[asyncio.Task[Any]] = set()
        if self._solo_session is not None:
            self._load_channel_history(self.group_channel_history_path)
            self._frozen_group_history = list(self._channel_history)
            self._frozen_group_history_seq = self._channel_history_seq
            self._load_channel_history(self.solo_channel_history_path)
        else:
            self._load_channel_history()
        self._solo_memory_start_seq = int(self._channel_history_seq)
        self._agent_initiation_remaining = max(
            0, int(config.agent_initiation_limit)
        )
        self.connection_set: set[Connection] = set()
        self.queue: list[Turn] = []
        self.active: Turn | None = None
        self.approvals: dict[str, Approval] = {}
        self.last_usage: dict[str, Any] | None = None
        self.started_at = time.time()
        self._server: asyncio.AbstractServer | None = None
        self._layer: LayerProcess | None = None
        self._layer_start_task: asyncio.Task[None] | None = None
        self._layer_display_name: str | None = None
        self._layer_notifier: LayerNotifier | None = None
        self._layer_skip_reason: str | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._approval_tasks: set[asyncio.Task[Any]] = set()
        self._turn_tasks: set[asyncio.Task[Any]] = set()
        self._channel_tasks: set[asyncio.Task[Any]] = set()
        self._external_tool_tasks: set[asyncio.Task[Any]] = set()
        self._backend_queue: deque[dict[str, Any]] = deque()
        self._backend_current_batch: dict[str, Any] | None = None
        self._token_cache: tuple[float, dict[str, Any] | None, str | None] = (0.0, None, None)
        self._token_lock = asyncio.Lock()
        self._status_event = asyncio.Event()
        self._status_stop = asyncio.Event()
        self._status_task: asyncio.Task[None] | None = None
        self._probe_seen: dict[str, float] = {}
        self._probe_rate: dict[str, float] = {}
        self._writable_lock = asyncio.Lock()
        self._mqtt_lock = asyncio.Lock()
        self._reset_lock = asyncio.Lock()
        self._username_lock = asyncio.Lock()
        self._recompile_lock = asyncio.Lock()
        self._compact_lock = asyncio.Lock()
        self._live_context_lock = asyncio.Lock()
        self._tool_ownership_lock = asyncio.Lock()
        self._wake_routing_lock = asyncio.Lock()
        self._kde_detected_at = 0.0
        self._kde_detected = False
        self.config.live_context_budget_tokens = clamp_budget_tokens(
            self.config.live_context_budget_tokens
        )
        self._live_context_status: dict[str, Any] = {
            "enabled": self.config.live_context_enabled,
            "budget_tokens": self.config.live_context_budget_tokens,
            "last_captured_at": None,
            "last_sections": {},
            "last_errors": [],
            "block_bytes": 0,
            "agent_path": None,
            "updated": False,
        }
        self._recompiling = False
        self._compacting = False
        self._resetting = False
        self._policy_restarting = False
        self._shutting_down = False
        self.memory_dir = config.memory_dir or default_memory_dir(config.agent_id)
        solo_memory_path = config.solo_memory_path
        if solo_memory_path is None:
            if config.profile_config_path is not None:
                solo_memory_path = (
                    config.profile_config_path.parent
                    / "solo-memory"
                    / f"{config.profile}.json"
                )
            else:
                solo_memory_path = default_solo_memory_path(config.profile)
        self.solo_memory = SoloMemoryManager(
            profile=config.profile,
            path=solo_memory_path,
            memory_dir=self.memory_dir,
            budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
            logger=logging.getLogger("akid.solo-memory"),
        )
        self.mqtt_state_path = config.mqtt_state or default_mqtt_state_path()
        self.writable = WritableRegistry(
            state_path=config.writable_state,
            letta_home=config.letta_home,
            backend_dir=config.backend_dir,
            bwrap_bin=config.bwrap_bin,
        )
        self.writable.load()
        self._sandbox_blocked_reason: str | None = None
        self._external_tools_registered = False
        self._external_tools_error: str | None = None
        self._external_tools_pending = False
        self._external_tools_ready = asyncio.Event()
        self._external_tools_ready.set()
        self.operation_journal = OperationJournal()
        state_root = state_dir()
        self.schedule_state = config.schedule_state or (
            state_root / "schedules" / f"{config.profile}.json"
        )
        self.ledger_state = config.ledger_state or (
            state_root / "ledger" / f"{config.profile}.json"
        )
        self.presence_state = config.presence_state or (
            state_root / "presence" / f"{config.profile}.json"
        )
        self.schedule_store = ScheduleStore(
            self.schedule_state,
            max_records=config.schedule_max_records,
            missed_grace_seconds=config.schedule_missed_grace_seconds,
        )
        self.life_ledger = LifeLedger(self.ledger_state)
        self.presence_monitor = PresenceMonitor(
            self.presence_state,
            provider=LogindActivityProvider(
                loginctl_bin=config.presence_loginctl_bin,
                session_id=config.presence_session_id,
                input_probe=EvdevActivityProbe(),
                input_idle_seconds=config.presence_idle_reset_seconds,
            ),
            poll_seconds=config.presence_poll_seconds,
            idle_reset_seconds=config.presence_idle_reset_seconds,
            work_warn_seconds=config.presence_work_warn_seconds,
            work_repeat_seconds=config.presence_work_repeat_seconds,
            night_start=config.presence_night_start,
            night_end=config.presence_night_end,
            night_history_days=config.presence_night_history_days,
            return_idle_seconds=config.presence_return_idle_seconds,
            return_debounce_seconds=config.presence_return_debounce_seconds,
        )
        self._schedule_stop = asyncio.Event()
        self._schedule_task: asyncio.Task[None] | None = None
        self._proactive_pending: dict[str, dict[str, Any]] = {}
        self._proactive_lock = asyncio.Lock()
        self._last_proactive_at: str | None = None
        self._mcp_tools: list[dict[str, Any]] = []
        self._mcp_tool_names: set[str] = set()
        self._mcp_tools_error: str | None = None
        self._mcp_native_active_servers: list[str] = []
        self._mcp_native_excluded: set[str] = set(
            config.mcp_native_excluded_servers
        )
        self.mcp_native_state_path = (
            config.mcp_native_state or default_mcp_native_state_path()
        )
        self._mcp_lock = asyncio.Lock()
        self._load_mcp_native_state()
        lock_path = config.execution_lock_path or default_execution_lock_path()
        self.execution_lock = ExecutionLock(
            path=lock_path,
            status_path=(
                config.execution_lock_status_path
                or default_execution_lock_status_path(lock_path)
            ),
        )
        self.avatar_path = config.avatar_path or default_avatar_path()
        self.avatar: AvatarAsset | None = None
        self.avatar_error: str | None = None
        try:
            self.avatar = load_avatar(self.avatar_path)
        except AvatarError as exc:
            self.avatar_error = str(exc)
            LOGGER.warning("avatar unavailable path=%s error=%s", self.avatar_path, exc)
        self.channel_conn = ChannelConnection(self)
        self.proactive_conn = NullConnection()
        self.channel: TokifactorChannel | None = None
        if config.mqtt_enabled:
            active_magic = (
                derive_solo_magic(config.mqtt_magic, self._channel_self_key())
                if self._solo_session is not None
                else config.mqtt_magic
            )
            self.channel = TokifactorChannel(
                host=config.mqtt_host,
                port=config.mqtt_port,
                magic=active_magic,
                session_magic=config.mqtt_magic,
                topic=config.mqtt_topic,
                sender_name=config.mqtt_sender_name,
                sender_id=config.mqtt_sender_id,
                avatar_bytes=self.avatar.data if self.avatar is not None else None,
                avatar_hash=self.avatar.sha256 if self.avatar is not None else "",
                avatar_mime=self.avatar.mime if self.avatar is not None else "",
                dedup_path=self.channel_history_path.with_suffix(".seen.json"),
                avatar_cache_path=self.channel_history_path.with_suffix(".avatars"),
                on_text=self._on_channel_text,
                on_image=self._on_channel_image,
                on_control=self._on_channel_control,
                on_connected=self._on_channel_connected,
                logger=logging.getLogger("akid.channel"),
            )
        if backend_factory is None:
            backend_factory = self._default_backend_factory
        self.backend = backend_factory(
            self._on_backend_message, self._on_backend_ready, self._on_backend_exit
        )

    def _default_backend_factory(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_ready: Callable[[dict[str, Any]], Awaitable[None]],
        on_exit: Callable[[int | None, str | None], Awaitable[None]],
    ) -> Any:
        letta_command = build_letta_command(self.config)
        command = letta_command
        env = dict(os.environ)
        env.update(
            {
                "AKI_PROFILE": self.config.profile,
                "AKI_TURN_OVERLAY_DIR": str(self._turn_overlay_dir()),
                "AKI_EXECUTION_LOCK_STATUS": str(
                    self.execution_lock.status_path
                ),
            }
        )
        if self.config.sandbox_enabled:
            command = [
                sys.executable,
                str(Path(__file__).with_name("sandbox_exec.py")),
                "--state",
                str(self.writable.state_path),
                "--bwrap",
                self.config.bwrap_bin,
                "--",
                *letta_command,
            ]
            env.update(
                {
                    "AKI_SANDBOX_ENABLED": "1",
                    "AKI_BWRAP_BIN": self.config.bwrap_bin,
                    "AKI_WRITABLE_STATE": str(self.writable.state_path),
                    "LETTA_HOME": str(self.writable.letta_home),
                    "LETTA_LOCAL_BACKEND_DIR": str(self.writable.backend_dir),
                }
            )
        return LettaProcess(
            command,
            cwd=str(self.config.cwd),
            env=env,
            on_message=on_message,
            on_ready=on_ready,
            on_exit=on_exit,
            logger=logging.getLogger("akid.letta"),
        )

    def _turn_overlay_dir(self) -> Path:
        configured = os.environ.get("AKI_TURN_OVERLAY_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()
        return default_turn_overlay_dir(
            self.config.backend_dir, self.config.agent_id
        )

    # ------------------------------------------------------------------ server
    async def _abort_startup(self, message: str) -> None:
        LOGGER.error("refusing to start: %s", message)
        with contextlib.suppress(Exception):
            await self.backend.stop()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()
        raise RuntimeError(message)

    async def start(self) -> None:
        path = self.config.socket_path
        parent = path.parent
        existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = parent.stat()
        if existing.st_uid != os.getuid():
            raise RuntimeError(f"socket directory is owned by uid {existing.st_uid}")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        dedicated_dirs = {Path(f"/tmp/aki-{os.getuid()}")}
        if runtime:
            dedicated_dirs.add(Path(runtime) / "aki")
        if not existed or parent in dedicated_dirs:
            os.chmod(parent, 0o700)
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError(f"refusing to replace non-socket path {path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"another akid may be running on {path}: {exc}") from exc
            else:
                raise RuntimeError(f"another akid is already listening on {path}")
            finally:
                probe.close()
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(path),
            limit=MAX_LINE_BYTES + 2,
        )
        os.chmod(path, 0o600)
        self._clear_live_context_block()
        self._clear_channel_transcript_block()
        self._clear_channel_identity_block()
        await self._refresh_channel_policy()
        await self._refresh_channel_wake_routing()
        await self._refresh_channel_identity()
        self._backfill_public_memory()
        await self._refresh_channel_transcript()
        await self._refresh_tool_ownership()
        if self.config.aki is not None:
            try:
                self._prepare_letta_provider()
            except Exception:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                raise
        sandbox_available = True
        if self.config.sandbox_enabled:
            self.writable.ensure_runtime_dirs()
            available, bwrap_path, error = self.writable.probe(refresh=True)
            sandbox_available = available
            if not available:
                self._sandbox_blocked_reason = error or "sandbox unavailable"
                LOGGER.error("sandbox unavailable; Letta backend blocked: %s", self._sandbox_blocked_reason)
            else:
                LOGGER.info("sandbox ready backend=bwrap path=%s", bwrap_path)
        if sandbox_available:
            self.backend.start()
            if not await self._wait_backend_ready(30.0):
                await self._abort_startup(
                    "Letta backend did not become ready; refusing to run"
                )
            if not self._aki_bridge_available():
                await self._abort_startup(
                    "Aki requires the customized Letta runtime with aki_bridge; "
                    "the configured backend does not advertise it"
                )
        if self.channel is not None:
            await self.channel.start()
            self._start_channel_status_loop()
        self._start_layer()
        self._start_layer_notifier()
        self._start_proactive_services()
        memory_config = self._solo_memory_llm_config()
        if memory_config is not None and any(
            session.summary_status == FALLBACK_SUMMARY_STATUS
            for session in self.solo_memory.sessions
        ):
            task = asyncio.create_task(
                self.solo_memory.retry_fallback_summaries(
                    **memory_config,
                    timeout=SOLO_SUMMARY_TIMEOUT_SECONDS,
                ),
                name="akid-solo-summary-retry",
            )
            self._channel_tasks.add(task)
            task.add_done_callback(self._channel_tasks.discard)
        LOGGER.info("akid listening on %s", path)

    async def stop(self) -> None:
        self._shutting_down = True
        await self._stop_proactive_services()
        server = self._server
        if server is not None:
            server.close()
        for conn in list(self.connection_set):
            conn.closed = True
            with contextlib.suppress(Exception):
                conn.writer.close()
        all_tasks = (
            list(self._client_tasks)
            + list(self._approval_tasks)
            + list(self._turn_tasks)
            + list(self._channel_tasks)
            + list(self._external_tool_tasks)
        )
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.active is not None:
            self._release_turn_lock(self.active)
        if server is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await server.wait_closed()
            self._server = None
        self.connection_set.clear()
        self.channel_conn.closed = True
        await self.channel_conn.stop()
        self._status_stop.set()
        self._status_event.set()
        status_task = self._status_task
        self._status_task = None
        if status_task is not None:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await status_task
        if self.channel is not None:
            if self.channel.connected:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._publish_channel_offline(), timeout=2.0)
            with contextlib.suppress(Exception):
                await self.channel.stop()
        notifier = self._layer_notifier
        self._layer_notifier = None
        if notifier is not None:
            with contextlib.suppress(Exception):
                await notifier.stop()
        layer_start_task = self._layer_start_task
        self._layer_start_task = None
        if layer_start_task is not None:
            layer_start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await layer_start_task
        layer = self._layer
        self._layer = None
        if layer is not None:
            with contextlib.suppress(Exception):
                await layer.stop()
        with contextlib.suppress(Exception):
            await self.backend.stop()
        self.execution_lock.close()
        self._clear_live_context_block()
        self._clear_channel_transcript_block()
        self._clear_channel_identity_block()
        self._clear_tool_ownership_block()
        self._clear_execution_lock_policy()
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()
        LOGGER.info("akid stopped")

    def _start_layer(self) -> None:
        self._layer_skip_reason = None
        if not self.config.layer_enabled:
            self._layer_skip_reason = "disabled"
            LOGGER.info("aki-layer disabled")
            return
        if self._layer is not None or self._layer_start_task is not None:
            return
        binary = self.config.layer_bin
        if not binary.is_file() or not os.access(binary, os.X_OK):
            self._layer_skip_reason = f"binary not executable: {binary}"
            LOGGER.warning("aki-layer unavailable: %s", self._layer_skip_reason)
            return
        self._layer_start_task = asyncio.create_task(
            self._bootstrap_layer(binary), name="aki-layer-bootstrap"
        )

    async def _bootstrap_layer(self, binary: Path) -> None:
        try:
            while not self._shutting_down and self._layer is None:
                display = discover_wayland_display(
                    configured=os.environ.get("WAYLAND_DISPLAY")
                )
                if display:
                    self._launch_layer(binary, display)
                    return
                if self._layer_skip_reason != "waiting for Wayland display":
                    self._layer_skip_reason = "waiting for Wayland display"
                    LOGGER.info(
                        "aki-layer waiting for a Wayland display runtime=%s",
                        os.environ.get("XDG_RUNTIME_DIR", ""),
                    )
                await asyncio.sleep(LAYER_DISPLAY_RETRY_SECONDS)
        finally:
            self._layer_start_task = None

    def _launch_layer(self, binary: Path, display: str) -> None:
        env = dict(os.environ)
        env["WAYLAND_DISPLAY"] = display
        env.setdefault("XDG_SESSION_TYPE", "wayland")
        env.setdefault("QT_QPA_PLATFORM", "wayland")
        layer = LayerProcess(
            [str(binary)],
            cwd=str(self.config.cwd),
            env=env,
            logger=logging.getLogger("akid.layer"),
        )
        layer.start()
        self._layer = layer
        self._layer_display_name = display
        self._layer_skip_reason = None
        LOGGER.info("aki-layer starting binary=%s display=%s", binary, display)

    def _start_layer_notifier(self) -> None:
        if not self.config.layer_notify_enabled or self.config.layer_socket is None:
            return
        notifier = LayerNotifier(
            self.config.layer_socket,
            preview_chars=self.config.layer_preview_chars,
            logger=logging.getLogger("akid.layer"),
        )
        notifier.start()
        self._layer_notifier = notifier
        LOGGER.info("aki-layer notifier socket=%s", self.config.layer_socket)

    def _notify_layer(self, character: str | None, text: str) -> None:
        notifier = self._layer_notifier
        if notifier is None or not character:
            return
        if contains_no_reply_marker(text):
            return
        notifier.notify(character, text)

    async def _publish_layer_presence(self) -> None:
        notifier = self._layer_notifier
        if notifier is None:
            return
        notifier.notify_presence(self.presence_monitor.layer_status())

    def _layer_own_character(self) -> str:
        return character_for_profile(self.config.profile, self.config.channel_name)

    def _layer_character_for_member(self, member_id: str, name: str) -> str | None:
        member = self._channel_members.get(member_id) if member_id else None
        aliases = tuple(member.get("aliases") or ()) if member is not None else ()
        member_name = str(member.get("name") or "") if member is not None else ""
        return character_for_names(name, member_name, member_id, *aliases)

    def _layer_health(self) -> dict[str, Any]:
        status = (
            self._layer.status()
            if self._layer is not None
            else {
                "state": "stopped",
                "pid": None,
                "command": [str(self.config.layer_bin)],
                "cwd": str(self.config.cwd),
                "restart_count": 0,
                "last_error": None,
                "started_at": None,
                "stderr_tail": [],
            }
        )
        status.update(
            {
                "enabled": bool(self.config.layer_enabled),
                "available": self._layer is not None,
                "display": self._layer_display_name
                or os.environ.get("WAYLAND_DISPLAY"),
                "skip_reason": self._layer_skip_reason,
            }
        )
        if self._layer_notifier is not None:
            status["notifier"] = self._layer_notifier.status()
        return status

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        except OSError:
            return None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        uid = self._peer_uid(writer)
        if uid is not None and uid != os.getuid():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            LOGGER.warning("rejected peer uid=%s", uid)
            return
        conn = Connection(
            reader=reader,
            writer=writer,
            connection_id=f"conn-{uuid.uuid4().hex[:12]}",
            uid=uid,
        )
        self.connection_set.add(conn)
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            while not conn.closed:
                try:
                    raw = await read_message(reader)
                except FrameError as exc:
                    await conn.send(make_error(None, ErrorCode.PARSE_ERROR, str(exc)))
                    break
                if raw is None:
                    break
                request_id: Any = None
                try:
                    value = decode_message(raw)
                    if isinstance(value, dict):
                        request_id = value.get("id")
                    request = parse_request(value)
                except FrameError as exc:
                    await conn.send(make_error(request_id, ErrorCode.PARSE_ERROR, str(exc)))
                    continue
                except RpcError as exc:
                    await conn.send(make_error(request_id, exc.code, exc.message, exc.data))
                    continue

                try:
                    result, post = await self._dispatch(conn, request)
                except RpcError as exc:
                    if not request.is_notification:
                        await conn.send(
                            make_error(request.id, exc.code, exc.message, exc.data)
                        )
                    continue
                except Exception:
                    LOGGER.exception("request handler failed method=%s", request.method)
                    if not request.is_notification:
                        await conn.send(
                            make_error(
                                request.id,
                                ErrorCode.INTERNAL_ERROR,
                                "internal gateway error",
                            )
                        )
                    continue

                if not request.is_notification:
                    try:
                        await conn.send(make_response(request.id, result))
                    except FrameError:
                        LOGGER.error(
                            "response exceeds frame limit method=%s", request.method
                        )
                        await conn.send(
                            make_error(
                                request.id,
                                ErrorCode.INTERNAL_ERROR,
                                "response exceeds the 1 MiB frame limit",
                            )
                        )
                if post is not None:
                    maybe = post()
                    if asyncio.iscoroutine(maybe):
                        await maybe
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            LOGGER.exception("connection loop failed")
        finally:
            conn.closed = True
            self.connection_set.discard(conn)
            if task is not None:
                self._client_tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            await self._on_connection_closed(conn)

    async def _on_connection_closed(self, conn: Connection) -> None:
        if self._shutting_down:
            return
        removed = [t for t in self.queue if t.conn is conn]
        if removed:
            self.queue = [t for t in self.queue if t.conn is not conn]
            for turn in removed:
                turn.state = "cancelled"
                turn.end_sent = True
                turn.done_event.set()
        if self.active is not None and self.active.conn is conn:
            await self._cancel_turn(self.active, disconnect=True)

    # --------------------------------------------------------------- dispatch
    async def _dispatch(
        self, conn: Connection, request: Request
    ) -> tuple[Any, Callable[[], Any] | None]:
        method = request.method
        if method == "aki.health":
            return self._health(), None
        if method == "aki.hello":
            return self._hello(conn, request.params), None

        if not conn.handshaken:
            raise RpcError(ErrorCode.INVALID_REQUEST, "aki.hello is required first")

        if method == "aki.status":
            return await self._status(), None
        if method == "aki.conversation.reset":
            return await self._reset_conversation(), None
        if method == "aki.username.get":
            return self._username_get(), None
        if method == "aki.username.set":
            return await self._username_set(request.params), None
        if method == "aki.identity.get":
            return self._identity_get(), None
        if method == "aki.identity.rename":
            return await self._identity_rename(request.params), None
        if method == "aki.conversation.recompile":
            return await self._recompile_conversation(), None
        if method == "aki.conversation.compact":
            return await self._compact_conversation(request.params), None
        if method == "aki.approval.respond":
            return self._approval_respond(conn, request.params), None
        if method == "aki.memory.status":
            return await self._memory_status(), None
        if method == "aki.memory.tokens":
            return await self._memory_tokens(), None
        if method == "aki.memory.tree":
            return self._memory_tree(request.params), None
        if method == "aki.memory.read":
            return self._memory_read(request.params), None
        if method == "aki.memory.search":
            return self._memory_search(request.params), None
        if method == "aki.fs.writable.list":
            return self._writable_status(), None
        if method == "aki.fs.writable.add":
            return await self._writable_add(request.params), None
        if method == "aki.fs.writable.remove":
            return await self._writable_remove(request.params), None
        if method == "aki.fs.writable.clear":
            return await self._writable_clear(), None
        if method == "aki.mqtt.status":
            return self._mqtt_status(), None
        if method == "aki.mqtt.set":
            return await self._mqtt_set(request.params), None
        if method == "aki.mcp.status":
            return self._mcp_native_status(), None
        if method == "aki.mcp.set":
            return await self._mcp_native_set(request.params), None
        if method == "aki.avatar.status":
            return self._avatar_health(), None
        if method == "aki.reasoning.get":
            return self._reasoning_status(), None
        if method == "aki.reasoning.set":
            return await self._reasoning_set(request.params), None
        if method == "aki.model.get":
            return self._model_status(), None
        if method == "aki.model.set":
            return await self._model_set(request.params), None
        if method == "aki.admin.zero_initiation_budget":
            return self._zero_initiation_budget(request.params), None
        raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")

    def _zero_initiation_budget(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        source = require_string(params, "source_profile").strip()
        if source != "aki":
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "only source_profile 'aki' may zero reply budgets",
            )
        if source == self.config.profile:
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "BUDGET_SELF_FORBIDDEN: Aki cannot zero her own reply budget",
            )
        previous = self._agent_initiation_remaining
        self._agent_initiation_remaining = 0
        reason = optional_string(params, "reason") or ""
        LOGGER.info(
            "reply budget zeroed by %s previous=%s target=%s reason=%s",
            source,
            previous,
            self.config.profile,
            reason[:200],
        )
        return {
            "target_profile": self.config.profile,
            "previous": previous,
            "remaining": self._agent_initiation_remaining,
            "reset_on": "next-user-message",
        }

    def _hello(self, conn: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        version = params.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise RpcError(ErrorCode.INVALID_PARAMS, "protocol_version must be an integer")
        if version != PROTOCOL_VERSION:
            raise RpcError(
                ErrorCode.PROTOCOL_VERSION,
                f"unsupported protocol_version {version}",
                {"supported": PROTOCOL_VERSION},
            )
        conn.client_name = require_string(params, "client_name")
        conn.client_version = require_string(params, "client_version")
        conn.handshaken = True
        capabilities = dict(CAPABILITIES)
        allowed = {tool["name"] for tool in tools_for_profile(self.config.profile)}
        capabilities["exclusive_tools"] = bool(allowed)
        capabilities["schedule_tools"] = {
            "aki_schedule_create",
            "aki_schedule_update",
            "aki_schedule_delete",
            "aki_schedule_list",
        }.issubset(allowed)
        capabilities["life_ledger"] = "aki_ledger_add" in allowed
        capabilities["proactive_triggers"] = self.config.profile == "aki"
        return {
            "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "agent_id": self.config.agent_id,
            "conversation_id": self.config.conversation_id,
            "capabilities": capabilities,
        }

    def _backend_health(self) -> dict[str, Any]:
        return {
            "state": getattr(self.backend, "state", "unknown"),
            "ready": bool(getattr(self.backend, "ready", False)),
            "pid": getattr(self.backend, "pid", None),
            "restart_count": getattr(self.backend, "restart_count", 0),
            "last_error": getattr(self.backend, "last_error", None),
            "last_ready_at": getattr(self.backend, "last_ready_at", None),
            "letta_bin": self.config.letta_bin,
            "aki_bridge": getattr(self.backend, "aki_bridge", {}),
            "aki_bridge_ready": bool(
                getattr(self.backend, "supports_aki_bridge", False)
            ),
        }

    def _channel_health(self) -> dict[str, Any]:
        if self.channel is None:
            return {
                "enabled": False,
                "state": "disabled",
                "connected": False,
                "host": self.config.mqtt_host,
                "port": self.config.mqtt_port,
                "topic": self.config.mqtt_topic,
                "wait_for": list(self.config.channel_wait_for),
                "transcript": dict(self._channel_transcript_status),
                "public_memory": self.public_memory.status(),
            }
        health = self.channel.health()
        health["wait_for"] = list(self.config.channel_wait_for)
        health["transcript"] = dict(self._channel_transcript_status)
        health["public_memory"] = self.public_memory.status()
        return health

    def _sandbox_health(self) -> dict[str, Any]:
        return self._writable_status()

    def _load_mcp_native_state(self) -> None:
        path = self.mcp_native_state_path
        try:
            excluded = read_mcp_excluded(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("invalid MCP native state file: %s (%s)", path, exc)
            return
        if excluded is None:
            return
        self._mcp_native_excluded = set(excluded)

    def _save_mcp_native_state(self) -> None:
        write_mcp_excluded(
            self.mcp_native_state_path, sorted(self._mcp_native_excluded)
        )

    def _prepare_letta_provider(self) -> None:
        """Prepare Letta provider metadata without owning the proxy process."""
        aki = self.config.aki
        if aki is None:
            return
        letta_home = self.config.letta_home or self.writable.letta_home
        self.config.aki = prepare_letta(
            aki,
            backend_dir=self.config.backend_dir,
            letta_home=letta_home,
        )

    def _mcp_native_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.mcp_native_tools_enabled,
            "allowed_servers": list(self.config.mcp_native_servers),
            "excluded_servers": sorted(self._mcp_native_excluded),
            "active_servers": list(self._mcp_native_active_servers),
            "tool_count": len(self._mcp_tools),
            "tools": [tool["name"] for tool in self._mcp_tools],
            "registered": self._external_tools_registered,
            "pending": self._external_tools_pending,
            "error": self._mcp_tools_error,
            "state_path": str(self.mcp_native_state_path),
        }

    def _external_tools_health(self) -> dict[str, Any]:
        tools = [tool["name"] for tool in self._external_tools()]
        return {
            "registered": self._external_tools_registered,
            "error": self._external_tools_error,
            "tools": tools,
            "exclusive_tools": [
                tool["name"] for tool in tools_for_profile(self.config.profile)
            ],
            "mcp_native_enabled": self.config.mcp_native_tools_enabled,
            "mcp_native_tool_count": len(self._mcp_tools),
            "mcp_native_error": self._mcp_tools_error,
            "mcp_native_excluded": sorted(self._mcp_native_excluded),
            "mcp_native_servers": list(self._mcp_native_active_servers),
            "require_approval": {
                TOKIFACTOR_IMAGE_TOOL_NAME: self.config.tokifactor_image_require_approval,
                CAMERA_TOOL_NAME: self.config.camera_require_approval,
                SCREENSHOT_TOOL_NAME: self.config.screenshot_require_approval,
                IMAGE_GENERATION_TOOL_NAME: bool(
                    self.config.image_generation
                    and self.config.image_generation.require_approval
                ),
            },
            "camera": self._camera_health(),
            "screenshot": self._screenshot_health(),
            "image_generation": self._image_generation_health(),
        }

    def _image_generation_health(self) -> dict[str, Any]:
        image = self.config.image_generation
        error: str | None = None
        if image is None:
            error = "image generation is not configured"
        elif not image.enabled:
            error = "image generation tool is disabled"
        elif not image.api_key:
            error = "image generation API key is empty"
        return {
            "enabled": bool(image and image.enabled),
            "available": bool(image and image.available),
            "provider": "minimax" if image is not None else None,
            "model": image.model if image is not None else None,
            "endpoint": image.path if image is not None else None,
            "output_dir": image.output_dir if image is not None else None,
            "require_approval": bool(image and image.require_approval),
            "error": error,
        }

    def _tool_ownership_status(self) -> dict[str, Any]:
        allowed = sorted(
            tool["name"] for tool in tools_for_profile(self.config.profile)
        )
        return {
            "profile": self.config.profile,
            "allowed": allowed,
            "blocked": sorted(EXCLUSIVE_TOOL_NAMES - set(allowed)),
            "blocked_count": len(EXCLUSIVE_TOOL_NAMES - set(allowed)),
        }

    def _tool_policy_status(self) -> dict[str, Any]:
        policy = self.config.tool_policy
        return {
            "profile": self.config.profile,
            "explicit": policy.explicit,
            "toolset": policy.toolset,
            "allow": list(policy.allow),
            "deny": list(policy.deny),
            "write_roots": list(policy.write_roots),
        }

    def _camera_health(self) -> dict[str, Any]:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        device = Path(self.config.camera_device).expanduser()
        error: str | None = None
        if not self.config.camera_enabled:
            error = "camera tool is disabled"
        elif not ffmpeg:
            error = f"ffmpeg is unavailable: {self.config.ffmpeg_bin}"
        else:
            try:
                resolved = device.resolve(strict=True)
            except OSError:
                error = f"camera device is unavailable: {device}"
            else:
                if not _is_video_device(resolved):
                    error = "camera device is not a V4L2 /dev/videoN character device"
                else:
                    device = resolved
        return {
            "enabled": self.config.camera_enabled,
            "available": error is None,
            "device": str(device),
            "ffmpeg": ffmpeg,
            "ffprobe": shutil.which(self.config.ffprobe_bin) if self.config.ffprobe_bin else None,
            "output_dir": str(
                self.config.camera_output_dir or default_camera_dir()
            ),
            "require_approval": self.config.camera_require_approval,
            "error": error,
        }

    def _wayland_display_candidates(self) -> list[str]:
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        ).expanduser()
        candidates: list[str] = []
        configured = self.config.screenshot_wayland_display or os.environ.get(
            "WAYLAND_DISPLAY"
        )
        if configured:
            candidates.append(configured)
        try:
            sockets = [
                path.name
                for path in runtime.glob("wayland-*")
                if path.is_socket()
            ]
        except OSError:
            sockets = []
        def socket_mtime(name: str) -> float:
            try:
                return (runtime / name).stat().st_mtime
            except OSError:
                return 0.0

        sockets.sort(key=socket_mtime, reverse=True)
        for name in sockets:
            if name not in candidates:
                candidates.append(name)
        return candidates

    def _desktop_is_kde(self) -> bool:
        now = time.monotonic()
        if now - self._kde_detected_at < 30.0:
            return self._kde_detected
        desktop = " ".join(
            value
            for value in (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_DESKTOP", ""),
                os.environ.get("DESKTOP_SESSION", ""),
            )
            if value
        ).lower()
        detected = "kde" in desktop or "plasma" in desktop or os.environ.get(
            "KDE_FULL_SESSION", ""
        ).lower() in ("1", "true")
        if not detected and not desktop:
            qdbus = self.config.qdbus_bin or shutil.which("qdbus") or shutil.which(
                "qdbus6"
            )
            if qdbus:
                try:
                    result = subprocess.run(
                        [qdbus, "org.kde.KWin", "/Scripting"],
                        env=desktop_environment(),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                    )
                    detected = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    detected = False
        self._kde_detected_at = now
        self._kde_detected = detected
        return detected

    def _screenshot_backends(self) -> list[tuple[str, str]]:
        if self._desktop_is_kde():
            spectacle = shutil.which(self.config.spectacle_bin)
            return [("spectacle", spectacle)] if spectacle else []
        grim = shutil.which(self.config.grim_bin)
        return [("grim", grim)] if grim else []

    def _screenshot_health(self) -> dict[str, Any]:
        grim = shutil.which(self.config.grim_bin)
        spectacle = shutil.which(self.config.spectacle_bin)
        backends = self._screenshot_backends()
        candidates = self._wayland_display_candidates()
        error: str | None = None
        if not self.config.screenshot_enabled:
            error = "screenshot tool is disabled"
        elif not backends:
            error = "no supported screenshot backend found (spectacle/grim)"
        elif not candidates:
            error = "no Wayland display socket found"
        return {
            "enabled": self.config.screenshot_enabled,
            "available": error is None,
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP")
            or os.environ.get("XDG_SESSION_DESKTOP"),
            "backend": backends[0][0] if backends else None,
            "backends": [name for name, _ in backends],
            "spectacle": spectacle,
            "grim": grim,
            "wayland_display": candidates[0] if candidates else None,
            "candidates": candidates,
            "output_dir": str(
                self.config.screenshot_output_dir or default_screenshot_dir()
            ),
            "require_approval": self.config.screenshot_require_approval,
            "error": error,
        }

    def _avatar_health(self) -> dict[str, Any]:
        if self.avatar is not None:
            status = self.avatar.status()
        else:
            status = {
                "configured": self.config.avatar_path is not None,
                "loaded": False,
                "path": str(self.avatar_path) if self.avatar_path else None,
                "error": self.avatar_error,
            }
        if self.channel is not None:
            channel_health = self.channel.health()
            status["channel"] = channel_health.get("avatar")
        return status

    def _writable_status(self) -> dict[str, Any]:
        if not self.config.sandbox_enabled:
            return {
                "enabled": False,
                "available": True,
                "backend": "none",
                "blocked_reason": None,
                "revision": self.writable.revision,
                "defaults": [str(path) for path in self.writable.defaults],
                "internal": [str(path) for path in self.writable.internal],
                "registered": [
                    {"path": entry.path, "added_at": entry.added_at}
                    for entry in self.writable._roots
                ],
                "effective": [str(path) for path in self.writable.effective_roots()],
                "invalid": list(self.writable._invalid),
                "state_path": str(self.writable.state_path),
            }
        status = self.writable.status()
        status["blocked_reason"] = self._sandbox_blocked_reason
        return status

    def _health(self) -> dict[str, Any]:
        backend = self._backend_health()
        channel = self._channel_health()
        sandbox = self._sandbox_health()
        channel_ready = not channel.get("enabled", False) or bool(channel.get("connected", False))
        sandbox_ready = not sandbox.get("enabled", False) or bool(sandbox.get("available", False))
        bridge_ready = self._aki_bridge_available()
        ready = bool(backend["ready"]) and bridge_ready and channel_ready and sandbox_ready
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "backend_ready": bool(backend["ready"]),
            "bridge_ready": bridge_ready,
            "channel_ready": channel_ready,
            "sandbox_ready": sandbox_ready,
            "version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started_at, 3),
            "socket": str(self.config.socket_path),
            "agent_id": self.config.agent_id,
            "profile": self.config.profile,
            "conversation_id": self.config.conversation_id,
            "channel_identity": {
                "id": self._channel_self_key(),
                "channel_id": self.config.channel_id,
                "name": self.config.channel_name,
                "aliases": list(self.config.channel_aliases),
                "description": self.config.channel_description,
            },
            "proxy": {
                "listen": f"{self.config.aki.proxy_host}:{self.config.aki.proxy_port}"
                if self.config.aki is not None
                else None,
                "owned_by_akid": False,
            },
            "agent_initiation": {
                "remaining": self._agent_initiation_remaining,
                "limit": self.config.agent_initiation_limit,
            },
            "execution_lock": self.execution_lock.status(),
            "backend": backend,
            "channel": channel,
            "solo": self._solo_status(),
            "sandbox": sandbox,
            "external_tools": self._external_tools_health(),
            "tool_ownership": self._tool_ownership_status(),
            "tool_policy": self._tool_policy_status(),
            "proactive": self._proactive_status(),
            "live_context": self._live_context_health(),
            "layer": self._layer_health(),
            "avatar": self._avatar_health(),
            "connections": len(self.connection_set),
            "queue_depth": len(self.queue),
        }

    def _live_context_health(self) -> dict[str, Any]:
        status = dict(self._live_context_status)
        status["enabled"] = bool(self.config.live_context_enabled)
        status["budget_tokens"] = self.config.live_context_budget_tokens
        return status

    def _agent_record_path(self) -> Path | None:
        folder = self.config.backend_dir / "agents"
        try:
            paths = sorted(folder.glob("*.json"))
        except OSError:
            return None
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id") == self.config.agent_id:
                return path
        return None

    def _write_agent_record(
        self, path: Path, record: Mapping[str, Any], system: str
    ) -> None:
        payload = dict(record)
        payload["system"] = system
        try:
            previous_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            previous_mtime_ns = 0
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            now_ns = time.time_ns()
            os.utime(
                path,
                ns=(now_ns, max(now_ns, previous_mtime_ns + 1_000_000)),
            )
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _clear_live_context_block(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                return
            system = record.get("system")
            if not isinstance(system, str) or not system:
                return
            cleared = clear_live_context(system)
            if cleared == system:
                return
            self._write_agent_record(path, record, cleared)
            self._live_context_status["updated"] = False
            self._live_context_status["agent_path"] = str(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear live context block: %s", exc)

    def _record_channel_message(
        self,
        *,
        sender: str,
        sender_id: str,
        text: str,
        from_agent: bool,
        message_id: str = "",
        sent_at: int = 0,
        phase: str = "final",
        arrived_while_busy: bool = False,
        attachments: tuple[ChannelImageAttachment, ...] = (),
    ) -> ChannelTranscriptEntry:
        self._channel_history_seq += 1
        resolved_id = self._history_member_id(sender, sender_id)
        entry = ChannelTranscriptEntry(
            seq=self._channel_history_seq,
            sender=sender or sender_id or "unknown",
            sender_id=resolved_id or sender_id.strip(),
            text=text,
            from_agent=from_agent,
            timestamp=now_iso(),
            sent_at=sent_at if sent_at > 0 else int(time.time() * 1000),
            message_id=message_id,
            phase=phase,
            arrived_while_busy=arrived_while_busy,
            attachments=tuple(attachments),
        )
        self._channel_history.append(entry)
        if len(self._channel_history) > CHANNEL_TRANSCRIPT_MAX_RECORDS:
            del self._channel_history[:-CHANNEL_TRANSCRIPT_MAX_RECORDS]
        self._persist_channel_history()
        if self._solo_session is None:
            try:
                self.public_memory.record(entry)
            except Exception as exc:
                LOGGER.warning("public memory record failed: %s", exc)
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
            }
        )
        return entry

    def _channel_history_key(self) -> str:
        return channel_history_key(
            host=self.config.mqtt_host,
            port=self.config.mqtt_port,
            topic=self.config.mqtt_topic,
            magic=self.config.mqtt_magic,
        )

    def _channel_history_path(self, *, solo: bool | None = None) -> Path:
        use_solo = (
            self._solo_session is not None if solo is None else bool(solo)
        )
        return channel_history_path(
            self.channel_history_dir,
            key=self._channel_history_key(),
            profile=self.config.profile,
            solo=use_solo,
        )

    def _channel_media_root(self) -> Path:
        return (
            self.channel_media_dir
            / self._channel_history_key()
            / (self.config.profile or "aki")
        )

    @staticmethod
    def _safe_media_component(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._")
        return cleaned[:96] or "message"

    def _store_channel_image(
        self,
        image: ImageEnvelope,
        *,
        name: str,
        index: int,
    ) -> ChannelImageAttachment | None:
        mime = image.mime or detect_image_mime(image.data)
        if mime is None:
            return None
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(mime)
        if extension is None:
            return None
        root = self._channel_media_root()
        directory = root / self._safe_media_component(image.message_id)
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("channel media path is not a directory")
            resolved_root = root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
            if resolved_root not in resolved_directory.parents:
                raise ValueError("channel media path escaped its root")
            path = resolved_directory / f"{max(1, index)}{extension}"
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(image.data)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            return ChannelImageAttachment(
                name=name[:MAX_ATTACHMENT_NAME_CHARS] or f"图片{max(1, index)}",
                path=str(path),
                mime=mime,
                size=len(image.data),
                index=max(1, index),
            )
        except OSError as exc:
            LOGGER.warning("failed to store channel image: %s", exc)
            return None

    def _cleanup_channel_media(self) -> None:
        root = self._channel_media_root()
        keep = {
            attachment.path
            for entry in self._channel_history
            for attachment in entry.attachments
            if attachment.path and not attachment.missing
        }
        if not root.is_dir() or root.is_symlink():
            return
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if path.is_file() and str(path) not in keep:
                    with contextlib.suppress(OSError):
                        path.unlink()
            with contextlib.suppress(OSError):
                directory.rmdir()

    @staticmethod
    def _channel_history_payload(entry: ChannelTranscriptEntry) -> dict[str, Any]:
        return {
            "seq": entry.seq,
            "sender": entry.sender,
            "sender_id": entry.sender_id,
            "text": entry.text,
            "from_agent": entry.from_agent,
            "timestamp": entry.timestamp,
            "sent_at": entry.sent_at,
            "message_id": entry.message_id,
            "phase": entry.phase,
            "arrived_while_busy": entry.arrived_while_busy,
            "attachments": [
                {
                    "name": attachment.name,
                    "path": attachment.path,
                    "mime": attachment.mime,
                    "size": attachment.size,
                    "index": attachment.index,
                    "missing": attachment.missing,
                }
                for attachment in entry.attachments
            ],
        }

    @staticmethod
    def _load_channel_attachments(value: Any) -> tuple[ChannelImageAttachment, ...]:
        if not isinstance(value, list):
            return ()
        attachments: list[ChannelImageAttachment] = []
        for item in value[:MAX_CHANNEL_ATTACHMENTS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:MAX_ATTACHMENT_NAME_CHARS]
            path = str(item.get("path") or "").strip()
            if not name:
                continue
            attachments.append(
                ChannelImageAttachment(
                    name=name,
                    path=path,
                    mime=str(item.get("mime") or ""),
                    size=max(0, int(item.get("size") or 0)),
                    index=max(1, int(item.get("index") or len(attachments) + 1)),
                    missing=bool(item.get("missing")) or not path,
                )
            )
        return tuple(attachments)

    def _load_channel_history(self, path: Path | None = None) -> None:
        path = path or self._channel_history_path()
        self.channel_history_path = path
        by_seq: dict[int, ChannelTranscriptEntry] = {}
        load_error: str | None = None
        history_changed = False
        if path.is_file():
            try:
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    try:
                        seq = int(payload.get("seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    if seq <= 0:
                        continue
                    raw_sender = str(payload.get("sender") or "")
                    raw_sender_id = str(payload.get("sender_id") or "")
                    normalized_id = self._history_member_id(raw_sender, raw_sender_id)
                    if normalized_id and normalized_id != raw_sender_id:
                        history_changed = True
                    by_seq[seq] = ChannelTranscriptEntry(
                        seq=seq,
                        sender=raw_sender,
                        sender_id=normalized_id or raw_sender_id,
                        text=str(payload.get("text") or "")[:MAX_CHANNEL_TEXT_CHARS],
                        from_agent=bool(payload.get("from_agent")),
                        timestamp=str(payload.get("timestamp") or ""),
                        sent_at=int(payload.get("sent_at") or 0),
                        message_id=str(payload.get("message_id") or ""),
                        phase=str(payload.get("phase") or "final"),
                        arrived_while_busy=bool(payload.get("arrived_while_busy")),
                        attachments=self._load_channel_attachments(payload.get("attachments")),
                    )
            except OSError as exc:
                load_error = str(exc)
                LOGGER.warning("failed to load channel history: %s", exc)
        ordered = [by_seq[seq] for seq in sorted(by_seq)]
        self._channel_history = ordered[-CHANNEL_TRANSCRIPT_MAX_RECORDS:]
        self._channel_history_seq = max(by_seq, default=0)
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "channel_key": self._channel_history_key(),
                "history_path": str(path),
                "persisted": path.is_file(),
            }
        )
        if load_error is None:
            self._channel_transcript_status.pop("last_history_error", None)
        else:
            self._channel_transcript_status["last_history_error"] = load_error
        if len(ordered) > CHANNEL_TRANSCRIPT_MAX_RECORDS:
            self._persist_channel_history()
        elif history_changed:
            self._persist_channel_history()
        for member_id, member in self._channel_members.items():
            if normalize_member_kind(member.get("kind")) == MEMBER_KIND_USER:
                self._normalize_channel_history_kind(member_id, MEMBER_KIND_USER)

    def _persist_channel_history(self) -> None:
        if self._solo_session is None and not self._channel_history:
            try:
                if (
                    self.channel_history_path.is_file()
                    and self.channel_history_path.stat().st_size > 1
                ):
                    LOGGER.error(
                        "refusing to replace non-empty group history with an empty snapshot: %s",
                        self.channel_history_path,
                    )
                    return
            except OSError:
                pass
        body = "".join(
            json.dumps(
                self._channel_history_payload(entry),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for entry in self._channel_history
        )
        try:
            atomic_write(self.channel_history_path, body)
        except (OSError, TypeError, ValueError) as exc:
            self._channel_transcript_status["last_history_error"] = str(exc)
            LOGGER.warning("failed to persist channel history: %s", exc)
            return
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "channel_key": self._channel_history_key(),
                "history_path": str(self.channel_history_path),
                "persisted": True,
            }
        )
        self._channel_transcript_status.pop("last_history_error", None)
        self._cleanup_channel_media()

    def _clear_channel_transcript_block(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                return
            system = record.get("system")
            if not isinstance(system, str) or not system:
                return
            cleared = clear_channel_transcript(system)
            if cleared == system:
                return
            self._write_agent_record(path, record, cleared)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear channel transcript block: %s", exc)

    def _clear_persisted_turn_blocks(self) -> None:
        """Remove legacy per-turn blocks once the bridge owns turn context."""
        path = self._agent_record_path()
        if path is None:
            return

        def clear(system: str) -> str:
            return clear_live_context(
                clear_solo_memories(
                    clear_solo_transcript(
                        clear_solo_mode(
                            clear_channel_turn(clear_channel_transcript(system))
                        )
                    )
                )
            )

        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict):
                system = record.get("system")
                if isinstance(system, str) and system:
                    updated = clear(system)
                    if updated != system:
                        self._write_agent_record(path, record, updated)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear persisted turn blocks: %s", exc)

    def _public_memory_prompt_block(self) -> str:
        records = self.public_memory.snapshot()
        if not records:
            return ""
        return render_shared_memory(
            records,
            path=str(self.public_memory.path),
            updated_at=str(records[-1].get("recorded_at") or ""),
        )

    def _backfill_public_memory(self) -> int:
        """Seed the shared store once from Aki's canonical group history."""
        if self.config.profile != "aki":
            return 0
        if self.public_memory.status()["records"] > 0:
            return 0
        entries: list[ChannelTranscriptEntry] = []
        for row in read_channel_history(self.group_channel_history_path):
            if str(row.get("phase") or "final") != "final":
                continue
            text = str(row.get("text") or "").strip()
            if not text or "[NO_REPLY]" in text:
                continue
            entries.append(
                ChannelTranscriptEntry(
                    seq=int(row.get("seq") or 0),
                    sender=str(row.get("sender") or ""),
                    sender_id=str(row.get("sender_id") or ""),
                    text=text,
                    from_agent=bool(row.get("from_agent")),
                    timestamp=str(row.get("timestamp") or ""),
                    sent_at=int(row.get("sent_at") or 0),
                    message_id=str(row.get("message_id") or ""),
                    phase="final",
                )
            )
        if not entries:
            return 0
        added = self.public_memory.record_many(entries)
        if added:
            LOGGER.info(
                "public memory backfilled from canonical group history records=%s",
                added,
            )
        return added
        prompt_path = self._conversation_dir() / "system-prompt.json"
        if not prompt_path.is_file():
            return
        try:
            prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
            if not isinstance(prompt, dict):
                return
            content = prompt.get("content")
            if not isinstance(content, str) or not content:
                return
            updated = clear(content)
            if updated != content:
                prompt["content"] = updated
                atomic_write(
                    prompt_path,
                    json.dumps(prompt, ensure_ascii=False, indent=2) + "\n",
                )
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "failed to clear persisted turn blocks from conversation prompt: %s",
                exc,
            )

    def _clear_channel_identity_block(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                return
            system = record.get("system")
            if not isinstance(system, str) or not system:
                return
            cleared = clear_channel_identity(system)
            if cleared == system:
                return
            self._write_agent_record(path, record, cleared)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear channel identity block: %s", exc)

    async def _refresh_channel_identity(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        block = render_channel_identity(self._channel_self_member())
        async with self._channel_transcript_lock:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("agent record is not a JSON object")
                system = record.get("system")
                if not isinstance(system, str) or not system:
                    raise ValueError("agent system prompt is not a string")
                updated = apply_channel_identity(system, block)
                if updated != system:
                    self._write_agent_record(path, record, updated)
            except (OSError, ValueError) as exc:
                LOGGER.warning("failed to refresh channel identity: %s", exc)

    def _update_system_prompt_text(
        self,
        update: Callable[[str], str],
        *,
        label: str,
    ) -> None:
        path = self._agent_record_path()
        targets: list[tuple[Path, dict[str, Any], str]] = []
        if path is not None:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("agent record is not a JSON object")
                system = record.get("system")
                if not isinstance(system, str) or not system:
                    raise ValueError("agent system prompt is not a string")
                targets.append((path, record, system))
            except (OSError, ValueError) as exc:
                LOGGER.warning("failed to update %s in agent record: %s", label, exc)

        prompt_path = self._conversation_dir() / "system-prompt.json"
        if prompt_path.is_file():
            try:
                record = json.loads(prompt_path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("conversation system prompt is not an object")
                content = record.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("conversation system prompt has no content")
                targets.append((prompt_path, record, content))
            except (OSError, ValueError) as exc:
                LOGGER.warning(
                    "failed to update %s in conversation prompt: %s", label, exc
                )

        for target, record, current in targets:
            updated = update(current)
            if updated == current:
                continue
            try:
                if target == path:
                    self._write_agent_record(target, record, updated)
                else:
                    record["content"] = updated
                    atomic_write(
                        target,
                        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    )
            except (OSError, ValueError) as exc:
                LOGGER.warning("failed to write %s into %s: %s", label, target, exc)

    def _emoji_entries(self) -> list[EmojiEntry]:
        root = self.config.emoji_dir or (self.config.cwd / "assets" / "emoji")
        return load_emoji_entries(root, self.config.profile)

    async def _refresh_channel_policy(self) -> None:
        block = render_channel_policy(
            user_name=self.config.username or "Master",
            emoji_entries=self._emoji_entries(),
            wait_user_available=self.channel is not None,
        )
        async with self._wake_routing_lock:
            self._update_system_prompt_text(
                lambda system: apply_channel_policy(system, block),
                label="channel policy",
            )

    async def _refresh_channel_turn(self, turn: Turn | None) -> str:
        if turn is None or turn.origin != "mqtt":
            return ""
        solo = self._solo_session is not None
        wait_labels = (
            () if solo else self._wait_labels_for_participants(turn.channel_participants)
        )
        block = render_channel_turn(
            self_member=self._channel_self_member(),
            members=self._channel_member_views(),
            message=turn.channel_text,
            message_seq=turn.channel_message_seq,
            message_sender=turn.channel_sender,
            message_sender_id=turn.origin_sender_id,
            initiation_remaining=self._agent_initiation_remaining,
            initiation_limit=self.config.agent_initiation_limit,
            from_agent=turn.origin_is_agent,
            user_name=self.config.username,
            wait_labels=wait_labels,
            message_attachments=turn.channel_attachments,
            solo=solo,
        )
        return block

    async def _refresh_channel_wake_routing(self) -> None:
        """Keep the wake-routing rules in the real system prompt."""
        block = render_channel_wake_routing(self.config.username or "Master")
        async with self._wake_routing_lock:
            path = self._agent_record_path()
            if path is not None:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError("agent record is not a JSON object")
                    system = record.get("system")
                    if not isinstance(system, str) or not system:
                        raise ValueError("agent system prompt is not a string")
                    updated = apply_channel_wake_routing(system, block)
                    if updated != system:
                        self._write_agent_record(path, record, updated)
                except (OSError, ValueError) as exc:
                    LOGGER.warning("failed to refresh channel wake routing: %s", exc)

            prompt_path = self._conversation_dir() / "system-prompt.json"
            if prompt_path.is_file():
                try:
                    record = json.loads(prompt_path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError("conversation system prompt is not an object")
                    content = record.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("conversation system prompt has no content")
                    updated_content = apply_channel_wake_routing(content, block)
                    if updated_content != content:
                        record["content"] = updated_content
                        atomic_write(
                            prompt_path,
                            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                        )
                except (OSError, ValueError) as exc:
                    LOGGER.warning(
                        "failed to write channel wake routing into system prompt: %s",
                        exc,
                    )

    async def _refresh_tool_ownership(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        online = set(discover_profiles())
        online.add(self.config.profile)
        block = render_tool_ownership_prompt(
            self.config.profile,
            online_profiles=online,
        )
        async with self._tool_ownership_lock:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("agent record is not a JSON object")
                system = record.get("system")
                if not isinstance(system, str) or not system:
                    raise ValueError("agent system prompt is not a string")
                updated = apply_tool_ownership(system, block)
                if updated != system:
                    self._write_agent_record(path, record, updated)
            except (OSError, ValueError) as exc:
                LOGGER.warning("failed to refresh tool ownership: %s", exc)

    def _clear_tool_ownership_block(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                return
            system = record.get("system")
            if not isinstance(system, str) or not system:
                return
            cleared = clear_tool_ownership(system)
            if cleared != system:
                self._write_agent_record(path, record, cleared)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear tool ownership: %s", exc)

    async def _refresh_channel_transcript(
        self,
        *,
        new_seq: int | None = None,
        limit: int | None = None,
    ) -> str:
        path = self._agent_record_path()

        def with_attachment_state(
            entries: list[ChannelTranscriptEntry],
        ) -> list[ChannelTranscriptEntry]:
            return [
                replace(
                    entry,
                    attachments=tuple(
                        replace(
                            attachment,
                            missing=attachment.missing
                            or not attachment.path
                            or not Path(attachment.path).is_file(),
                        )
                        for attachment in entry.attachments
                    ),
                )
                if entry.attachments
                else entry
                for entry in entries
            ]

        members = self._channel_member_views()
        solo_mode_block = ""
        if self._solo_session is not None:
            group_entries = with_attachment_state(
                list(self._frozen_group_history)
                if limit is None
                else self._frozen_group_history[: max(0, limit)]
            )
            group_block = render_channel_transcript(
                group_entries,
                new_seq=new_seq,
                budget_tokens=self.config.channel_transcript_budget_tokens,
                members=members,
                self_id=self._channel_self_key(),
                frozen=True,
            )
            solo_entries = with_attachment_state(
                list(self._channel_history)
                if limit is None
                else self._channel_history[: max(0, limit)]
            )
            solo_block = render_solo_transcript(
                solo_entries,
                budget_tokens=self.config.aki.solo_transcript_budget_tokens
                if self.config.aki is not None
                else 64 * 1024,
                members=members,
                self_id=self._channel_self_key(),
            )
            scene = self._solo_scene or load_solo_scene(
                self.config.profile,
                username=self.config.username,
                explicit=(
                    self.config.aki.solo_prompt_file
                    if self.config.aki is not None
                    else ""
                ),
            )
            self._solo_scene = scene
            solo_mode_block = render_solo_mode(
                member_name=self.config.channel_name,
                user_name=self.config.username,
                session_id=self._solo_session.session_id,
                scene=scene,
            )
            rendered_entries = len(group_entries)
            solo_entries_count = len(solo_entries)
        else:
            group_block = render_channel_transcript(
                with_attachment_state(
                    list(self._channel_history)
                    if limit is None
                    else self._channel_history[: max(0, limit)]
                ),
                new_seq=new_seq,
                budget_tokens=self.config.channel_transcript_budget_tokens,
                members=members,
                self_id=self._channel_self_key(),
            )
            solo_block = ""
            rendered_entries = len(self._channel_history)
            solo_entries_count = 0

        memory_config = self._solo_memory_llm_config()
        if memory_config is not None:
            solo_memories_block = await self.solo_memory.render_for_prompt(
                current_session_id=(
                    self._solo_session.session_id
                    if self._solo_session is not None
                    else ""
                ),
                **memory_config,
            )
        else:
            solo_memories_block = render_solo_memories(
                [
                    session
                    for session in self.solo_memory.sessions
                    if self._solo_session is None
                    or session.session_id != self._solo_session.session_id
                ],
                budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
            )
        shared_memory_block = self._public_memory_prompt_block()
        overlay_block = self._transcript_overlay_block(group_block, solo_block)
        self._channel_transcript_status.update(
            {
                "budget_tokens": self.config.channel_transcript_budget_tokens,
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "rendered_seq": new_seq,
                "rendered_entries": rendered_entries,
                "block_tokens": len(group_block.encode("utf-8"))
                if group_block
                else 0,
                "solo_mode": self._solo_session is not None,
                "solo_entries": solo_entries_count,
                "solo_block_tokens": len(solo_block.encode("utf-8"))
                if solo_block
                else 0,
                "solo_memories_entries": len(self.solo_memory.sessions),
                "solo_memories_block_tokens": len(
                    solo_memories_block.encode("utf-8")
                )
                if solo_memories_block
                else 0,
                "solo_memories_error": self.solo_memory.last_error,
                "agent_path": str(path) if path is not None else "",
            }
        )
        if path is None:
            return overlay_block

        def update_system(system: str) -> str:
            # The bridge owns per-turn transcripts as an overlay. The
            # persistent system prompt only carries rules and memory.
            updated = clear_solo_transcript(system)
            updated = clear_channel_transcript(updated)
            updated = (
                apply_solo_mode(updated, solo_mode_block)
                if solo_mode_block
                else clear_solo_mode(updated)
            )
            updated = (
                apply_solo_memories(updated, solo_memories_block)
                if solo_memories_block
                else clear_solo_memories(updated)
            )
            updated = (
                apply_shared_memory(updated, shared_memory_block)
                if shared_memory_block
                else clear_shared_memory(updated)
            )
            return updated

        async with self._channel_transcript_lock:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("agent record is not a JSON object")
                system = record.get("system")
                if not isinstance(system, str) or not system:
                    raise ValueError("agent system prompt is not a string")
                updated = update_system(system)
                if updated != system:
                    self._write_agent_record(path, record, updated)
                prompt_path = self._conversation_dir() / "system-prompt.json"
                if prompt_path.is_file():
                    try:
                        prompt_record = json.loads(
                            prompt_path.read_text(encoding="utf-8")
                        )
                        if not isinstance(prompt_record, dict):
                            raise ValueError("conversation prompt is not an object")
                        content = prompt_record.get("content")
                        if not isinstance(content, str) or not content:
                            raise ValueError("conversation prompt has no content")
                        updated_content = update_system(content)
                        if updated_content != content:
                            prompt_record["content"] = updated_content
                            atomic_write(
                                prompt_path,
                                json.dumps(prompt_record, ensure_ascii=False, indent=2)
                                + "\n",
                            )
                    except (OSError, ValueError) as exc:
                        LOGGER.warning(
                            "failed to refresh conversation channel transcript: %s",
                            exc,
                        )
            except (OSError, ValueError) as exc:
                LOGGER.warning("failed to refresh channel transcript: %s", exc)
        return overlay_block

    def _transcript_overlay_block(
        self,
        group_block: str,
        solo_block: str,
    ) -> str:
        parts: list[str] = []
        if self._solo_session is not None:
            if group_block:
                parts.append(group_block)
            if solo_block:
                parts.append(solo_block)
        elif group_block:
            parts.append(group_block)
        return "\n\n".join(parts)

    def _clear_execution_lock_policy(self) -> None:
        path = self._agent_record_path()
        if path is None:
            return
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                return
            system = record.get("system")
            if not isinstance(system, str) or not system:
                return
            cleared = clear_execution_lock_policy(system)
            if cleared != system:
                self._write_agent_record(path, record, cleared)
        except (OSError, ValueError) as exc:
            LOGGER.warning("failed to clear execution lock policy: %s", exc)

    async def _capture_live_screenshot(self) -> dict[str, Any] | None:
        output_dir = self.config.screenshot_output_dir or default_screenshot_dir()
        output = unique_capture_path(output_dir / "live", "desktop", ".png")
        return await self._execute_screenshot_capture(
            {"include_cursor": False},
            output_path=output,
            timeout=min(self.config.live_context_timeout, 2.5),
        )

    async def _refresh_live_context(self) -> str:
        if not self.config.live_context_enabled:
            return ""
        path = self._agent_record_path()
        captured_at = datetime.now().astimezone()
        options = LiveContextOptions(
            ps_bin=self.config.ps_bin,
            qdbus_bin=self.config.qdbus_bin,
            history_path=self.config.zsh_history_path,
            history_limit=self.config.live_context_history_limit,
            process_limit=self.config.live_context_process_limit,
            timeout=self.config.live_context_timeout,
            screenshot_timeout=min(self.config.live_context_timeout, 2.5),
            include_screenshot=self.config.screenshot_enabled,
        )
        try:
            snapshot = await collect_live_context(
                options,
                screenshot_capture=(
                    self._capture_live_screenshot
                    if self.config.screenshot_enabled
                    else None
                ),
                now=captured_at,
            )
            block = render_live_context(
                snapshot,
                budget_tokens=self.config.live_context_budget_tokens,
                ledger_block=(
                    render_ledger_block(self.life_ledger.records())
                    if self.config.profile == "aki" and self.config.aki is not None
                    else ""
                ),
            )
            self._live_context_status.update(
                {
                    "enabled": True,
                    "budget_tokens": self.config.live_context_budget_tokens,
                    "last_captured_at": snapshot.captured_at.isoformat(
                        timespec="seconds"
                    ),
                    "last_sections": snapshot.sections(),
                    "last_errors": list(snapshot.errors),
                    "block_bytes": len(block.encode("utf-8")),
                    "agent_path": str(path) if path is not None else "",
                    "updated": True,
                }
            )
            return block
        except Exception as exc:
            LOGGER.warning("live context refresh failed: %s", exc)
            self._live_context_status.update(
                {
                    "last_captured_at": captured_at.isoformat(timespec="seconds"),
                    "last_errors": [str(exc)],
                    "agent_path": str(path) if path is not None else "",
                    "updated": False,
                }
            )
            return ""

    def _agent_info(self) -> tuple[str | None, str | None, int | None]:
        try:
            for path in (self.config.backend_dir / "agents").glob("*.json"):
                try:
                    data = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("id") == self.config.agent_id:
                    model = data.get("model")
                    settings = data.get("model_settings") or {}
                    reasoning = settings.get("reasoning_effort")
                    if reasoning is None:
                        reasoning = (settings.get("reasoning") or {}).get("reasoning_effort")
                    context_limit = settings.get("context_window_limit")
                    return (
                        model if isinstance(model, str) else None,
                        reasoning if isinstance(reasoning, str) else None,
                        context_limit if isinstance(context_limit, int) else None,
                    )
        except OSError:
            pass
        return None, None, None

    async def _status(self) -> dict[str, Any]:
        model, reasoning, context_limit = self._agent_info()
        tokens, token_error = await self._memory_tokens_cached(allow_error=True)
        active = None
        if self.active is not None:
            active = {
                "turn_id": self.active.turn_id,
                "state": self.active.state,
                "cancelled": self.active.cancelled,
                "started_at": self.active.started_at,
                "client_name": self.active.conn.client_name,
            }
        channel = self._channel_health()
        sandbox = self._sandbox_health()
        backend_ready = bool(getattr(self.backend, "ready", False))
        channel_ready = not channel.get("enabled", False) or bool(channel.get("connected", False))
        sandbox_ready = not sandbox.get("enabled", False) or bool(sandbox.get("available", False))
        return {
            "server_version": SERVER_VERSION,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started_at, 3),
            "socket": str(self.config.socket_path),
            "agent_id": self.config.agent_id,
            "profile": self.config.profile,
            "conversation_id": self.config.conversation_id,
            "channel_identity": {
                "id": self._channel_self_key(),
                "channel_id": self.config.channel_id,
                "name": self.config.channel_name,
                "aliases": list(self.config.channel_aliases),
                "description": self.config.channel_description,
            },
            "proxy": {
                "listen": f"{self.config.aki.proxy_host}:{self.config.aki.proxy_port}"
                if self.config.aki is not None
                else None,
                "owned_by_akid": False,
            },
            "agent_initiation": {
                "remaining": self._agent_initiation_remaining,
                "limit": self.config.agent_initiation_limit,
            },
            "execution_lock": self.execution_lock.status(),
            "model": model,
            "reasoning": reasoning,
            "context_limit": context_limit,
            "last_usage": self.last_usage,
            "active_turn": active,
            "compacting": self._compacting,
            "queue_depth": len(self.queue),
            "connections": len(self.connection_set),
            "memory_tokens": tokens,
            "memory_tokens_error": token_error,
            "backend": self._backend_health(),
            "channel": channel,
            "solo": self._solo_status(),
            "sandbox": sandbox,
            "external_tools": self._external_tools_health(),
            "tool_ownership": self._tool_ownership_status(),
            "tool_policy": self._tool_policy_status(),
            "proactive": self._proactive_status(),
            "live_context": self._live_context_health(),
            "layer": self._layer_health(),
            "avatar": self._avatar_health(),
            "backend_ready": backend_ready,
            "bridge_ready": self._aki_bridge_available(),
            "channel_ready": channel_ready,
            "sandbox_ready": sandbox_ready,
            "ready": (
                backend_ready
                and self._aki_bridge_available()
                and channel_ready
                and sandbox_ready
            ),
        }

    # ------------------------------------------------------------------ turns
    def _spawn_turn(self, turn: Turn) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._run_turn(turn), name=f"turn:{turn.turn_id}")
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return task

    async def _build_user_content(
        self, turn: Turn
    ) -> str | list[dict[str, Any]]:
        if not turn.channel_attachments:
            return turn.message
        parts: list[dict[str, Any]] = [{"type": "text", "text": turn.message}]
        total = 0
        for attachment in sorted(turn.channel_attachments, key=lambda item: item.index):
            if attachment.missing or not attachment.path:
                continue
            path = Path(attachment.path)
            try:
                if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    continue
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                continue
            if not data or total + len(data) > MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES:
                continue
            mime = attachment.mime or detect_image_mime(data)
            if mime is None:
                continue
            total += len(data)
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        return parts if len(parts) > 1 else turn.message

    async def _run_turn(self, turn: Turn) -> None:
        if turn.end_sent:
            return
        turn.state = "running"
        turn.started_at = time.time()
        self._schedule_status_refresh()
        LOGGER.info("turn started id=%s queue=%s", turn.turn_id, len(self.queue))
        try:
            await self._emit(
                turn,
                "turn_started",
                {
                    "client_message_id": turn.client_message_id,
                    "include_reasoning": turn.include_reasoning,
                    "origin": turn.origin,
                    "origin_sender_id": turn.origin_sender_id,
                    "origin_is_agent": turn.origin_is_agent,
                },
            )
            if not bool(getattr(self.backend, "ready", False)):
                await self._emit(
                    turn,
                    "error",
                    {"code": "BACKEND_UNAVAILABLE", "message": "Letta backend is not ready"},
                )
                await self._finish_turn(turn, "backend_unavailable")
                return
            self._clear_execution_lock_policy()
            if not self._aki_bridge_available():
                await self._emit(
                    turn,
                    "error",
                    {
                        "code": "BRIDGE_REQUIRED",
                        "message": (
                            "Aki requires the customized Letta runtime with "
                            "aki_bridge"
                        ),
                    },
                )
                await self._finish_turn(turn, "bridge_unavailable")
                return
            overlay_parts = await self._build_turn_overlay(turn)
            await self._refresh_tool_ownership()
            live_block = await self._refresh_live_context()
            if live_block:
                overlay_parts.append(live_block)
            overlay = (
                {"turn_context": {"system_append": "\n\n".join(overlay_parts)}}
                if overlay_parts
                else {}
            )
            try:
                await self.backend.begin_aki_turn(turn.turn_id, overlay)
                turn.bridge_turn_started = True
            except Exception as exc:
                LOGGER.warning(
                    "failed to install Aki turn overlay turn=%s: %s",
                    turn.turn_id,
                    exc,
                )
            try:
                await self.backend.send_user(await self._build_user_content(turn))
            except BackendUnavailable:
                await self._emit(
                    turn,
                    "error",
                    {"code": "BACKEND_UNAVAILABLE", "message": "Letta backend is unavailable"},
                )
                await self._finish_turn(turn, "backend_unavailable")
                return
            await turn.done_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("turn failed id=%s", turn.turn_id)
            await self._finish_turn(turn, "internal_error")
        finally:
            if not turn.end_sent:
                await self._finish_turn(turn, "error")

    async def _build_turn_overlay(self, turn: Turn) -> list[str]:
        """Build the per-turn bridge overlay without mutating the transcript."""
        overlay_parts: list[str] = []
        if turn.origin == "solo_notice":
            await self._refresh_channel_identity()
            block = await self._refresh_channel_transcript()
            if block:
                overlay_parts.append(block)
            await self._snapshot_turn_conversation(turn)
        elif turn.origin in ("mqtt", "proactive"):
            await self._refresh_channel_identity()
            block = await self._refresh_channel_turn(turn)
            if block:
                overlay_parts.append(block)
            block = await self._refresh_channel_transcript(
                new_seq=turn.channel_message_seq
            )
            if block:
                overlay_parts.append(block)
            overlay_parts.append(
                render_channel_roster(
                    self_member=self._channel_self_member(),
                    members=self._channel_member_views(),
                )
            )
            if turn.origin_is_agent:
                await self._snapshot_turn_conversation(turn)
        elif turn.origin == WAIT_GUARD_ORIGIN:
            await self._refresh_channel_identity()
            block = await self._refresh_channel_transcript()
            if block:
                overlay_parts.append(block)
            overlay_parts.append(
                render_channel_roster(
                    self_member=self._channel_self_member(),
                    members=self._channel_member_views(),
                )
            )
        return overlay_parts

    async def _update_busy_turn_overlay(self, turn: Turn) -> None:
        """Refresh a running bridge turn after a during-work message."""
        if not turn.bridge_turn_started or not self._aki_bridge_available():
            return
        try:
            parts = await self._build_turn_overlay(turn)
            live_block = await self._refresh_live_context()
            if live_block:
                parts.append(live_block)
            await self.backend.update_aki_turn(
                turn.turn_id,
                {"turn_context": {"system_append": "\n\n".join(parts)}},
            )
        except Exception as exc:
            LOGGER.warning(
                "failed to refresh busy turn overlay turn=%s: %s",
                turn.turn_id,
                exc,
            )

    def _start_proactive_services(self) -> None:
        if self.config.profile != "aki" or self.config.aki is None:
            return
        if self.config.schedule_enabled and self._schedule_task is None:
            self._schedule_stop.clear()
            self._schedule_task = asyncio.create_task(
                self._schedule_loop(), name="akid-schedule"
            )
            LOGGER.info("schedule scheduler started path=%s", self.schedule_state)
        if self.config.presence_enabled:
            self.presence_monitor.start(
                self._on_presence_triggers, self._publish_layer_presence
            )
            LOGGER.info("presence monitor started path=%s", self.presence_state)

    async def _stop_proactive_services(self) -> None:
        if self.config.profile != "aki" or self.config.aki is None:
            return
        self._schedule_stop.set()
        task = self._schedule_task
        self._schedule_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.presence_monitor.stop()

    async def _schedule_loop(self) -> None:
        tick = max(1, int(self.config.schedule_tick_seconds))
        while not self._schedule_stop.is_set():
            try:
                await self._poll_schedules()
                if self._proactive_pending:
                    await self._maybe_enqueue_proactive_turn()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("schedule poll failed")
            try:
                await asyncio.wait_for(self._schedule_stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass

    async def _poll_schedules(self) -> None:
        due = await asyncio.to_thread(self.schedule_store.due)
        if not due:
            return
        now = now_utc()
        grace = max(0, int(self.config.schedule_missed_grace_seconds))
        fireable: list[ScheduleRecord] = []
        missed: list[ScheduleRecord] = []
        for record in due:
            try:
                due_at = parse_datetime(record.next_fire_at)
            except ScheduleError:
                missed.append(record)
                continue
            if (now - due_at).total_seconds() <= grace:
                fireable.append(record)
            else:
                missed.append(record)
        if missed:
            await asyncio.to_thread(
                self.schedule_store.complete_due, missed, missed=True
            )
            LOGGER.info(
                "schedules marked missed ids=%s",
                ",".join(record.id for record in missed),
            )
        if not fireable:
            return
        await asyncio.to_thread(self.schedule_store.complete_due, fireable)
        payload = {
            "schedules": [
                {
                    "id": record.id,
                    "title": record.title,
                    "due_at": record.next_fire_at,
                    "notes": record.notes[:512],
                    "repeat": record.repeat,
                }
                for record in fireable
            ]
        }
        self._queue_proactive("schedule", payload)
        await self._maybe_enqueue_proactive_turn()

    async def _on_presence_triggers(
        self, triggers: list[PresenceTrigger]
    ) -> None:
        if not triggers:
            return
        LOGGER.info(
            "presence triggers=%s",
            ",".join(trigger.kind for trigger in triggers),
        )
        payload: dict[str, Any] = {
            "triggers": [trigger.to_dict() for trigger in triggers],
        }
        if any(trigger.kind == "return" for trigger in triggers):
            payload["journal"] = self.operation_journal.snapshot()
            payload["execution_lock"] = self.execution_lock.status()
        self._queue_proactive("presence", payload)
        await self._maybe_enqueue_proactive_turn()

    def _queue_proactive(self, kind: str, payload: Mapping[str, Any]) -> None:
        existing = self._proactive_pending.get(kind)
        if existing is None:
            self._proactive_pending[kind] = dict(payload)
            return
        if kind == "schedule":
            merged = {
                "schedules": list(existing.get("schedules") or [])
                + list(payload.get("schedules") or [])
            }
            self._proactive_pending[kind] = merged
        elif kind == "presence":
            merged_triggers = list(existing.get("triggers") or []) + list(
                payload.get("triggers") or []
            )
            merged = dict(existing)
            merged.update(payload)
            merged["triggers"] = merged_triggers
            self._proactive_pending[kind] = merged
        else:
            self._proactive_pending[kind] = dict(payload)

    async def _maybe_enqueue_proactive_turn(self) -> None:
        async with self._proactive_lock:
            if not self._proactive_pending:
                return
            if self.active is not None and self.active.origin == "proactive":
                return
            if any(turn.origin == "proactive" for turn in self.queue):
                return
            if len(self.queue) >= self.config.queue_max:
                LOGGER.warning("proactive trigger deferred: queue is full")
                return
            pending = self._proactive_pending
            self._proactive_pending = {}
            message = self._build_proactive_message(pending)
            turn = Turn(
                turn_id=f"turn-{uuid.uuid4().hex}",
                conn=self.proactive_conn,
                message=message,
                include_reasoning=False,
                client_message_id=None,
                origin="proactive",
                lock_authorized=True,
                lock_authorization="exclusive-owner",
                trigger_kind="+".join(sorted(pending)),
                trigger_facts={key: dict(value) for key, value in pending.items()},
                proactive_id=f"proactive-{uuid.uuid4().hex}",
            )
            self._last_proactive_at = now_iso()
            LOGGER.info(
                "proactive turn enqueued kind=%s id=%s",
                turn.trigger_kind,
                turn.proactive_id,
            )
            if (
                self.active is None
                and not self.queue
                and not self._compacting
                and not self._external_tools_pending
                and bool(getattr(self.backend, "ready", False))
            ):
                self.active = turn
                turn.state = "running"
                self._spawn_turn(turn)
                return
            turn.state = "queued"
            self.queue.append(turn)
            self._schedule_status_refresh()

    def _enqueue_solo_notice(self, target_names: str) -> None:
        names = ", ".join(
            part.strip() for part in target_names.split(",") if part.strip()
        )
        if not names:
            return
        if self.active is not None and self.active.origin == "solo_notice":
            return
        if any(turn.origin == "solo_notice" for turn in self.queue):
            return
        user_name = self.config.username or "Master"
        message = (
            "[akid solo notice]\n"
            f"你刚才提醒的目标（{names}）正在与{user_name}独处，当前无法响应。"
            f"请自然地向{user_name}简短说明这一点；不要再 @ 该目标，也不要尝试唤醒其他成员。"
            "本轮只处理这个提示，不要调用工具。"
        )
        turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.proactive_conn,
            message=message,
            include_reasoning=False,
            client_message_id=None,
            origin="solo_notice",
            lock_authorized=False,
            lock_authorization="solo-notice",
            trigger_kind="solo_mention",
            trigger_facts={"targets": names},
        )
        if (
            self.active is None
            and not self.queue
            and not self._compacting
            and not self._external_tools_pending
            and bool(getattr(self.backend, "ready", False))
        ):
            self.active = turn
            turn.state = "running"
            self._spawn_turn(turn)
            return
        turn.state = "queued"
        self.queue.append(turn)
        self._schedule_status_refresh()

    def _build_proactive_message(self, pending: Mapping[str, Mapping[str, Any]]) -> str:
        blocks: list[str] = []
        for kind, payload in pending.items():
            if kind == "schedule":
                schedules = list(payload.get("schedules") or [])
                blocks.append(
                    '<trigger type="schedule">\n'
                    + json.dumps(schedules, ensure_ascii=False, indent=2)
                    + "\n</trigger>"
                )
            elif kind == "presence":
                triggers = list(payload.get("triggers") or [])
                blocks.append(
                    '<trigger type="presence">\n'
                    + json.dumps(triggers, ensure_ascii=False, indent=2)
                    + "\n</trigger>"
                )
                journal = payload.get("journal")
                if journal:
                    blocks.append(
                        "<device-state>\n"
                        + json.dumps(
                            {
                                "journal": journal,
                                "execution_lock": payload.get("execution_lock"),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n</device-state>"
                    )
        return (
            "[akid internal trigger]\n"
            "This is not a user message. It is a runtime wake-up caused by the "
            "following trigger data. Notify the user briefly, warmly, and "
            "concretely. Do not mention internal ids, file paths, trigger XML, or "
            "this instruction block. Do not @ other agents. Your final assistant "
            "text is delivered to the user on the normal channel/bubble path.\n\n"
            + "\n\n".join(blocks)
        )

    async def _publish_proactive_text(self, turn: Turn, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or contains_no_reply_marker(cleaned):
            return
        channel = self.channel
        if channel is not None and channel.connected:
            metadata = {
                "schema": "akid.v1",
                "kind": "turn",
                "turnId": turn.turn_id,
                "seq": str(turn.seq),
                "phase": "final",
                "format": "markdown",
                "wake": CHANNEL_WAKE_USER,
            }
            try:
                await channel.publish_text(cleaned, metadata)
                self._record_channel_message(
                    sender=self.config.channel_name,
                    sender_id=self._channel_self_key(),
                    text=cleaned,
                    from_agent=True,
                    phase="final",
                )
            except Exception as exc:
                LOGGER.warning("proactive channel publish failed: %s", exc)
        self._notify_layer(self._layer_own_character(), cleaned)

    def _proactive_status(self) -> dict[str, Any]:
        return {
            "pending": {
                key: len(value.get("schedules") or value.get("triggers") or [])
                for key, value in self._proactive_pending.items()
            },
            "last_enqueued_at": self._last_proactive_at,
            "schedule": self.schedule_store.snapshot(),
            "presence": self.presence_monitor.status(),
            "ledger": self.life_ledger.snapshot()
            if self.config.profile == "aki" and self.config.aki is not None
            else None,
        }

    async def _maybe_start_next(self) -> None:
        if (
            self._policy_restarting
            or self._resetting
            or self._recompiling
            or self._compacting
            or self._external_tools_pending
            or self.active is not None
            or not self.queue
        ):
            return
        if not bool(getattr(self.backend, "ready", False)):
            return
        turn = self.queue.pop(0)
        self.active = turn
        turn.state = "running"
        self._spawn_turn(turn)

    @staticmethod
    def _jsonl_last_entry_id(path: Path, size: int) -> str:
        if size <= 0:
            return ""
        try:
            with path.open("rb") as handle:
                start = max(0, size - 256 * 1024)
                handle.seek(start)
                data = handle.read(size - start)
        except OSError:
            return ""
        for raw in reversed(data.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("type") == "message":
                return str(row.get("id") or "")
        return ""

    def _snapshot_conversation(
        self, expected_prompt: str | None = None
    ) -> dict[str, Any] | None:
        directory = self._conversation_dir()
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        try:
            if not messages.is_file() or not conversation.is_file():
                return None
            size = messages.stat().st_size
            boundary = capture_conversation_boundary(messages)
            return {
                "directory": directory,
                "messages_size": size,
                "conversation_bytes": conversation.read_bytes(),
                "conversation_mode": conversation.stat().st_mode & 0o777,
                "last_entry_id": self._jsonl_last_entry_id(messages, size),
                "expected_prompt": expected_prompt,
                "prefix_sha256": boundary.prefix_sha256,
                "prefix_length": boundary.prefix_length,
            }
        except OSError as exc:
            LOGGER.warning("cannot snapshot channel conversation: %s", exc)
            return None

    def _aki_bridge_available(self) -> bool:
        return bool(getattr(self.backend, "supports_aki_bridge", False))

    async def _snapshot_turn_conversation(self, turn: Turn) -> None:
        """Keep a file snapshot as fallback and capture an in-process snapshot."""
        turn.conversation_snapshot = self._snapshot_conversation(turn.message)
        if not self._aki_bridge_available():
            return
        try:
            payload = await self.backend.snapshot_conversation(
                conversation_id=self.config.conversation_id,
                agent_id=self.config.agent_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "Aki bridge snapshot failed turn=%s; using file snapshot: %s",
                turn.turn_id,
                exc,
            )
            return
        snapshot_id = str(payload.get("snapshot_id") or "")
        if snapshot_id:
            turn.bridge_snapshot_id = snapshot_id

    @staticmethod
    def _render_during_work_message(
        text: str,
        *,
        sender: str,
        sender_id: str,
        seq: int | None,
        from_agent: bool,
    ) -> str:
        return render_new_message(
            text,
            sender=sender,
            sender_id=sender_id,
            source="agent" if from_agent else "user",
            phase="final",
            seq=seq,
            untrusted=True,
            during_work=True,
        )

    @staticmethod
    def _overlay_attachments_payload(
        attachments: Sequence[ChannelImageAttachment],
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": attachment.name,
                "path": attachment.path,
                **({"missing": True} if attachment.missing else {}),
            }
            for attachment in attachments
        ]

    def _ensure_turn_overlay_dir(self, turn_id: str) -> Path:
        root = self._turn_overlay_dir()
        directory = root / turn_id
        if not os.environ.get("AKI_TURN_OVERLAY_DIR", "").strip():
            try:
                root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(root.parent, 0o700)
            except OSError:
                pass
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (root, directory):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        return directory

    def _build_insertion_record(
        self, turn: Turn, item: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        text = str(item.get("text") or "")
        sender = str(item.get("sender") or turn.channel_sender or "user")
        sender_id = str(item.get("sender_id") or turn.origin_sender_id or "")
        attachments = tuple(item.get("attachments") or ())
        insertion_id = str(item.get("insertion_id") or "").strip()
        if not insertion_id:
            insertion_id = f"ins-{uuid.uuid4().hex}"
        try:
            inserted_at_ms = int(
                item.get("inserted_at_ms")
                or item.get("sent_at")
                or int(time.time() * 1000)
            )
        except (TypeError, ValueError):
            inserted_at_ms = int(time.time() * 1000)
        inserted_at = str(item.get("inserted_at") or "").strip()
        if not inserted_at:
            inserted_at = (
                datetime.fromtimestamp(inserted_at_ms / 1000, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="milliseconds")
            )
        attachment_payload = self._overlay_attachments_payload(attachments)
        content = render_user_insertion(
            text,
            insertion_id=insertion_id,
            seq=seq,
            inserted_at=inserted_at,
            inserted_at_ms=inserted_at_ms,
            attachments=attachment_payload,
        )
        item["insertion_id"] = insertion_id
        item["inserted_at"] = inserted_at
        item["inserted_at_ms"] = inserted_at_ms
        item["insertion_content"] = content
        return {
            "schema": "aki.turn-overlay.v1",
            "turn_id": turn.turn_id,
            "insertion_id": insertion_id,
            "seq": seq,
            "sender": sender,
            "sender_id": sender_id,
            "origin_is_agent": False,
            "inserted_at": inserted_at,
            "inserted_at_ms": inserted_at_ms,
            "text": text,
            "content": content,
            "sent_at": int(item.get("sent_at") or 0),
            "attachments": attachment_payload,
        }

    async def _write_turn_overlay_file(
        self, turn: Turn, item: dict[str, Any]
    ) -> Path | None:
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= 0:
            return None
        payload = self._build_insertion_record(turn, item)
        try:
            directory = await asyncio.to_thread(
                self._ensure_turn_overlay_dir, turn.turn_id
            )
            path = directory / f"{seq:08d}-{uuid.uuid4().hex}.json"
            data = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=str(directory)
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
                raise
            return path
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "failed to write turn overlay file turn=%s seq=%s: %s",
                turn.turn_id,
                seq,
                exc,
            )
            return None

    @staticmethod
    def _remove_turn_overlay_file(turn: Turn, seq: int) -> None:
        path = turn.overlay_files.pop(seq, None)
        if path is None:
            return
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    async def _push_aki_interjection(
        self,
        turn: Turn,
        text: str,
        sender: str,
        sender_id: str,
        seq: int | None,
        *,
        from_agent: bool,
    ) -> None:
        if not turn.bridge_turn_started or not self._aki_bridge_available():
            return
        content = self._render_during_work_message(
            text,
            sender=sender,
            sender_id=sender_id,
            seq=seq,
            from_agent=from_agent,
        )
        try:
            await self.backend.update_aki_turn(
                turn.turn_id,
                {"interjection": {"user_messages": [{"content": content}]}},
            )
        except Exception as exc:
            LOGGER.warning(
                "failed to deliver Aki interjection turn=%s: %s",
                turn.turn_id,
                exc,
            )

    def _restore_boundary_error(
        self, snapshot: Mapping[str, Any], data: bytes
    ) -> str:
        expected_size = int(snapshot.get("messages_size") or 0)
        if len(data) < expected_size:
            return (
                f"conversation transcript shrank from {expected_size} "
                f"to {len(data)}"
            )
        if expected_size > 0 and data[expected_size - 1 : expected_size] != b"\n":
            return "conversation transcript boundary is not a line ending"
        prefix_length = int(snapshot.get("prefix_length") or 0)
        expected_prefix = str(snapshot.get("prefix_sha256") or "")
        if prefix_length and expected_prefix:
            start = max(0, expected_size - prefix_length)
            actual = hashlib.sha256(data[start:expected_size]).hexdigest()
            if actual != expected_prefix:
                return "conversation transcript prefix changed after snapshot"
        last_entry_id = str(snapshot.get("last_entry_id") or "")
        if last_entry_id:
            actual_last = ""
            for raw in reversed(data[:expected_size].splitlines()):
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("type") == "message":
                    actual_last = str(row.get("id") or "")
                    break
            if actual_last != last_entry_id:
                return (
                    "conversation snapshot boundary entry changed "
                    f"(expected {last_entry_id!r}, got {actual_last!r})"
                )
        expected_prompt = snapshot.get("expected_prompt")
        if expected_prompt is None:
            return ""
        suffix = data[expected_size:]
        first_row: dict[str, Any] | None = None
        for raw in suffix.splitlines():
            if not raw.strip():
                continue
            try:
                candidate = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return "first line after the snapshot is not valid JSON"
            if isinstance(candidate, dict) and candidate.get("type") == "message":
                first_row = candidate
                break
            return "first line after the snapshot is not a message"
        if first_row is None:
            return "no message followed the snapshot boundary"
        message = first_row.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            return "first message after the snapshot is not the temporary user turn"
        content = message.get("content")
        texts = (
            [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if isinstance(content, list)
            else [content] if isinstance(content, str) else []
        )
        if str(expected_prompt) not in texts:
            return "first message after the snapshot does not match the temporary turn prompt"
        return ""

    async def _restore_conversation_snapshot(
        self, snapshot: Mapping[str, Any]
    ) -> None:
        directory = Path(snapshot["directory"])
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        try:
            expected_size = int(snapshot["messages_size"])
            data = messages.read_bytes()
        except (OSError, TypeError, ValueError) as exc:
            self._channel_transcript_status["last_restore_error"] = str(exc)
            LOGGER.warning("cannot read conversation before rollback: %s", exc)
            return
        boundary_error = self._restore_boundary_error(snapshot, data)
        if boundary_error:
            LOGGER.error(
                "refusing to roll back conversation snapshot: %s",
                boundary_error,
            )
            self._channel_transcript_status["last_restore_error"] = (
                boundary_error
            )
            return
        stopped = False
        try:
            await self.backend.stop()
            stopped = True
            temp = conversation.with_name(f".{conversation.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_bytes(snapshot["conversation_bytes"])
                os.chmod(temp, int(snapshot.get("conversation_mode", 0o600)))
                os.replace(temp, conversation)
            finally:
                with contextlib.suppress(OSError):
                    temp.unlink()
            with messages.open("r+b") as handle:
                if expected_size > 0:
                    handle.seek(expected_size - 1)
                    if handle.read(1) != b"\n":
                        raise ValueError("conversation transcript boundary is invalid")
                handle.truncate(expected_size)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(messages, 0o600)
            self._channel_transcript_status["last_ephemeral_restore_at"] = now_iso()
            self._channel_transcript_status["ephemeral_turns"] = (
                int(self._channel_transcript_status.get("ephemeral_turns", 0)) + 1
            )
            self._channel_transcript_status.pop("last_restore_error", None)
        except (OSError, ValueError, TypeError) as exc:
            self._channel_transcript_status["last_restore_error"] = str(exc)
            LOGGER.warning("failed to roll back channel conversation: %s", exc)
        finally:
            if stopped and not self._shutting_down:
                self.backend.start()
                ready = await self._wait_backend_ready(20.0)
                if not ready:
                    LOGGER.warning(
                        "backend did not become ready after channel rollback"
                    )

    async def _finish_turn(self, turn: Turn, reason: str, extra: Mapping[str, Any] | None = None) -> None:
        if turn.end_sent:
            return
        turn.end_sent = True
        self._release_turn_lock(turn)
        turn.finished_at = time.time()
        turn.state = "finished"
        assistant_text = "".join(turn.assistant_parts)
        guard_targets: list[tuple[str, str, str]] = []
        if (
            reason == "completed"
            and assistant_text
            and not contains_no_reply_marker(assistant_text)
        ):
            guard_targets = self._unfinished_wait_targets(turn)
        if guard_targets:
            turn.wait_guard_blocked = True
            self.channel_conn.discard_turn_output(turn.turn_id)
            self._enqueue_wait_guard_turn(turn, guard_targets)
        if assistant_text and not turn.wait_guard_blocked:
            await self._emit(
                turn,
                "assistant_message",
                {
                    "content": assistant_text,
                    "text": assistant_text,
                },
            )
            if reason == "completed":
                if turn.origin in ("proactive", "solo_notice"):
                    await self._publish_proactive_text(turn, assistant_text)
                elif turn.origin not in ("mqtt", WAIT_GUARD_ORIGIN):
                    self._notify_layer(self._layer_own_character(), assistant_text)
        data: dict[str, Any] = {
            "reason": reason,
            "cancelled": turn.cancelled,
            "stop_reason": turn.final_result.get("stop_reason"),
            "subtype": turn.final_result.get("subtype"),
            "result": turn.final_result.get("result"),
            "duration_ms": round(((turn.finished_at or time.time()) - (turn.started_at or time.time())) * 1000),
        }
        if turn.wait_guard_blocked:
            data["wait_guard"] = True
            data["wait_targets"] = [
                name or label for _member_id, name, label in guard_targets
            ]
        if extra:
            data.update(extra)
        await self._emit(turn, "turn_end", data)
        LOGGER.info(
            "turn ended id=%s reason=%s duration_ms=%s",
            turn.turn_id,
            reason,
            data["duration_ms"],
        )
        if turn.bridge_turn_started:
            with contextlib.suppress(Exception):
                await self.backend.end_aki_turn(turn.turn_id)
            turn.bridge_turn_started = False
        task = asyncio.create_task(
            self._complete_finished_turn(turn),
            name=f"channel-finalize:{turn.turn_id}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _complete_finished_turn(self, turn: Turn) -> None:
        """Complete rollback, overlay persistence and turn handoff in order."""
        try:
            if turn.bridge_snapshot_id is not None:
                await self._restore_channel_turn_bridge(turn)
            elif turn.conversation_snapshot is not None:
                await self._restore_channel_turn(turn)
        finally:
            try:
                await self._finalize_turn_overlays(turn)
            finally:
                self._queue_unhandled_busy_messages(turn)
                await self._finalize_turn_state(turn)

    def _queue_unhandled_busy_messages(self, turn: Turn) -> None:
        queued = 0
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)) and str(
                item.get("sender_id") or ""
            ) in turn.waited_agent_ids:
                continue
            arrival_count = int(item.get("tool_results_at_arrival", 0))
            handled = (
                not turn.cancelled
                and (
                    int(item.get("pending_tool_results_at_arrival", 0)) > 0
                    or turn.tool_result_events > arrival_count
                )
            )
            if handled:
                continue
            origin_is_agent = bool(item.get("origin_is_agent", False))
            text = str(item["text"])
            lock_authorized, lock_authorization = self._channel_lock_authorization(
                text, from_agent=origin_is_agent
            )
            self._enqueue_channel_turn(
                str(item["sender"]),
                str(item["sender_id"]),
                text,
                origin_is_agent=origin_is_agent,
                channel_message_seq=int(item["seq"]),
                participants=frozenset(item.get("participants") or ()),
                attachments=tuple(item.get("attachments") or ()),
                sent_at=int(item.get("sent_at") or 0),
                lock_authorized=lock_authorized,
                lock_authorization=lock_authorization,
                client_message_id=(
                    str(item.get("client_message_id") or "").strip() or None
                ),
            )
            queued += 1
        if queued:
            LOGGER.info(
                "queued deferred messages after busy turn id=%s count=%s",
                turn.turn_id,
                queued,
            )
        turn.busy_messages.clear()

    async def _restore_channel_turn(self, turn: Turn) -> None:
        try:
            if turn.conversation_snapshot is not None:
                await self._restore_conversation_snapshot(turn.conversation_snapshot)
        except Exception:
            LOGGER.exception("channel conversation rollback failed id=%s", turn.turn_id)

    async def _restore_channel_turn_bridge(self, turn: Turn) -> None:
        try:
            snapshot_id = turn.bridge_snapshot_id
            if snapshot_id:
                await self.backend.restore_conversation(snapshot_id)
                turn.bridge_snapshot_id = None
        except Exception:
            LOGGER.exception(
                "Aki bridge conversation rollback failed id=%s; using file snapshot",
                turn.turn_id,
            )
            if turn.conversation_snapshot is not None:
                await self._restore_conversation_snapshot(turn.conversation_snapshot)

    @staticmethod
    def _jsonl_row_text(row: Mapping[str, Any]) -> str:
        message = row.get("message")
        if not isinstance(message, Mapping):
            return ""
        return _content_text(message.get("content"))

    @classmethod
    def _is_human_channel_user_row(cls, row: Mapping[str, Any]) -> bool:
        message = row.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "user":
            return False
        metadata = message.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("compaction"):
            return False
        text = cls._jsonl_row_text(row)
        return (
            "[akid MQTT channel message]" in text
            and "\nmessage:\n" in text
        )

    @classmethod
    def _find_user_role_index(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        expected_prompt: str = "",
        human_only: bool = False,
    ) -> int | None:
        fallback: int | None = None
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            message = row.get("message")
            if (
                row.get("type") != "message"
                or not isinstance(message, Mapping)
                or message.get("role") != "user"
            ):
                continue
            metadata = message.get("metadata")
            if isinstance(metadata, Mapping) and metadata.get("compaction"):
                continue
            text = cls._jsonl_row_text(row)
            if expected_prompt and expected_prompt in text:
                return index
            if cls._is_human_channel_user_row(row):
                if fallback is None:
                    fallback = index
                continue
            if not human_only and fallback is None:
                fallback = index
        return fallback

    @staticmethod
    def _replace_messages_file(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    async def _merge_consumed_user_messages(
        self, turn: Turn, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        """Append provider-consumed human messages to the latest human user role."""
        if not messages:
            return
        if turn.origin not in ("mqtt", WAIT_GUARD_ORIGIN):
            raise ValueError("consumed overlay messages require a channel turn")
        path = self._conversation_dir() / "messages.jsonl"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        rows: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                rows.append({})
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(
                    f"conversation row {index + 1} is not valid JSON"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"conversation row {index + 1} is not an object")
            rows.append(value)
        human_only = turn.origin_is_agent or turn.origin == WAIT_GUARD_ORIGIN
        target_index = self._find_user_role_index(
            rows,
            expected_prompt="" if human_only else turn.message,
            human_only=human_only,
        )
        if target_index is None:
            raise ValueError("no human user role available for channel merge")
        target = rows[target_index]
        message = target.get("message")
        if not isinstance(message, dict):
            raise ValueError("target user role has no message object")
        content = message.get("content")
        if isinstance(content, str):
            parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = [
                dict(part)
                if isinstance(part, dict)
                else {"type": "text", "text": str(part)}
                for part in content
            ]
        else:
            parts = []
        blocks: list[str] = []
        for item in sorted(
            messages,
            key=lambda value: (
                int(value.get("inserted_at_ms") or value.get("sent_at") or 0),
                int(value.get("seq") or 0),
            ),
        ):
            record_item = item if isinstance(item, dict) else dict(item)
            record = self._build_insertion_record(turn, record_item)
            blocks.append(str(record["content"]))
        rendered = render_user_insertions(
            blocks, turn_id=turn.turn_id, during_work=True
        )
        if rendered:
            parts.append({"type": "text", "text": rendered})
        message["content"] = parts
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            metadata["updated_at"] = now_iso()
        lines[target_index] = (
            json.dumps(target, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            self._replace_messages_file(path, b"".join(lines))
            await self.backend.reload_conversation(
                conversation_id=self.config.conversation_id,
                agent_id=self.config.agent_id,
            )
        except Exception:
            with contextlib.suppress(Exception):
                self._replace_messages_file(path, original)
                await self.backend.reload_conversation(
                    conversation_id=self.config.conversation_id,
                    agent_id=self.config.agent_id,
                )
            raise
        turn.user_role_entry_id = str(target.get("id") or "")
        turn.merged_user_messages.extend(dict(item) for item in messages)

    def _enqueue_merged_overlay_turn(
        self, turn: Turn, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        ordered = sorted(
            messages,
            key=lambda value: (
                int(value.get("seq") or 0),
                int(value.get("sent_at") or 0),
            ),
        )
        if not ordered:
            return
        text = "\n\n".join(
            str(item.get("text") or "").strip()
            for item in ordered
            if str(item.get("text") or "").strip()
        )
        attachments: list[ChannelImageAttachment] = []
        participants: set[str] = set()
        for item in ordered:
            attachments.extend(item.get("attachments") or ())
            participants.update(item.get("participants") or ())
        last = ordered[-1]
        client_message_id = ""
        for item in reversed(ordered):
            client_message_id = str(item.get("client_message_id") or "").strip()
            if client_message_id:
                break
        lock_authorized, lock_authorization = self._channel_lock_authorization(
            text, from_agent=False
        )
        self._enqueue_channel_turn(
            str(last.get("sender") or turn.channel_sender or "user"),
            str(last.get("sender_id") or turn.origin_sender_id or ""),
            text,
            origin_is_agent=False,
            channel_message_seq=int(last.get("seq") or 0) or None,
            participants=frozenset(participants),
            attachments=tuple(attachments),
            sent_at=int(last.get("sent_at") or 0),
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            client_message_id=client_message_id or None,
        )
        LOGGER.info(
            "merged unconsumed overlay messages into a persistent turn "
            "turn=%s count=%s seq=%s",
            turn.turn_id,
            len(ordered),
            last.get("seq"),
        )

    def _cleanup_turn_overlay_files(self, turn: Turn) -> None:
        for path in list(turn.overlay_files.values()):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        turn.overlay_files.clear()
        with contextlib.suppress(OSError):
            (self._turn_overlay_dir() / turn.turn_id).rmdir()

    async def _finalize_turn_overlays(self, turn: Turn) -> None:
        human_items: list[dict[str, Any]] = []
        agent_items: list[dict[str, Any]] = []
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)):
                agent_items.append(item)
            else:
                human_items.append(item)
        turn.busy_messages = agent_items
        human_items.sort(
            key=lambda item: (
                int(item.get("seq") or 0),
                int(item.get("sent_at") or 0),
            )
        )
        consumed: list[dict[str, Any]] = []
        unconsumed: list[dict[str, Any]] = []
        for item in human_items:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0 and seq in turn.waited_user_seqs:
                self._remove_turn_overlay_file(turn, seq)
                continue
            path = turn.overlay_files.get(seq)
            if path is not None and not path.exists():
                turn.overlay_consumed_seqs.add(seq)
                consumed.append(item)
            else:
                unconsumed.append(item)
        if consumed:
            try:
                await self._merge_consumed_user_messages(turn, consumed)
            except Exception:
                LOGGER.exception(
                    "failed to merge consumed channel messages into user role "
                    "turn=%s; keeping them consumed without a duplicate turn",
                    turn.turn_id,
                )
        self._enqueue_merged_overlay_turn(turn, unconsumed)
        self._cleanup_turn_overlay_files(turn)

    async def _finalize_turn_state(self, turn: Turn) -> None:
        if self._wait_user_active:
            with contextlib.suppress(Exception):
                await self._set_wait_user_active(False)
        if self._wait_other_active:
            with contextlib.suppress(Exception):
                await self._set_wait_other_active(False)
        self._release_turn_lock(turn)
        turn.done_event.set()
        if self.active is turn:
            self.active = None
        self._schedule_status_refresh()
        await self._maybe_enqueue_proactive_turn()
        await self._maybe_start_next()

    async def _cancel_turn(self, turn: Turn, *, disconnect: bool = False) -> None:
        if turn.cancelled or turn.end_sent:
            return
        turn.cancelled = True
        for approval in list(self.approvals.values()):
            if approval.turn is turn and not approval.future.done():
                approval.future.set_result((False, "turn cancelled"))
        if self._shutting_down:
            self._release_turn_lock(turn)
            turn.end_sent = True
            turn.done_event.set()
            if self.active is turn:
                self.active = None
            return
        if bool(getattr(self.backend, "ready", False)):
            with contextlib.suppress(Exception):
                await self.backend.send_interrupt()
        LOGGER.info("turn cancel requested id=%s disconnect=%s", turn.turn_id, disconnect)
        try:
            await asyncio.wait_for(turn.done_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            await self._finish_turn(
                turn,
                "cancelled",
                {"message": "turn cancelled by client" if not disconnect else "client disconnected"},
            )

    def _release_turn_lock(self, turn: Turn) -> None:
        if not turn.lock_held and not self.execution_lock.held_by(
            profile=self.config.profile, turn_id=turn.turn_id
        ):
            return
        self.execution_lock.release(
            profile=self.config.profile, turn_id=turn.turn_id
        )
        turn.lock_held = False

    def _find_turn(self, turn_id: str) -> Turn | None:
        if self.active is not None and self.active.turn_id == turn_id:
            return self.active
        for turn in self.queue:
            if turn.turn_id == turn_id:
                return turn
        return None

    # -------------------------------------------------------- writable dirs
    async def _writable_add(self, params: Mapping[str, Any]) -> dict[str, Any]:
        path = require_string(params, "path")
        async with self._writable_lock:
            result = self.writable.add(path)
            if not result.get("added"):
                result.update(
                    {
                        "backend_restarted": False,
                        "interrupted_turn_id": None,
                        "backend_ready": bool(getattr(self.backend, "ready", False)),
                    }
                )
                return result
            result.update(await self._restart_for_policy_change())
            return result

    async def _writable_remove(self, params: Mapping[str, Any]) -> dict[str, Any]:
        path = require_string(params, "path")
        async with self._writable_lock:
            result = self.writable.remove(path)
            result.update(await self._restart_for_policy_change())
            return result

    async def _writable_clear(self) -> dict[str, Any]:
        async with self._writable_lock:
            result = self.writable.clear()
            if not result.get("removed_count"):
                result.update(
                    {
                        "backend_restarted": False,
                        "interrupted_turn_id": None,
                        "backend_ready": bool(getattr(self.backend, "ready", False)),
                    }
                )
                return result
            result.update(await self._restart_for_policy_change())
            return result

    async def _mcp_native_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        server = require_string(params, "server").strip()
        if not server:
            raise RpcError(ErrorCode.INVALID_PARAMS, "server must be non-empty")
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcError(ErrorCode.INVALID_PARAMS, "enabled must be a boolean")
        async with self._mcp_lock:
            before = set(self._mcp_native_excluded)
            if enabled:
                self._mcp_native_excluded.discard(server)
            else:
                self._mcp_native_excluded.add(server)
            changed = before != self._mcp_native_excluded
            if not changed:
                return {
                    "server": server,
                    "enabled": enabled,
                    "changed": False,
                    "backend_restarted": False,
                    "interrupted_turn_id": None,
                    "backend_ready": bool(getattr(self.backend, "ready", False)),
                    "status": self._mcp_native_status(),
                }
            try:
                self._save_mcp_native_state()
            except OSError as exc:
                self._mcp_native_excluded = before
                raise RpcError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"cannot persist MCP native state: {exc}",
                ) from exc
            if self.config.mcp_native_tools_enabled:
                restart = await self._restart_backend_for_mcp_change()
            else:
                restart = {
                    "backend_restarted": False,
                    "interrupted_turn_id": None,
                    "backend_ready": bool(getattr(self.backend, "ready", False)),
                    "external_tools_pending": self._external_tools_pending,
                }
            return {
                "server": server,
                "enabled": enabled,
                "changed": True,
                **restart,
                "status": self._mcp_native_status(),
            }

    def _mqtt_status(self) -> dict[str, Any]:
        channel = self.channel
        return {
            "enabled": channel is not None,
            "host": self.config.mqtt_host,
            "port": self.config.mqtt_port,
            "topic": self.config.mqtt_topic,
            "state": channel.state if channel is not None else "disabled",
            "connected": channel.connected if channel is not None else False,
            "last_error": channel.last_error if channel is not None else None,
            "persisted": self.mqtt_state_path.exists(),
            "state_path": str(self.mqtt_state_path),
        }

    # -------------------------------------------------------- channel control
    def _start_channel_status_loop(self) -> None:
        if self._status_task is not None and not self._status_task.done():
            return
        self._status_stop.clear()
        self._status_task = asyncio.create_task(
            self._channel_status_loop(), name="akid-channel-status"
        )

    async def _channel_status_loop(self) -> None:
        next_status = 0.0
        next_probe = 0.0
        while not self._status_stop.is_set():
            accelerated = False
            now = time.monotonic()
            timeout = max(0.2, min(next_status, next_probe) - now)
            try:
                await asyncio.wait_for(self._status_event.wait(), timeout=timeout)
                accelerated = True
            except asyncio.TimeoutError:
                pass
            self._status_event.clear()
            if self._status_stop.is_set():
                break
            self._prune_channel_members()
            channel = self.channel
            if channel is None or not channel.connected:
                next_status = time.monotonic() + 1.0
                next_probe = next_status
                continue
            now = time.monotonic()
            if accelerated or now >= next_status:
                try:
                    await self._publish_channel_status()
                    next_status = now + CHANNEL_STATUS_INTERVAL
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.warning("channel status publish failed: %s", exc)
                    next_status = now + CHANNEL_STATUS_INTERVAL
            if now >= next_probe:
                try:
                    await self._publish_channel_probe()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.warning("channel roster probe failed: %s", exc)
                next_probe = now + CHANNEL_ROSTER_PROBE_INTERVAL + random.uniform(0.0, 2.0)

    def _schedule_status_refresh(self) -> None:
        if self.channel is not None:
            self._status_event.set()

    async def _on_channel_connected(self) -> None:
        with contextlib.suppress(Exception):
            await self._publish_channel_probe()
        self._schedule_status_refresh()
        with contextlib.suppress(Exception):
            await self._publish_channel_status()

    @staticmethod
    def _token_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, Mapping):
            for key in ("total_tokens", "total", "tokens", "count", "context_tokens"):
                candidate = value.get(key)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    return candidate
            numeric = [
                item
                for item in value.values()
                if isinstance(item, int) and not isinstance(item, bool)
            ]
            if numeric:
                return sum(numeric)
        return 0

    def _channel_capabilities(self) -> str:
        return ",".join(
            sorted(
                name
                for name, value in CAPABILITIES.items()
                if value is True and name not in {"queue_max", "protocol_version"}
            )
        )

    def _channel_identity_metadata(self) -> dict[str, str]:
        config = self.config
        member_id = self._channel_self_key()
        return {
            "schema": "akid.v1",
            "memberId": member_id,
            "memberName": config.channel_name,
            "sourceId": member_id,
            # Old clients keep using agentId as their stable key.
            "agentId": member_id,
            "agent": member_id,
            "agentName": config.channel_name,
            "description": truncate_member_description(config.channel_description),
            "memberKind": MEMBER_KIND_AGENT,
            "aliases": ",".join(config.channel_aliases[:16]),
            "protocolVersion": str(CHANNEL_PROTOCOL_VERSION),
        }

    def _channel_live_metadata(self) -> dict[str, str]:
        model, reasoning, _context_limit = self._agent_info()
        channel = self.channel
        channel_ready = bool(channel is not None and channel.connected)
        metadata = self._channel_identity_metadata()
        metadata.update(
            {
                "capabilities": self._channel_capabilities(),
                "model": model or "",
                "reasoning": reasoning or "",
                "backendReady": "true"
                if bool(getattr(self.backend, "ready", False))
                else "false",
                "channelReady": "true" if channel_ready else "false",
                "waitingUser": "true" if self._wait_user_active else "false",
                "waitingOther": "true" if self._wait_other_active else "false",
                "waitingOtherTargets": ",".join(self._wait_other_targets),
            }
        )
        return metadata

    def _channel_control_metadata(self) -> dict[str, str]:
        model, reasoning, context_limit = self._agent_info()
        usage = self.last_usage if isinstance(self.last_usage, dict) else {}
        context_tokens = self._token_count(
            usage.get("context_tokens") or usage.get("prompt_tokens")
        )
        if context_limit is None:
            value = usage.get("context_limit")
            context_limit = value if isinstance(value, int) else None
        cached_tokens = self._token_cache[1]
        sandbox = self._sandbox_health()
        sandbox_ready = not sandbox.get("enabled", False) or bool(
            sandbox.get("available", False)
        )
        metadata = self._channel_live_metadata()
        metadata.update(
            {
                "kind": "status",
                "contextLimit": str(context_limit or 0),
                "contextTokens": str(max(context_tokens, 0)),
                "sandboxReady": "true" if sandbox_ready else "false",
                "queueDepth": str(len(self.queue)),
                "activeTurn": "true" if self.active is not None else "false",
                "pendingApprovals": str(len(self.approvals)),
                "uptimeS": str(int(max(0.0, time.time() - self.started_at))),
                "memoryTokens": str(self._token_count(cached_tokens)),
            }
        )
        return metadata

    async def _publish_channel_control(
        self,
        text: str,
        metadata: Mapping[str, Any],
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> bool:
        channel = self.channel
        if channel is None or not channel.connected:
            return False
        await channel.publish_control(text, metadata, qos=qos, retain=retain)
        return True

    async def _publish_channel_status(self) -> None:
        await self._publish_channel_control(
            f"{self.config.channel_name} status", self._channel_control_metadata()
        )

    async def _publish_channel_probe(self) -> None:
        metadata = self._channel_live_metadata()
        metadata.update(
            {"kind": "probe", "probeId": f"probe-{uuid.uuid4().hex}"}
        )
        await self._publish_channel_control(
            f"{self.config.channel_name} probe", metadata
        )

    async def _publish_channel_presence(self, probe_id: str) -> None:
        metadata = self._channel_live_metadata()
        metadata.update({"kind": "presence", "probeId": probe_id})
        await self._publish_channel_control(
            f"{self.config.channel_name} is online", metadata
        )

    async def _publish_channel_offline(self) -> None:
        metadata = self._channel_identity_metadata()
        metadata.update({"kind": "offline"})
        await self._publish_channel_control(
            f"{self.config.channel_name} is offline", metadata
        )

    def _member_key(
        self,
        metadata: Mapping[str, Any],
        sender_name: str = "",
        sender_id: str = "",
    ) -> str:
        explicit = str(metadata.get("memberId") or "").strip()
        if is_member_id(explicit):
            return explicit
        if is_member_id(sender_id):
            return sender_id.strip()
        name = (
            str(metadata.get("agentName") or "").strip()
            or sender_name.strip()
            or str(metadata.get("agentId") or metadata.get("agent") or "").strip()
            or sender_id.strip()
        )
        known = self._resolve_label_member_id(name)
        if known:
            return known
        return legacy_member_id(name) if name else ""

    def _resolve_label_member_id(self, label: object) -> str:
        if self._identity_registry is not None:
            owner = self._identity_registry.resolve(label)
            if owner:
                return owner
        key = member_name_key(label)
        if not key:
            return ""
        for member_id, member in self._channel_members.items():
            aliases = {
                member_name_key(member.get("name")),
                *(member_name_key(alias) for alias in member.get("aliases") or ()),
            }
            if key in aliases:
                return member_id
        return ""

    def _update_channel_member(
        self,
        envelope: TextEnvelope,
        metadata: Mapping[str, Any],
    ) -> str:
        member_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
        if not member_id or member_id == self._channel_self_key():
            return member_id
        now = time.monotonic()
        source_id = (
            str(metadata.get("sourceId") or "").strip()
            or envelope.sender_id.strip()
            or member_id
        )
        previous = self._channel_members.get(member_id)
        sources = dict((previous or {}).get("sources") or {})
        if previous and not sources:
            previous_source = str(previous.get("source_id") or member_id)
            sources[previous_source] = float(previous.get("last_seen", 0.0))
        sources[source_id] = now
        owner = str((previous or {}).get("source_id") or source_id)
        update_profile = source_id == owner
        name = str(
            (metadata.get("agentName") if update_profile else "")
            or (envelope.sender if update_profile else "")
            or (previous or {}).get("name")
            or member_id
        ).strip()
        if update_profile and "aliases" in metadata:
            aliases = tuple(
                item.strip()
                for item in str(metadata.get("aliases") or "").split(",")
                if item.strip()
            )
        else:
            aliases = tuple((previous or {}).get("aliases") or ())
        description = (
            truncate_member_description(metadata.get("description"))
            if update_profile and "description" in metadata
            else str((previous or {}).get("description") or "")
        )
        member_kind = (
            normalize_member_kind(metadata.get("memberKind"))
            if update_profile and "memberKind" in metadata
            else normalize_member_kind((previous or {}).get("kind"))
        )
        claimed = True
        if self._identity_registry is not None:
            identity = MemberIdentity(
                id=member_id,
                name=name,
                aliases=aliases,
                kind=member_kind,
            )
            claimed, conflicts = self._identity_registry.observe(
                identity, source=source_id
            )
            for conflict in conflicts:
                LOGGER.warning(
                    "remote identity label conflict label=%s owner=%s member=%s source=%s",
                    conflict.get("label"),
                    conflict.get("owner"),
                    conflict.get("member_id"),
                    source_id,
                )
            if not claimed:
                # The existing owner keeps its labels.  Keep the conflicting id
                # online, but do not let it capture mention routing.
                aliases = tuple((previous or {}).get("aliases") or ())
                if previous:
                    name = str(previous.get("name") or member_id)
                else:
                    name = member_id
        self._channel_members[member_id] = {
            "id": member_id,
            "name": name[:80] or member_id,
            "aliases": aliases[:16],
            "description": description,
            "kind": member_kind,
            "labelsClaimed": claimed,
            "source_id": owner,
            "sources": sources,
            "last_seen": now,
        }
        if member_kind == MEMBER_KIND_USER:
            self._normalize_channel_history_kind(member_id, member_kind)
        return member_id

    def _next_channel_event_seq(self) -> int:
        self._channel_event_seq += 1
        return self._channel_event_seq

    def _agent_channel_state(self, member_id: str) -> AgentChannelState:
        state = self._channel_agent_states.get(member_id)
        if state is None:
            state = AgentChannelState()
            self._channel_agent_states[member_id] = state
        return state

    def _observe_channel_turn_event(
        self,
        member_id: str,
        metadata: Mapping[str, Any],
        text: str,
        *,
        sent_at: int = 0,
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        if metadata.get("schema") != "akid.v1" or metadata.get("kind") != "turn":
            return
        phase = str(metadata.get("phase") or "").strip()
        if phase not in ("started", "final", "ended", "no_reply"):
            return
        state = self._agent_channel_state(member_id)
        seq = self._next_channel_event_seq()
        turn_id = str(metadata.get("turnId") or "")
        timestamp = sent_at if sent_at > 0 else int(time.time() * 1000)
        if phase == "started":
            state.active = True
            state.active_turn_id = turn_id
            state.last_final_text = ""
            state.last_final_turn_id = ""
            state.last_turn_event_sent_at = timestamp
            return
        if phase == "final":
            state.last_final_text = text
            state.last_final_turn_id = turn_id
            state.last_turn_event_sent_at = timestamp
            return
        state.active = False
        state.active_turn_id = ""
        state.queue_depth = 0
        state.last_completion_seq = seq
        state.last_completion_phase = phase
        state.last_completion_sent_at = timestamp
        state.last_turn_event_sent_at = timestamp
        if phase == "no_reply":
            state.last_final_text = ""
            state.last_final_turn_id = ""

    def _observe_channel_status(
        self,
        member_id: str,
        metadata: Mapping[str, Any],
        *,
        sent_at: int = 0,
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        state = self._agent_channel_state(member_id)
        timestamp = sent_at if sent_at > 0 else int(time.time() * 1000)
        if timestamp < max(
            state.last_completion_sent_at, state.last_turn_event_sent_at
        ):
            return
        try:
            queue_depth = max(0, int(metadata.get("queueDepth") or 0))
        except (TypeError, ValueError):
            queue_depth = 0
        state.queue_depth = queue_depth
        state.active = str(metadata.get("activeTurn") or "").lower() == "true"
        state.last_status_seq = self._next_channel_event_seq()
        state.last_status_sent_at = timestamp
        if not state.active and queue_depth == 0:
            state.active_turn_id = ""

    def _observe_channel_waiting_user(
        self, member_id: str, metadata: Mapping[str, Any]
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        value = metadata.get("waitingUser")
        if value is None:
            return
        waiting = str(value).strip().lower() in ("true", "1", "yes", "on")
        if waiting:
            self._channel_waiting_user[member_id] = time.monotonic()
        else:
            self._channel_waiting_user.pop(member_id, None)

    def _channel_waiting_user_ids(self) -> set[str]:
        cutoff = time.monotonic() - CHANNEL_MEMBER_TIMEOUT
        self_key = self._channel_self_key()
        self._channel_waiting_user = {
            member_id: seen_at
            for member_id, seen_at in self._channel_waiting_user.items()
            if seen_at >= cutoff
        }
        waiting = set(self._channel_waiting_user)
        if self._wait_user_active:
            waiting.add(self_key)
        return waiting

    async def _set_wait_user_active(self, active: bool) -> None:
        if self._wait_user_active == active:
            return
        self._wait_user_active = active
        self._schedule_status_refresh()
        channel = self.channel
        if channel is None or not channel.connected:
            return
        metadata = {
            **self._channel_live_metadata(),
            "kind": "wait_user",
            "waitingUser": "true" if active else "false",
        }
        try:
            await channel.publish_text(
                "waiting for user" if active else "stopped waiting for user",
                metadata,
            )
        except Exception as exc:
            LOGGER.warning("failed to publish wait_user state active=%s: %s", active, exc)

    async def _set_wait_other_active(
        self, active: bool, targets: tuple[str, ...] = ()
    ) -> None:
        normalized = tuple(label.strip() for label in targets if label.strip())
        if self._wait_other_active == active and self._wait_other_targets == normalized:
            return
        self._wait_other_active = active
        self._wait_other_targets = normalized if active else ()
        self._schedule_status_refresh()
        channel = self.channel
        if channel is None or not channel.connected:
            return
        metadata = {
            **self._channel_live_metadata(),
            "kind": "wait_other",
            "waitingOther": "true" if active else "false",
            "waitingOtherTargets": ",".join(self._wait_other_targets),
        }
        try:
            await channel.publish_text(
                "waiting for other agents" if active else "stopped waiting for other agents",
                metadata,
            )
        except Exception as exc:
            LOGGER.warning("failed to publish wait_other state active=%s: %s", active, exc)

    def _mark_channel_member_offline(self, member_id: str) -> None:
        self._channel_waiting_user.pop(member_id, None)
        state = self._channel_agent_states.get(member_id)
        if state is None:
            return
        state.active = False
        state.active_turn_id = ""
        state.queue_depth = 0
        state.last_completion_seq = self._next_channel_event_seq()
        state.last_completion_phase = "offline"
        state.last_completion_sent_at = int(time.time() * 1000)

    def _normalize_channel_history_kind(self, member_id: str, member_kind: str) -> None:
        if member_kind != MEMBER_KIND_USER:
            return
        changed = False
        normalized: list[ChannelTranscriptEntry] = []
        for entry in self._channel_history:
            resolved = self._history_member_id(entry.sender, entry.sender_id)
            if resolved == member_id and (entry.from_agent or entry.sender_id != member_id):
                entry = replace(
                    entry,
                    sender_id=member_id,
                    from_agent=False if entry.from_agent else entry.from_agent,
                )
                changed = True
            normalized.append(entry)
        if not changed:
            return
        self._channel_history = normalized
        self._persist_channel_history()

    def _drop_channel_member_source(self, member_id: str, source_id: str) -> None:
        member = self._channel_members.get(member_id)
        if member is None:
            return
        sources = dict(member.get("sources") or {})
        sources.pop(source_id, None)
        if not sources:
            self._channel_members.pop(member_id, None)
            self._channel_waiting_user.pop(member_id, None)
            self._mark_channel_member_offline(member_id)
            return
        updated = dict(member)
        updated["sources"] = sources
        updated["last_seen"] = max(sources.values())
        self._channel_members[member_id] = updated

    def _prune_channel_members(self) -> None:
        cutoff = time.monotonic() - CHANNEL_MEMBER_TIMEOUT
        kept: dict[str, dict[str, Any]] = {}
        for key, member in self._channel_members.items():
            sources = dict(member.get("sources") or {})
            if not sources:
                if float(member.get("last_seen", 0.0)) >= cutoff:
                    kept[key] = member
                continue
            active = {
                source_id: seen_at
                for source_id, seen_at in sources.items()
                if float(seen_at) >= cutoff
            }
            if not active:
                continue
            updated = dict(member)
            updated["sources"] = active
            updated["last_seen"] = max(active.values())
            kept[key] = updated
        self._channel_members = kept
        self._channel_waiting_user = {
            member_id: seen_at
            for member_id, seen_at in self._channel_waiting_user.items()
            if seen_at >= cutoff
        }

    def _channel_self_key(self) -> str:
        return self.config.channel_member_id or legacy_member_id(
            self.config.channel_name
        )

    def _channel_self_member(self) -> ChannelMember:
        return ChannelMember(
            id=self._channel_self_key(),
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            description=truncate_member_description(self.config.channel_description),
            is_self=True,
            kind=MEMBER_KIND_AGENT,
        )

    def _history_member_id(self, sender: str, sender_id: str = "") -> str:
        candidate = str(sender_id or "").strip()
        if is_member_id(candidate):
            return candidate
        for label in (sender, candidate):
            owner = self._resolve_label_member_id(label)
            if owner:
                return owner
        name = str(sender or candidate).strip()
        return legacy_member_id(name) if name else ""

    def _member_display_name(self, member_id: str, fallback: str = "") -> str:
        if member_id and member_id == self._channel_self_key():
            return self.config.channel_name
        member = self._channel_members.get(member_id) if member_id else None
        if member is not None:
            return str(member.get("name") or fallback)
        if self._identity_registry is not None and member_id:
            identity = self._identity_registry.get(member_id)
            if identity is not None:
                return identity.name
        return str(fallback or "")

    def _channel_member_views(self) -> list[ChannelMember]:
        self._prune_channel_members()
        views = [self._channel_self_member()]
        for member in self._channel_members.values():
            views.append(
                ChannelMember(
                    id=str(member["id"]),
                    name=str(member["name"]),
                    aliases=tuple(member.get("aliases") or ()),
                    description=str(member.get("description") or ""),
                    kind=normalize_member_kind(member.get("kind")),
                )
            )
        return views

    def _solo_status(self) -> dict[str, Any]:
        session = self._solo_session
        return {
            "active": session is not None,
            "session_id": session.session_id if session else "",
            "target_id": session.target_id if session else "",
            "target_name": session.target_name if session else "",
            "started_at": session.started_at if session else "",
            "group_magic_session": self.config.mqtt_magic,
            "known_solo_targets": sorted(self._solo_targets),
            "group_transcript_frozen": session is not None,
            "solo_history_path": str(self.solo_channel_history_path),
            "memory": self.solo_memory.status(),
        }

    async def _cancel_mqtt_turns_for_solo(self) -> None:
        queued = [turn for turn in self.queue if turn.origin == "mqtt"]
        if queued:
            self.queue = [turn for turn in self.queue if turn.origin != "mqtt"]
            for turn in queued:
                turn.cancelled = True
                turn.end_sent = True
                turn.done_event.set()
        active = self.active
        if active is not None and active.origin == "mqtt" and not active.end_sent:
            await self._cancel_turn(active)

    async def _activate_solo(self, session: SoloSession) -> None:
        if session.target_id != self._channel_self_key():
            return
        if self._solo_session is not None:
            if self._solo_session.session_id == session.session_id:
                await self._publish_solo_ready(session)
            else:
                await self._publish_channel_control(
                    "solo busy",
                    {
                        **self._channel_identity_metadata(),
                        "kind": SOLO_FAILED,
                        "soloId": session.session_id,
                        "soloTargetId": session.target_id,
                        "soloError": "another solo session is already active",
                    },
                    qos=1,
                )
            return
        scene = load_solo_scene(
            self.config.profile,
            username=self.config.username,
            explicit=(
                self.config.aki.solo_prompt_file
                if self.config.aki is not None
                else ""
            ),
        )
        await self._cancel_mqtt_turns_for_solo()
        self._persist_channel_history()
        self._frozen_group_history = list(self._channel_history)
        self._frozen_group_history_seq = self._channel_history_seq
        self._solo_session = session
        self._solo_scene = scene
        save_solo_session(self.solo_state_path, session)
        self._solo_targets.pop(self._channel_self_key(), None)
        await self._publish_solo_ready(session)

        channel = self.channel
        if channel is not None:
            channel.set_active_magic(
                derive_solo_magic(self.config.mqtt_magic, self._channel_self_key())
            )
            channel.discard_inbox()
        self._load_channel_history(self.solo_channel_history_path)
        self._solo_memory_start_seq = int(self._channel_history_seq)
        await self._refresh_channel_transcript()
        await self._publish_channel_control(
            f"{self.config.channel_name} solo active",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_ACTIVE,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
                "soloStartedAt": session.started_at,
            },
            qos=1,
            retain=True,
        )
        LOGGER.info(
            "solo channel entered target=%s session=%s",
            session.target_name,
            session.session_id,
        )

    async def _publish_solo_ready(self, session: SoloSession) -> None:
        await self._publish_channel_control(
            f"{self.config.channel_name} solo ready",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_READY,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
                "soloStartedAt": session.started_at,
            },
            qos=1,
        )

    async def _deactivate_solo(self, *, session_id: str = "") -> None:
        session = self._solo_session
        if session is None:
            return
        if session_id and session_id != session.session_id:
            return
        solo_history = list(self._channel_history)
        await self._publish_channel_control(
            f"{self.config.channel_name} solo end",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_END,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
            },
            qos=1,
            retain=True,
        )
        self._persist_channel_history()
        self._solo_session = None
        self._solo_scene = ""
        clear_solo_session(self.solo_state_path)
        channel = self.channel
        if channel is not None:
            channel.set_active_magic(self.config.mqtt_magic)
            channel.discard_inbox()
        self._load_channel_history(self.group_channel_history_path)
        if not self._channel_history and self._frozen_group_history:
            self._channel_history = list(self._frozen_group_history)
            self._channel_history_seq = self._frozen_group_history_seq
        self._frozen_group_history = []
        self._frozen_group_history_seq = 0
        await self._finalize_solo_memory_session(session, solo_history)
        self._solo_memory_start_seq = int(self._channel_history_seq)
        await self._refresh_channel_transcript()
        await self._publish_channel_control(
            f"{self.config.channel_name} solo ended",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_END,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
            },
            qos=1,
            retain=True,
        )
        LOGGER.info("solo channel exited session=%s", session.session_id)

    async def _on_channel_control(self, envelope: TextEnvelope) -> None:
        metadata = envelope.metadata
        if metadata.get("schema") != "akid.v1":
            return
        kind = metadata.get("kind")
        if kind in (SOLO_START, SOLO_READY, SOLO_FAILED, SOLO_ACTIVE, SOLO_END):
            await self._on_solo_control(envelope)
            return
        if kind not in ("probe", "presence", "status", "offline"):
            return
        if metadata.get("protocolVersion") != str(CHANNEL_PROTOCOL_VERSION):
            return
        member_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
        if not member_id or member_id == self._channel_self_key():
            return
        if kind == "probe":
            probe_id = str(metadata.get("probeId") or "").strip()
            if not probe_id or not envelope.text.strip():
                return
            now = time.monotonic()
            cutoff = now - CHANNEL_PROBE_SEEN_TTL
            self._probe_seen = {
                key: value
                for key, value in self._probe_seen.items()
                if value >= cutoff
            }
            self._probe_rate = {
                key: value
                for key, value in self._probe_rate.items()
                if value >= cutoff
            }
            if probe_id in self._probe_seen:
                return
            sender_key = envelope.sender_id or envelope.sender or "unknown"
            if now - self._probe_rate.get(sender_key, float("-inf")) < CHANNEL_PROBE_RATE_LIMIT:
                return
            self._probe_seen[probe_id] = now
            self._probe_rate[sender_key] = now
            if member_id:
                self._update_channel_member(envelope, metadata)
            await self._publish_channel_presence(probe_id)
            return
        if not member_id:
            return
        if kind == "offline":
            source_id = (
                str(metadata.get("sourceId") or "").strip()
                or envelope.sender_id.strip()
                or member_id
            )
            self._drop_channel_member_source(member_id, source_id)
            return
        self._update_channel_member(envelope, metadata)
        if kind in ("presence", "status"):
            self._observe_channel_waiting_user(member_id, metadata)
        if kind == "status":
            self._observe_channel_status(
                member_id, metadata, sent_at=envelope.sent_at
            )

    async def _on_solo_control(self, envelope: TextEnvelope) -> None:
        metadata = envelope.metadata
        if metadata.get("protocolVersion") != str(CHANNEL_PROTOCOL_VERSION):
            return
        kind = str(metadata.get("kind") or "")
        session_id = str(metadata.get("soloId") or "").strip()
        target_id = str(metadata.get("soloTargetId") or "").strip()
        target_name = str(metadata.get("soloTargetName") or "").strip()
        if not target_id:
            return
        if kind == SOLO_START:
            if not session_id:
                return
            if metadata.get("memberKind") != MEMBER_KIND_USER:
                return
            sender_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
            if not sender_id or sender_id == self._channel_self_key():
                return
            if target_id == self._channel_self_key():
                session = SoloSession(
                    session_id=session_id,
                    target_id=target_id,
                    target_name=target_name or self.config.channel_name,
                    profile=self.config.profile,
                    started_at=str(metadata.get("soloStartedAt") or solo_now_iso()),
                    group_magic=self.config.mqtt_magic,
                )
                await self._activate_solo(session)
            else:
                self._solo_targets[target_id] = {
                    "session_id": session_id,
                    "target_name": target_name or target_id,
                    "started_at": str(metadata.get("soloStartedAt") or ""),
                }
            return
        if kind == SOLO_END:
            self._solo_targets.pop(target_id, None)
            if self._solo_session is not None and (
                not target_id or target_id == self._channel_self_key()
            ):
                await self._deactivate_solo(session_id=session_id)
            return
        if kind == SOLO_ACTIVE:
            return
        if kind == SOLO_READY:
            if target_id == self._channel_self_key():
                await self._publish_solo_ready(
                    self._solo_session
                    or SoloSession(
                        session_id=session_id,
                        target_id=target_id,
                        target_name=target_name or self.config.channel_name,
                        profile=self.config.profile,
                        started_at=str(metadata.get("soloStartedAt") or ""),
                        group_magic=self.config.mqtt_magic,
                    )
                )
            return
        if kind == SOLO_FAILED:
            return

    async def _mqtt_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        host = require_string(params, "host")
        host = host.strip()
        if not host or len(host) > 255 or any(char.isspace() for char in host):
            raise RpcError(ErrorCode.INVALID_PARAMS, "host must be a valid MQTT hostname or IP")
        port = require_int(params, "port", minimum=1, maximum=65535)
        async with self._mqtt_lock:
            if self.channel is None:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    "MQTT channel is disabled; set [mqtt] enabled = true in ~/.aki/aki.conf",
                )
            try:
                save_mqtt_server(host, port, self.mqtt_state_path)
            except OSError as exc:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR, f"cannot save MQTT settings: {exc}"
                ) from exc
            for approval in list(self.approvals.values()):
                if approval.turn.origin == "mqtt" and not approval.future.done():
                    approval.future.set_result((False, "MQTT server changed"))
            old = self.channel
            with contextlib.suppress(Exception):
                await old.stop()
            self.config.mqtt_host = host
            self.config.mqtt_port = port
            self._load_channel_history()
            active_magic = (
                derive_solo_magic(self.config.mqtt_magic, self._channel_self_key())
                if self._solo_session is not None
                else self.config.mqtt_magic
            )
            new_channel = TokifactorChannel(
                host=host,
                port=port,
                magic=active_magic,
                session_magic=self.config.mqtt_magic,
                topic=self.config.mqtt_topic,
                sender_name=self.config.mqtt_sender_name,
                sender_id=self.config.mqtt_sender_id,
                dedup_path=self.channel_history_path.with_suffix(".seen.json"),
                avatar_cache_path=self.channel_history_path.with_suffix(".avatars"),
                on_text=self._on_channel_text,
                on_image=self._on_channel_image,
                on_control=self._on_channel_control,
                on_connected=self._on_channel_connected,
                logger=logging.getLogger("akid.channel"),
            )
            self.channel = new_channel
            await new_channel.start()
            deadline = time.monotonic() + 10.0
            while not new_channel.connected and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            result = self._mqtt_status()
            result["persisted"] = True
            LOGGER.info(
                "MQTT broker changed host=%s port=%s connected=%s",
                host,
                port,
                new_channel.connected,
            )
            return result

    def _provider_id(self) -> str:
        aki = self.config.aki
        value = getattr(aki, "provider_id", None) if aki is not None else None
        if isinstance(value, str) and value:
            return value
        return "aki" if self.config.profile == "aki" else f"aki-{self.config.profile}"

    def _reload_model_catalog(self) -> None:
        aki = self.config.aki
        path = getattr(aki, "path", None) if aki is not None else None
        if not path:
            return
        try:
            loaded = load_config(
                Path(path),
                profile=getattr(aki, "profile", self.config.profile),
            )
        except (OSError, ValueError) as exc:
            raise RpcError(ErrorCode.INTERNAL_ERROR, f"cannot read aki.conf: {exc}") from exc
        self.config.aki = loaded

    def _model_catalog(self) -> list[dict[str, str]]:
        aki = self.config.aki
        routes = () if aki is None else tuple(getattr(aki, "enabled_models", ()))
        return [
            {
                "id": route.model_id,
                "handle": f"{self._provider_id()}/{route.model_id}",
                "upstream": route.upstream_name,
                "upstream_model": route.upstream_model,
            }
            for route in routes
        ]

    def _model_status(self) -> dict[str, Any]:
        self._reload_model_catalog()
        model, _effort, _context = self._agent_info()
        return {"model": model, "models": self._model_catalog()}

    def _normalize_model_id(self, raw: str) -> str:
        text = raw.strip()
        provider_id = self._provider_id()
        while "/" in text:
            prefix, remainder = text.split("/", 1)
            if not (prefix == provider_id or prefix == "aki" or prefix.startswith("aki-")):
                break
            text = remainder
        if not text or "/" in text:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "model must be a configured id, for example gemini-3.8-flash",
            )
        return text

    async def _model_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw = params.get("model")
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "model must be a string")
        self._reload_model_catalog()
        model_id = self._normalize_model_id(raw)
        catalog = self._model_catalog()
        known = {item["id"] for item in catalog}
        if model_id not in known:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"model must be one of: {', '.join(item['id'] for item in catalog)}",
                {"models": catalog},
            )
        provider_id = self._provider_id()
        handle = f"{provider_id}/{model_id}"
        active_route = None
        if self.config.aki is not None:
            active_route = next(
                (route for route in self.config.aki.enabled_models if route.model_id == model_id),
                None,
            )
        previous = self._write_agent_model(
            handle,
            context_window=getattr(active_route, "context_window", None),
            max_tokens=getattr(active_route, "max_tokens", None),
        )
        changed = previous != handle
        if changed and hasattr(self.config.aki, "proxy_base_url"):
            letta_home = self.config.letta_home or self.writable.letta_home
            write_provider_mod(letta_home, self.config.aki)
        restart = (
            await self._restart_backend_for_mcp_change(
                code="MODEL_CHANGED",
                message="model changed; restarting backend",
                finish_reason="model_restart",
                finish_message="backend restarted for model change",
            )
            if changed
            else {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        )
        status = self._model_status()
        status.update(
            {
                "changed": changed,
                "backend_restarted": restart.get("backend_restarted", False),
                "interrupted_turn_id": restart.get("interrupted_turn_id"),
                "backend_ready": restart.get("backend_ready", False),
            }
        )
        return status

    def _write_agent_model(
        self,
        handle: str,
        *,
        context_window: int | None = None,
        max_tokens: int | None = None,
    ) -> str | None:
        target, data = self._agent_record()
        previous = data.get("model")
        data["model"] = handle
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
            data["model_settings"] = settings
        settings["provider_type"] = self._provider_id()
        if context_window is not None:
            settings["context_window_limit"] = context_window
        if max_tokens is not None:
            settings["max_tokens"] = max_tokens
        atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return previous if isinstance(previous, str) else None

    def _agent_record(self) -> tuple[Path, dict[str, Any]]:
        agent_id = self.config.agent_id
        agents = self.config.backend_dir / "agents"
        if agents.is_dir():
            for path in agents.glob("*.json"):
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(loaded, dict) and loaded.get("id") == agent_id:
                    return path, loaded
        raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "agent file is missing")

    def _compiled_persona_prompt(self) -> str:
        prompt_path = self._conversation_dir() / "system-prompt.json"
        candidates: list[str] = []
        try:
            record = json.loads(prompt_path.read_text(encoding="utf-8"))
            content = record.get("content") if isinstance(record, dict) else None
            if isinstance(content, str) and content.strip():
                candidates.append(content)
        except (OSError, ValueError):
            pass
        if not candidates:
            try:
                _path, record = self._agent_record()
                system = record.get("system")
                if isinstance(system, str) and system.strip():
                    candidates.append(system)
            except Exception:
                pass
        return strip_runtime_prompt_blocks(candidates[0] if candidates else "")

    def _solo_memory_llm_config(self) -> dict[str, str] | None:
        aki = self.config.aki
        if aki is None or not aki.proxy_token:
            return None
        model, _reasoning, _limit = self._agent_info()
        route = self._active_model_route(model)
        if route is None:
            route = next(iter(aki.enabled_models), None)
        if route is None:
            return None
        return {
            "base_prompt": self._compiled_persona_prompt(),
            "proxy_base_url": aki.proxy_base_url,
            "proxy_token": aki.proxy_token,
            "model": route.model_id,
            "api": route.api,
            "route_path": route.path,
        }

    async def _finalize_solo_memory_session(
        self,
        session: SoloSession,
        history: Sequence[ChannelTranscriptEntry],
    ) -> None:
        entries = capture_solo_entries(
            history,
            session_id=session.session_id,
            start_seq=self._solo_memory_start_seq,
        )
        if not entries:
            LOGGER.info(
                "solo memory skipped empty session=%s", session.session_id
            )
            return
        memory_session = SoloMemorySession(
            session_id=session.session_id,
            started_at=session.started_at,
            ended_at=solo_now_iso(),
            summary="",
            summary_status=FALLBACK_SUMMARY_STATUS,
            entries=entries,
        )
        config = self._solo_memory_llm_config()
        if config is not None:
            await self.solo_memory.summarize(
                memory_session,
                base_prompt=config["base_prompt"],
                proxy_base_url=config["proxy_base_url"],
                proxy_token=config["proxy_token"],
                model=config["model"],
                api=config["api"],
                route_path=config["route_path"],
                timeout=SOLO_SUMMARY_TIMEOUT_SECONDS,
            )
        else:
            memory_session.summary = fallback_summary(entries)
            memory_session.summary_status = FALLBACK_SUMMARY_STATUS
        self.solo_memory.add_session(memory_session)
        LOGGER.info(
            "solo memory stored session=%s entries=%s summary_status=%s",
            memory_session.session_id,
            len(entries),
            memory_session.summary_status,
        )

    def _active_model_route(self, model: str | None):
        aki = self.config.aki
        if aki is None or not model:
            return None
        prefix, separator, model_id = model.partition("/")
        if not separator:
            model_id = model
        elif prefix != self._provider_id():
            return None
        for route in aki.enabled_models:
            if route.model_id == model_id:
                return route
        return None

    def _reasoning_status(self) -> dict[str, Any]:
        model, effort, _context = self._agent_info()
        allowed = supported_reasoning_efforts(self._active_model_route(model))
        return {
            "model": model,
            "effort": effort or DEFAULT_REASONING_EFFORT,
            "default": DEFAULT_REASONING_EFFORT,
            "allowed": list(allowed),
        }

    def _write_reasoning_effort(self, effort: str) -> str | None:
        target, data = self._agent_record()
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
            data["model_settings"] = settings
        previous = settings.get("reasoning_effort")
        if not isinstance(previous, str):
            nested = settings.get("reasoning")
            previous = nested.get("reasoning_effort") if isinstance(nested, dict) else None
        settings["reasoning_effort"] = effort
        nested = settings.get("reasoning")
        if not isinstance(nested, dict):
            nested = {}
        nested["reasoning_effort"] = effort
        settings["reasoning"] = nested
        atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return previous if isinstance(previous, str) else None

    async def _reasoning_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw = params.get("effort", DEFAULT_REASONING_EFFORT)
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "effort must be a string")
        effort = raw.strip().lower()
        model, _current, _context = self._agent_info()
        allowed = supported_reasoning_efforts(self._active_model_route(model))
        if effort not in allowed:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"reasoning effort must be one of: {', '.join(allowed)}",
                {"allowed": list(allowed)},
            )
        previous = self._write_reasoning_effort(effort)
        changed = previous != effort
        restart = (
            await self._restart_backend_for_mcp_change(
                code="REASONING_CHANGED",
                message="reasoning effort changed; restarting backend",
                finish_reason="reasoning_restart",
                finish_message="backend restarted for reasoning effort change",
            )
            if changed
            else {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        )
        status = self._reasoning_status()
        status.update(
            {
                "changed": changed,
                "backend_restarted": restart.get("backend_restarted", False),
                "interrupted_turn_id": restart.get("interrupted_turn_id"),
                "backend_ready": restart.get("backend_ready", False),
            }
        )
        return status

    async def _restart_backend_for_mcp_change(
        self,
        *,
        code: str = "MCP_POLICY_CHANGED",
        message: str = "MCP native tool policy changed; restarting backend",
        finish_reason: str = "mcp_restart",
        finish_message: str = "backend restarted for MCP native tool policy change",
    ) -> dict[str, Any]:
        if self._compacting:
            raise RpcError(
                ErrorCode.COMPACT_FAILED, "conversation compact in progress"
            )
        if not bool(getattr(self.backend, "ready", False)):
            return {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": False,
                "external_tools_pending": self._external_tools_pending,
            }
        self._policy_restarting = True
        try:
            return await self._restart_backend_for_mcp_change_locked(
                code=code,
                message=message,
                finish_reason=finish_reason,
                finish_message=finish_message,
            )
        finally:
            self._policy_restarting = False
            if bool(getattr(self.backend, "ready", False)):
                await self._maybe_start_next()

    async def _restart_backend_for_mcp_change_locked(
        self,
        *,
        code: str = "MCP_POLICY_CHANGED",
        message: str = "MCP native tool policy changed; restarting backend",
        finish_reason: str = "mcp_restart",
        finish_message: str = "backend restarted for MCP native tool policy change",
    ) -> dict[str, Any]:
        interrupted_turn_id: str | None = None
        turn = self.active
        if turn is not None and not turn.end_sent:
            interrupted_turn_id = turn.turn_id
            turn.cancelled = True
            for approval in list(self.approvals.values()):
                if approval.turn is turn and not approval.future.done():
                    approval.future.set_result((False, message))
            await self._emit(turn, "error", {"code": code, "message": message})
            await self._finish_turn(
                turn,
                finish_reason,
                {"interrupted": True, "message": finish_message},
            )

        try:
            with contextlib.suppress(Exception):
                await self.backend.stop()
            self.backend.start()
            ready = await self._wait_backend_ready(20.0)
        except Exception as exc:
            LOGGER.exception("backend restart failed after MCP native tool policy change")
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"backend restart failed: {exc}",
                {"interrupted_turn_id": interrupted_turn_id, "backend_ready": False},
            ) from exc
        if not ready:
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "backend did not become ready",
                {
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "backend_error": getattr(self.backend, "last_error", None),
                },
            )
        try:
            await asyncio.wait_for(self._external_tools_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            LOGGER.warning("MCP native tool registration still pending after restart")
        return {
            "backend_restarted": True,
            "interrupted_turn_id": interrupted_turn_id,
            "backend_ready": True,
            "external_tools_pending": self._external_tools_pending,
        }

    async def _restart_for_policy_change(self) -> dict[str, Any]:
        if self._compacting:
            raise RpcError(
                ErrorCode.COMPACT_FAILED, "conversation compact in progress"
            )
        if not self.config.sandbox_enabled:
            return {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        self._policy_restarting = True
        try:
            return await self._restart_for_policy_change_locked()
        finally:
            self._policy_restarting = False
            if bool(getattr(self.backend, "ready", False)):
                await self._maybe_start_next()

    async def _restart_for_policy_change_locked(self) -> dict[str, Any]:
        interrupted_turn_id: str | None = None
        turn = self.active
        if turn is not None and not turn.end_sent:
            interrupted_turn_id = turn.turn_id
            turn.cancelled = True
            for approval in list(self.approvals.values()):
                if approval.turn is turn and not approval.future.done():
                    approval.future.set_result((False, "writable policy changed"))
            await self._emit(
                turn,
                "error",
                {
                    "code": "SANDBOX_POLICY_CHANGED",
                    "message": "writable directory policy changed; restarting backend",
                },
            )
            await self._finish_turn(
                turn,
                "sandbox_restart",
                {"interrupted": True, "message": "backend restarted for writable policy change"},
            )

        self._sandbox_blocked_reason = None
        self.writable.ensure_runtime_dirs()
        available, _bwrap_path, error = self.writable.probe(refresh=True)
        if not available:
            self._sandbox_blocked_reason = error or "sandbox unavailable"
            with contextlib.suppress(Exception):
                await self.backend.stop()
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                "writable policy saved but sandbox is unavailable",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "sandbox_error": self._sandbox_blocked_reason,
                },
            )

        try:
            with contextlib.suppress(Exception):
                await self.backend.stop()
            self.backend.start()
            ready = await self._wait_backend_ready(20.0)
        except Exception as exc:
            LOGGER.exception("backend restart failed after writable policy change")
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                f"writable policy saved but backend restart failed: {exc}",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                },
            ) from exc
        if not ready:
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                "writable policy saved but backend did not become ready",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "backend_error": getattr(self.backend, "last_error", None),
                },
            )
        return {
            "backend_restarted": True,
            "interrupted_turn_id": interrupted_turn_id,
            "backend_ready": True,
            "revision": self.writable.revision,
        }

    async def _wait_backend_ready(self, timeout: float) -> bool:
        wait_ready = getattr(self.backend, "wait_ready", None)
        if wait_ready is not None:
            return bool(await wait_ready(timeout))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bool(getattr(self.backend, "ready", False)):
                return True
            await asyncio.sleep(0.05)
        return bool(getattr(self.backend, "ready", False))

    # --------------------------------------------------------------- mqtt rx
    def _claim_agent_initiation(self, turn_id: str) -> bool:
        if self._agent_initiation_remaining <= 0:
            LOGGER.info(
                "agent initiation budget exhausted; suppressing turn output turn=%s",
                turn_id,
            )
            return False
        self._agent_initiation_remaining -= 1
        LOGGER.info(
            "agent initiation budget consumed remaining=%s turn=%s",
            self._agent_initiation_remaining,
            turn_id,
        )
        return True

    def _channel_sender_is_agent(
        self,
        sender: str,
        sender_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        key = self._member_key(metadata or {}, sender, sender_id)
        member = self._channel_members.get(key) if key else None
        return (
            member is not None
            and normalize_member_kind(member.get("kind")) == MEMBER_KIND_AGENT
        )

    def _channel_member_kind(self, member_id: str) -> str:
        """Return ``user``, ``agent``, or an empty string when unknown."""
        if not member_id:
            return ""
        if member_id == self._channel_self_key():
            return MEMBER_KIND_AGENT
        if self._identity_registry is not None:
            identity = self._identity_registry.get(member_id)
            if identity is not None:
                return normalize_member_kind(identity.kind)
        member = self._channel_members.get(member_id)
        if member is not None:
            return normalize_member_kind(member.get("kind"))
        return ""

    def _approval_may_be_resolved_by(self, approval: "Approval", sender_id: str) -> bool:
        """Return whether a channel sender is allowed to resolve an approval.

        Approvals triggered by another agent belong to the human user, not to
        the agent whose message happened to wake the executor.  A local client
        connection is handled separately by ``_approval_respond``.
        """
        kind = self._channel_member_kind(sender_id)
        if approval.turn.origin_is_agent:
            return kind == MEMBER_KIND_USER
        if kind == MEMBER_KIND_AGENT:
            return False
        return bool(approval.turn.origin_sender_id) and (
            sender_id == approval.turn.origin_sender_id
        )

    def _channel_agent_views(self) -> list[ChannelMember]:
        return [
            member
            for member in self._channel_member_views()
            if normalize_member_kind(member.kind) == MEMBER_KIND_AGENT
        ]

    def _channel_message_mentions(self, text: str) -> set[str]:
        if self._solo_session is not None:
            return set()
        return match_channel_mentions(text, self._channel_member_views())

    def _channel_lock_authorization(
        self, text: str, *, from_agent: bool
    ) -> tuple[bool, str]:
        if self._solo_session is not None:
            return True, "solo-mode"
        if from_agent:
            return False, "agent-message"
        mentions = self._channel_message_mentions(text)
        if self._channel_self_key() in mentions:
            return True, "user-mentioned"
        if mentions:
            return False, "mentioned-other-members"
        if len(self._channel_agent_views()) <= 1:
            return True, "single-online-member"
        return False, "user-must-name-executor"

    def _channel_busy(self) -> bool:
        return self.active is not None or bool(self.queue)

    def _channel_participants_for_event(
        self,
        *,
        from_agent: bool,
        mentions: set[str],
        wake: str,
        wake_ids: set[str],
    ) -> frozenset[str]:
        if self._solo_session is not None:
            return frozenset({self._channel_self_key()})
        if from_agent:
            if wake == CHANNEL_WAKE_MENTIONS:
                return frozenset(wake_ids)
            return frozenset(member.id for member in self._channel_agent_views() if member.id)
        if mentions:
            return frozenset(mentions)
        return frozenset(member.id for member in self._channel_agent_views() if member.id)

    def _channel_text_action(self, metadata: Mapping[str, str]) -> str:
        """Return user/final/progress actions, or an empty string to ignore."""
        schema = str(metadata.get("schema") or "").strip()
        if not schema:
            return "user"
        if schema != "akid.v1":
            return "user"
        kind = str(metadata.get("kind") or "").strip()
        phase = str(metadata.get("phase") or "").strip()
        if kind == "turn" and phase == "final":
            return "agent-final"
        if kind == "turn" and phase == "progress":
            return "agent-progress"
        if kind in ("message", "text"):
            return "user"
        return ""

    @staticmethod
    def _channel_attachment_count(metadata: Mapping[str, str]) -> int:
        try:
            return max(0, min(int(metadata.get("attachmentCount") or 0), MAX_CHANNEL_ATTACHMENTS))
        except (TypeError, ValueError):
            return 0

    def _channel_attachment_group(self, message_id: str) -> dict[str, Any]:
        group = self._channel_pending_images.get(message_id)
        if group is None:
            group = {
                "message_id": message_id,
                "attachments": {},
                "count": 1,
                "text_event": None,
                "timer": None,
                "created": time.monotonic(),
            }
            self._channel_pending_images[message_id] = group
        return group

    def _cancel_attachment_timer(self, group: dict[str, Any]) -> None:
        timer = group.get("timer")
        if isinstance(timer, asyncio.Task) and timer is not asyncio.current_task():
            timer.cancel()
        group["timer"] = None

    def _schedule_attachment_finalize(
        self, message_id: str, delay: float
    ) -> None:
        group = self._channel_pending_images.get(message_id)
        if group is None:
            return
        self._cancel_attachment_timer(group)

        async def finalize() -> None:
            await asyncio.sleep(delay)
            await self._finalize_attachment_group(message_id)

        timer = asyncio.create_task(finalize(), name=f"channel-images:{message_id[:24]}")
        group["timer"] = timer
        self._channel_attachment_tasks.add(timer)
        timer.add_done_callback(self._channel_attachment_tasks.discard)

    async def _finalize_attachment_group(self, message_id: str) -> None:
        group = self._channel_pending_images.pop(message_id, None)
        if group is None:
            return
        self._cancel_attachment_timer(group)
        attachments = tuple(
            group["attachments"][index]
            for index in sorted(group["attachments"])
            if isinstance(group["attachments"].get(index), ChannelImageAttachment)
        )
        event = group.get("text_event")
        if isinstance(event, dict):
            await self._on_channel_text(
                str(event["sender"]),
                str(event["sender_id"]),
                str(event["text"]),
                event.get("metadata") or {},
                str(event.get("message_id") or ""),
                int(event.get("sent_at") or 0),
                bool(event.get("replayed")),
                _attachments=attachments,
            )
            return
        if not attachments:
            return
        sender = str(group.get("sender") or "Unknown")
        sender_id = str(group.get("sender_id") or "")
        member_id = self._history_member_id(sender, sender_id) or sender_id
        display_sender = self._member_display_name(member_id, sender) or sender
        from_agent = self._channel_sender_is_agent(sender, sender_id, {})
        self._record_channel_message(
            sender=display_sender,
            sender_id=member_id or sender_id,
            text="",
            from_agent=from_agent,
            sent_at=int(group.get("sent_at") or 0),
            phase="final",
            arrived_while_busy=self._channel_busy(),
            attachments=attachments,
        )
        await self._refresh_channel_transcript()
        LOGGER.info(
            "channel image-only message recorded message_id=%s images=%s",
            message_id,
            len(attachments),
        )

    async def _on_channel_image(self, image: ImageEnvelope, replayed: bool = False) -> None:
        message_id = image.message_id or image.transfer_id
        if not message_id:
            return
        group = self._channel_attachment_group(message_id)
        group["count"] = max(int(group.get("count") or 1), image.attachment_count)
        group["sender"] = image.sender
        group["sender_id"] = image.sender_id
        group["sent_at"] = image.sent_at
        index = max(0, min(image.attachment_index, MAX_CHANNEL_ATTACHMENTS - 1))
        name = (image.name or f"图片{index + 1}")[:MAX_ATTACHMENT_NAME_CHARS]
        attachment = self._store_channel_image(image, name=name, index=index + 1)
        if attachment is None:
            return
        group["attachments"][index] = attachment
        group["expect_text"] = bool(group.get("expect_text")) or image.expect_text
        if len(group["attachments"]) >= int(group["count"]):
            if group.get("text_event") is not None:
                self._schedule_attachment_finalize(message_id, 0)
                return
            self._schedule_attachment_finalize(
                message_id,
                CHANNEL_IMAGE_GROUP_TIMEOUT_S
                if group.get("expect_text")
                else CHANNEL_IMAGE_GROUP_GRACE_S,
            )
        else:
            self._schedule_attachment_finalize(message_id, CHANNEL_IMAGE_GROUP_TIMEOUT_S)

    def _take_complete_attachment_group(
        self, message_id: str, metadata: Mapping[str, str]
    ) -> tuple[ChannelImageAttachment, ...] | None:
        group = self._channel_pending_images.get(message_id)
        expected = self._channel_attachment_count(metadata)
        if group is None:
            return () if expected == 0 else None
        attachments = group.get("attachments") or {}
        required = max(expected, int(group.get("count") or 1))
        if len(attachments) < required:
            return None
        self._cancel_attachment_timer(group)
        self._channel_pending_images.pop(message_id, None)
        return tuple(
            attachments[index]
            for index in sorted(attachments)
            if isinstance(attachments.get(index), ChannelImageAttachment)
        )

    async def _on_channel_text(
        self,
        sender: str,
        sender_id: str,
        text: str,
        metadata: Mapping[str, str] | None = None,
        message_id: str = "",
        sent_at: int = 0,
        replayed: bool = False,
        *,
        _attachments: tuple[ChannelImageAttachment, ...] | None = None,
    ) -> None:
        text = text.strip()
        if len(text) > MAX_CHANNEL_TEXT_CHARS:
            text = text[:MAX_CHANNEL_TEXT_CHARS] + "\n...[truncated by akid]"
        event_metadata = dict(metadata or {})
        client_message_id = (
            str(event_metadata.get("clientMessageId") or "").strip() or None
        )
        member_id = self._member_key(event_metadata, sender, sender_id)
        if (
            event_metadata.get("schema") == "akid.v1"
            and event_metadata.get("kind") == "cancel"
        ):
            await self._handle_channel_cancel(
                sender, sender_id, member_id, event_metadata
            )
            return
        attachments = _attachments
        expected_attachments = self._channel_attachment_count(event_metadata)
        if expected_attachments and attachments is None:
            complete = self._take_complete_attachment_group(message_id, event_metadata)
            if complete is None:
                group = self._channel_attachment_group(message_id)
                group["text_event"] = {
                    "sender": sender,
                    "sender_id": sender_id,
                    "text": text,
                    "metadata": event_metadata,
                    "message_id": message_id,
                    "sent_at": sent_at,
                    "replayed": replayed,
                }
                self._schedule_attachment_finalize(
                    message_id, CHANNEL_IMAGE_GROUP_TIMEOUT_S
                )
                return
            attachments = complete
        if attachments is None:
            attachments = ()
        if not replayed:
            self._observe_channel_turn_event(
                member_id or sender_id,
                event_metadata,
                text,
                sent_at=sent_at,
            )
        if not text:
            return
        display_sender = self._member_display_name(member_id, sender) or sender
        action = self._channel_text_action(event_metadata)
        if not action:
            if (
                event_metadata.get("schema") == "akid.v1"
                and event_metadata.get("kind") == "wait_user"
                and not replayed
            ):
                self._observe_channel_waiting_user(
                    member_id or sender_id, event_metadata
                )
            return
        busy = self._channel_busy()
        if action == "agent-progress":
            self._record_channel_message(
                sender=display_sender,
                sender_id=member_id or sender_id,
                text=text,
                from_agent=True,
                message_id=message_id,
                sent_at=sent_at,
                phase="progress",
                arrived_while_busy=busy,
                attachments=attachments,
            )
            if replayed:
                LOGGER.info(
                    "channel backlog progress recorded without turn seq=%s",
                    self._channel_history_seq,
                )
                if busy:
                    await self._refresh_channel_transcript()
                return
            if busy:
                await self._refresh_channel_transcript()
            return
        from_agent = action == "agent-final" or self._channel_sender_is_agent(
            sender, sender_id, event_metadata
        )
        if not from_agent and not replayed:
            self._agent_initiation_remaining = max(
                0, int(self.config.agent_initiation_limit)
            )
        if await self._handle_channel_command(text, member_id or sender_id):
            return
        entry = self._record_channel_message(
            sender=display_sender,
            sender_id=member_id or sender_id,
            text=text,
            from_agent=from_agent,
            message_id=message_id,
            sent_at=sent_at,
            phase="final",
            arrived_while_busy=busy,
            attachments=attachments,
        )
        if replayed:
            LOGGER.info(
                "channel backlog message recorded without turn seq=%s sender_id=%s",
                entry.seq,
                member_id or sender_id or "unknown",
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        if from_agent and (member_id or sender_id) != self._channel_self_key():
            character = self._layer_character_for_member(
                member_id or sender_id,
                display_sender,
            )
            self._notify_layer(character, text)
        LOGGER.info(
            "channel message accepted sender_id=%s from_agent=%s seq=%s "
            "initiation_remaining=%s",
            member_id or sender_id or "unknown",
            from_agent,
            entry.seq,
            self._agent_initiation_remaining,
        )
        mentions = self._channel_message_mentions(text)
        wake = str(event_metadata.get("wake") or CHANNEL_WAKE_ALL).strip().lower()
        if wake not in (CHANNEL_WAKE_USER, CHANNEL_WAKE_MENTIONS, CHANNEL_WAKE_ALL):
            wake = CHANNEL_WAKE_ALL
        wake_ids = {
            self._history_member_id(item.strip(), item.strip()) or item.strip()
            for item in str(event_metadata.get("wakeIds") or "").split(",")
            if item.strip()
        }
        participants = self._channel_participants_for_event(
            from_agent=from_agent,
            mentions=mentions,
            wake=wake,
            wake_ids=wake_ids,
        )
        self_key = self._channel_self_key()
        directly_targeted = self_key in wake_ids or self_key in mentions
        should_wake = True
        if from_agent:
            if wake == CHANNEL_WAKE_USER:
                should_wake = False
            elif wake == CHANNEL_WAKE_MENTIONS:
                should_wake = directly_targeted
        if (
            not from_agent
            and mentions
            and self_key not in mentions
        ):
            LOGGER.info(
                "channel user message addressed to other agents; "
                "transcript-only seq=%s mentions=%s",
                entry.seq,
                ",".join(sorted(mentions)),
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        if from_agent and not should_wake:
            LOGGER.info(
                "channel agent reply recorded without wake seq=%s wake=%s targets=%s",
                entry.seq,
                wake,
                ",".join(sorted(wake_ids)),
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        # A turn that has already sent its end event must not swallow a message
        # that arrived at the boundary. Route it through the normal queue path
        # instead of appending it to a turn whose busy list is about to drain.
        active = (
            self.active
            if self.active is not None and not self.active.end_sent
            else None
        )
        if from_agent and active is not None:
            if directly_targeted:
                active.busy_messages.append(
                    {
                        "sender": sender,
                        "sender_id": member_id or sender_id,
                        "text": text,
                        "seq": entry.seq,
                        "sent_at": sent_at,
                        "origin_is_agent": True,
                        "tool_results_at_arrival": active.tool_result_events,
                        "pending_tool_results_at_arrival": active.pending_tool_results,
                        "participants": participants,
                        "attachments": attachments,
                        "client_message_id": client_message_id,
                    }
                )
            await self._refresh_channel_transcript()
            return
        if not from_agent and active is not None:
            item = {
                "sender": sender,
                "sender_id": member_id or sender_id,
                "text": text,
                "seq": entry.seq,
                "sent_at": sent_at,
                "origin_is_agent": False,
                "tool_results_at_arrival": active.tool_result_events,
                "pending_tool_results_at_arrival": active.pending_tool_results,
                "participants": participants,
                "attachments": attachments,
                "client_message_id": client_message_id,
            }
            active.busy_messages.append(item)
            if active.wait_user_call_active:
                active.wait_user_eligible_seqs.add(entry.seq)
            else:
                path = await self._write_turn_overlay_file(active, item)
                if path is not None:
                    active.overlay_files[entry.seq] = path
            active.user_message_event.set()
            await self._refresh_channel_transcript()
            return
        if not from_agent and not mentions:
            waiting_ids = self._channel_waiting_user_ids()
            if waiting_ids and self_key not in waiting_ids:
                LOGGER.info(
                    "channel user message routed only to wait_user members; "
                    "transcript-only seq=%s waiting=%s",
                    entry.seq,
                    ",".join(sorted(waiting_ids)),
                )
                if busy:
                    await self._refresh_channel_transcript()
                return
        if busy:
            await self._refresh_channel_transcript()
        lock_authorized, lock_authorization = self._channel_lock_authorization(
            text, from_agent=from_agent
        )
        self._enqueue_channel_turn(
            display_sender,
            member_id or sender_id,
            text,
            origin_is_agent=from_agent,
            channel_message_seq=entry.seq,
            participants=participants,
            attachments=attachments,
            sent_at=sent_at,
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            client_message_id=client_message_id,
        )

    async def _handle_channel_command(self, text: str, sender_id: str = "") -> bool:
        tokens = text.split()
        if not tokens:
            return False
        head = tokens[0].lower()
        decision: bool | None = None
        approval_id: str | None = None
        if head in ("akid-approve", "akid-allow"):
            decision = True
            approval_id = tokens[1] if len(tokens) > 1 else None
        elif head in ("akid-deny", "akid-reject"):
            decision = False
            approval_id = tokens[1] if len(tokens) > 1 else None
        elif head == "akid" and len(tokens) > 1:
            verb = tokens[1].lower()
            if verb in ("approve", "allow"):
                decision = True
            elif verb in ("deny", "reject"):
                decision = False
            if decision is not None:
                approval_id = tokens[2] if len(tokens) > 2 else None
        if decision is None:
            return False

        approval: Approval | None = None
        if approval_id:
            approval = self.approvals.get(approval_id)
        else:
            approval = next(iter(self.approvals.values()), None)
        if approval is None:
            LOGGER.info(
                "channel approval command ignored: no matching local approval id=%s",
                approval_id or "(oldest)",
            )
            return True
        if approval.turn.origin != "mqtt":
            self._schedule_channel_notice(
                "akid: that approval belongs to a local client; use aki.approval.respond"
            )
            return True
        if not self._approval_may_be_resolved_by(approval, sender_id):
            LOGGER.info(
                "channel approval rejected id=%s sender_id=%s origin_sender_id=%s "
                "origin_is_agent=%s",
                approval.approval_id,
                sender_id or "unknown",
                approval.turn.origin_sender_id or "unknown",
                approval.turn.origin_is_agent,
            )
            self._schedule_channel_notice(
                "akid: only the user may resolve an approval triggered by another agent"
                if approval.turn.origin_is_agent
                else "akid: only the message sender may resolve this approval"
            )
            return True
        if approval.future.done():
            self._schedule_channel_notice("akid: that approval was already resolved")
            return True

        reason = "approved via MQTT channel" if decision else "denied via MQTT channel"
        approval.future.set_result((decision, reason))
        verb = "approved" if decision else "denied"
        LOGGER.info(
            "channel approval decision id=%s decision=%s tool=%s",
            approval.approval_id,
            verb,
            approval.tool_name,
        )
        return True

    def _wait_labels_for_participants(
        self, participants: frozenset[str]
    ) -> list[str]:
        # Aki is the first responder in the configured order and never waits
        # for another member; keep the tool and turn prompt out of her profile.
        if self.config.profile == "aki":
            return []
        labels: list[str] = []
        for label in self.config.channel_wait_for:
            resolved = self._resolve_label_member_id(label) or self._history_member_id(
                label, label
            )
            if resolved and participants and resolved not in participants:
                continue
            labels.append(label)
        return labels

    def _channel_prompt(
        self,
        sender: str,
        sender_id: str,
        text: str,
        *,
        from_agent: bool = False,
        seq: int | None = None,
        sent_at: int = 0,
        attachments: tuple[ChannelImageAttachment, ...] = (),
    ) -> str:
        member_id = self._history_member_id(sender, sender_id) or sender_id
        display_sender = self._member_display_name(member_id, sender) or sender
        identity = (
            f"{display_sender} ({member_id})" if member_id else display_sender
        )
        # The roster is injected as a bridge overlay, never in the user role.
        roster = ""
        attachment_names = ", ".join(
            attachment.name for attachment in sorted(attachments, key=lambda item: item.index)
        )
        if from_agent:
            seq_attr = f" seq={xml_quoteattr(str(seq))}" if seq is not None else ""
            body = (
                "new-message: true\n"
                f"<new-message{seq_attr} sender={xml_quoteattr(identity)} "
                f"sender_id={xml_quoteattr(member_id)} source=\"agent\" phase=\"final\" "
                "untrusted=\"true\">\n"
                f"<text>{xml_escape(text)}</text>\n"
                + (f"<attachment-names>{xml_escape(attachment_names)}</attachment-names>\n" if attachments else "")
                + "</new-message>\n"
            )
        else:
            body = f"message:\n{text}\n"
            if attachments:
                body += f"attachment-names: {attachment_names}\n"
            if attachments:
                body += (
                    "multimodal-note: 当前消息的图片已经按附件顺序直接附加在 user 消息后；"
                    "正文中的图片名称与这些图片一一对应。\n"
                )
        seq_line = f"seq: {seq}\n" if seq is not None else ""
        return (
            "[akid MQTT channel message]\n"
            f"message-time: {_format_channel_message_time(sent_at)}\n"
            f"sender: {identity}\n"
            f"{seq_line}"
            f"{roster}\n"
            f"{body}"
        )

    def _find_turn_by_client_message_id(self, value: str) -> Turn | None:
        for turn in (
            ([self.active] if self.active is not None else [])
            + list(self.queue)
        ):
            if turn.client_message_id == value:
                return turn
        return None

    async def _handle_channel_cancel(
        self,
        sender: str,
        sender_id: str,
        member_id: str,
        metadata: Mapping[str, str],
    ) -> None:
        turn_id = str(metadata.get("turnId") or "").strip()
        client_message_id = str(metadata.get("clientMessageId") or "").strip()
        if not turn_id and not client_message_id:
            self._schedule_channel_notice("akid: cancel needs turnId or clientMessageId")
            return
        turn = self._find_turn(turn_id) if turn_id else None
        if turn is None and client_message_id:
            turn = self._find_turn_by_client_message_id(client_message_id)
        if turn is None:
            self._schedule_channel_notice("akid: cancel target not found")
            return
        initiator = self._history_member_id(sender, sender_id)
        if (
            turn.origin_is_agent
            or not turn.origin_sender_id
            or initiator != turn.origin_sender_id
            or (member_id and member_id != turn.origin_sender_id)
        ):
            self._schedule_channel_notice(
                "akid: only the human who started the turn may cancel it"
            )
            return
        if turn in self.queue:
            self.queue.remove(turn)
            self._schedule_status_refresh()
            turn.state = "cancelled"
            turn.cancelled = True
            turn.end_sent = True
            turn.done_event.set()
            state = self.channel_conn._turn_state(turn.turn_id)
            state.client_message_id = turn.client_message_id or ""
            await self._emit(
                turn,
                "turn_end",
                {"reason": "cancelled", "cancelled": True},
            )
        else:
            await self._cancel_turn(turn)
        LOGGER.info(
            "channel cancel accepted turn=%s client_message_id=%s sender_id=%s",
            turn.turn_id,
            turn.client_message_id or "",
            initiator,
        )

    def _enqueue_channel_turn(
        self,
        sender: str,
        sender_id: str,
        text: str,
        *,
        origin_is_agent: bool = False,
        channel_message_seq: int | None = None,
        participants: frozenset[str] = frozenset(),
        attachments: tuple[ChannelImageAttachment, ...] = (),
        sent_at: int = 0,
        lock_authorized: bool = False,
        lock_authorization: str = "channel-unknown",
        client_message_id: str | None = None,
    ) -> None:
        if len(self.queue) >= self.config.queue_max:
            self._schedule_channel_notice("akid: queue is full; channel message dropped")
            return
        prompt = self._channel_prompt(
            sender,
            sender_id,
            text,
            from_agent=origin_is_agent,
            seq=channel_message_seq,
            sent_at=sent_at,
            attachments=attachments,
        )
        turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.channel_conn,
            message=prompt,
            include_reasoning=False,
            client_message_id=client_message_id,
            origin="mqtt",
            origin_sender_id=sender_id,
            origin_is_agent=origin_is_agent,
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            channel_message_seq=channel_message_seq,
            channel_participants=participants,
            channel_sender=sender,
            channel_text=text,
            channel_attachments=tuple(attachments),
        )
        if (
            self.active is None
            and not self.queue
            and not self._compacting
            and not self._external_tools_pending
            and bool(getattr(self.backend, "ready", False))
        ):
            self.active = turn
            turn.state = "running"
            self._spawn_turn(turn)
            return
        turn.state = "queued"
        self.queue.append(turn)
        self._schedule_status_refresh()

    def _schedule_channel_notice(self, text: str) -> None:
        self._schedule_channel_publish(text, final=False)

    def _schedule_channel_publish(self, text: str, *, final: bool) -> None:
        async def publish() -> None:
            channel = self.channel
            if channel is None:
                return
            try:
                if final:
                    await channel.publish_text(text)
                else:
                    await channel.publish_notice(text)
            except Exception as exc:
                LOGGER.warning("channel publish failed: %s", exc)

        task = asyncio.create_task(publish(), name="channel-publish")
        self._channel_tasks.add(task)
        task.add_done_callback(self._channel_tasks.discard)

    async def _emit(self, turn: Turn, event_type: str, data: Mapping[str, Any] | None = None) -> None:
        turn.seq += 1
        message = make_event(turn.turn_id, turn.seq, event_type, data, now_iso())
        if turn.origin == "mqtt" and event_type in (
            "approval_request",
            "approval_resolved",
        ):
            await self._broadcast_local_event(message)
        if turn.conn.closed:
            return
        try:
            await turn.conn.send(message)
        except FrameError:
            turn.seq += 1
            fallback = make_event(
                turn.turn_id,
                turn.seq,
                "error",
                {
                    "code": "EVENT_TOO_LARGE",
                    "message": f"{event_type} event exceeded the 1 MiB frame limit",
                },
                now_iso(),
            )
            with contextlib.suppress(Exception):
                await turn.conn.send(fallback)
        except (ConnectionResetError, BrokenPipeError):
            turn.conn.closed = True

    async def _broadcast_local_event(self, message: Mapping[str, Any]) -> None:
        """Deliver a channel-turn approval event to every local user client."""
        for conn in list(self.connection_set):
            if conn.closed or not conn.handshaken:
                continue
            try:
                await conn.send(message)
            except FrameError:
                LOGGER.warning(
                    "local approval event exceeded frame limit type=%s",
                    (message.get("params") or {}).get("type"),
                )
            except (ConnectionResetError, BrokenPipeError):
                conn.closed = True

    async def _emit_tool_call_once(
        self, turn: Turn, tool_call_id: str, name: str, arguments: Any
    ) -> None:
        if tool_call_id:
            turn.tool_names[tool_call_id] = name
        if name in HIDDEN_TOOL_NAMES:
            await self._emit(
                turn,
                "assistant_segment",
                {
                    "reason": "hidden_tool",
                    "tool_call_id": tool_call_id,
                    "name": name,
                },
            )
            return
        if tool_call_id in turn.tool_calls_seen:
            return
        turn.tool_calls_seen.add(tool_call_id)
        self.operation_journal.start(tool_call_id, name, arguments)
        await self._emit(
            turn,
            "tool_call",
            {
                "tool_call_id": tool_call_id,
                "name": name,
                "arguments": _parse_tool_arguments(arguments),
            },
        )

    async def _emit_tool_result_once(
        self, turn: Turn, tool_call_id: str, data: Mapping[str, Any]
    ) -> None:
        key = str(tool_call_id or "")
        if key:
            if key in turn.tool_results_seen:
                return
            turn.tool_results_seen.add(key)
        status = str(data.get("status") or "completed")
        self.operation_journal.finish(
            key,
            status=status,
            error=(
                data.get("tool_return") or data.get("stderr") or ""
                if status.lower() in ("error", "failed", "failure")
                else ""
            ),
        )
        if turn.tool_names.get(key) in HIDDEN_TOOL_NAMES:
            return
        await self._emit(turn, "tool_result", data)

    # -------------------------------------------------------------- backend rx
    async def _on_backend_ready(self, payload: dict[str, Any]) -> None:
        LOGGER.info("backend ready conversation=%s", payload.get("conversation_id"))
        if not self._aki_bridge_available():
            LOGGER.error(
                "backend is not the customized Aki Letta runtime: "
                "missing aki_bridge capability"
            )
            return
        self._clear_persisted_turn_blocks()
        self._reset_backend_queue_state()
        self._external_tools_registered = False
        self._external_tools_error = None
        self._mcp_tools = []
        self._mcp_tool_names = set()
        self._mcp_tools_error = None
        self._external_tools_ready.clear()
        self._external_tools_pending = bool(
            self.config.mcp_native_tools_enabled or self._external_tools()
        )
        if self._external_tools_pending:
            task = asyncio.create_task(
                self._prepare_external_tools(), name="prepare-external-tools"
            )
            self._external_tool_tasks.add(task)
            task.add_done_callback(self._external_tool_tasks.discard)
        else:
            self._external_tools_ready.set()
            await self._maybe_start_next()

    def _external_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            ACQUIRE_EXECUTION_LOCK_TOOL,
        ]
        tools.extend(tools_for_profile(self.config.profile))
        if not (
            self.config.image_generation
            and self.config.image_generation.available
        ):
            tools = [
                tool
                for tool in tools
                if tool["name"] != IMAGE_GENERATION_TOOL_NAME
            ]
        if self.config.camera_enabled and shutil.which(self.config.ffmpeg_bin):
            tools.append(CAMERA_TOOL)
        if self.config.screenshot_enabled and self._screenshot_backends():
            tools.append(SCREENSHOT_TOOL)
        if self.channel is not None:
            tools.append(TOKIFACTOR_IMAGE_TOOL)
            tools.append(WAIT_USER_TOOL_SCHEMA)
        if (
            self.channel is not None
            and self.config.channel_wait_for
            and self.config.profile != "aki"
        ):
            tools.append(WAIT_OTHER_TOOL_SCHEMA)
        tools.extend(self._mcp_tools)
        policy = self.config.tool_policy
        if policy.explicit:
            tools = [tool for tool in tools if tool["name"] in policy.allow_set]
        return tools

    async def _prepare_external_tools(self) -> None:
        try:
            if self.config.mcp_native_tools_enabled:
                await self._discover_mcp_tools()
            if self._external_tools():
                await self._register_external_tools()
        except Exception:
            LOGGER.exception("external tool preparation failed")
        finally:
            self._external_tools_pending = False
            self._external_tools_ready.set()
            await self._maybe_start_next()

    async def _discover_mcp_tools(self) -> None:
        self._mcp_tools = []
        self._mcp_tool_names = set()
        self._mcp_tools_error = None
        self._mcp_native_active_servers = []
        if self._aki_bridge_available():
            await self._discover_mcp_tools_bridge()
            return
        try:
            servers = await self._run_letta_json_value(
                ["mcp", "list", "--agent", self.config.agent_id], timeout=10.0
            )
        except Exception as exc:
            self._mcp_tools_error = str(exc)
            LOGGER.warning("MCP server discovery failed: %s", exc)
            return
        if not isinstance(servers, list):
            self._mcp_tools_error = "letta mcp list returned unexpected JSON"
            LOGGER.warning(self._mcp_tools_error)
            return

        all_names = [
            str(server.get("name"))
            for server in servers
            if isinstance(server, dict) and server.get("name")
        ]
        allowed = set(self.config.mcp_native_servers)
        names = [
            name
            for name in all_names
            if (not allowed or name in allowed)
            and name not in self._mcp_native_excluded
        ]
        self._mcp_native_active_servers = list(names)
        if not names:
            return

        discovered: dict[str, dict[str, Any]] = {}
        for server_name in names:
            try:
                raw_tools = await self._run_letta_json_value(
                    [
                        "mcp",
                        "tools",
                        server_name,
                        "--full",
                        "--agent",
                        self.config.agent_id,
                    ],
                    timeout=10.0,
                )
            except Exception as exc:
                LOGGER.warning(
                    "MCP tool discovery failed server=%s: %s", server_name, exc
                )
                continue
            if not isinstance(raw_tools, list):
                continue
            for item in raw_tools:
                spec = self._mcp_tool_spec(item, server_name)
                if spec is not None:
                    discovered[spec["name"]] = spec

        tools = list(discovered.values())
        limit = max(0, self.config.mcp_native_max_tools)
        if limit:
            tools = tools[:limit]
        else:
            tools = []
        self._mcp_tools = tools
        self._mcp_tool_names = {tool["name"] for tool in tools}
        LOGGER.info(
            "discovered MCP native tools count=%s servers=%s",
            len(tools),
            ",".join(names),
        )

    async def _discover_mcp_tools_bridge(self) -> None:
        try:
            server_payload = await self.backend.mcp_list(
                agent_id=self.config.agent_id
            )
            tool_payload = await self.backend.mcp_tools(
                agent_id=self.config.agent_id
            )
        except Exception as exc:
            self._mcp_tools_error = str(exc)
            LOGGER.warning("MCP bridge discovery failed: %s", exc)
            return
        raw_servers = server_payload.get("servers")
        all_names = (
            [
                str(item.get("name"))
                for item in raw_servers
                if isinstance(item, dict) and item.get("name")
            ]
            if isinstance(raw_servers, list)
            else []
        )
        allowed = set(self.config.mcp_native_servers)
        names = [
            name
            for name in all_names
            if (not allowed or name in allowed)
            and name not in self._mcp_native_excluded
        ]
        self._mcp_native_active_servers = list(names)
        raw_tools = tool_payload.get("tools")
        if not isinstance(raw_tools, list):
            self._mcp_tools_error = "aki_mcp_tools returned unexpected payload"
            return
        tools: list[dict[str, Any]] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            parts = name.split("__")
            if len(parts) < 3 or parts[0] != "mcp":
                continue
            server_name = parts[1]
            if server_name not in names:
                continue
            spec = self._mcp_tool_spec(item, server_name)
            if spec is not None:
                tools.append(spec)
        limit = max(0, self.config.mcp_native_max_tools)
        self._mcp_tools = tools[:limit] if limit else []
        self._mcp_tool_names = {tool["name"] for tool in self._mcp_tools}
        LOGGER.info(
            "discovered MCP native tools via bridge count=%s servers=%s",
            len(self._mcp_tools),
            ",".join(names),
        )

    def _mcp_tool_spec(
        self, item: Any, server_name: str
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        if not name.startswith(MCP_TOOL_PREFIX) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", name
        ):
            return None
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        description = str(item.get("description") or "").strip()
        if not description:
            description = f"MCP tool {name} from {server_name}."
        return {
            "name": name,
            "label": name,
            "description": description,
            "parameters": schema,
            "auto_background": False,
            "timeout_ms": max(1000, int(self.config.mcp_tool_timeout * 1000)),
            "mcp_server": server_name,
        }

    async def _register_external_tools(self) -> None:
        tools = self._external_tools()
        if not tools:
            self._external_tools_error = "no external tools available"
            return
        for attempt in range(3):
            try:
                payload = await self.backend.control_request(
                    "register_external_tools",
                    {"tools": tools},
                    timeout=10.0,
                )
                registered = int(payload.get("registered", 0))
                self._external_tools_registered = registered > 0
                self._external_tools_error = None
                LOGGER.info(
                    "registered external tools=%s count=%s",
                    ",".join(tool["name"] for tool in tools),
                    registered,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._external_tools_error = str(exc)
                if attempt == 2:
                    LOGGER.warning("external tool registration failed: %s", exc)
                    return
                await asyncio.sleep(1.0)

    async def _on_backend_exit(self, returncode: int | None, detail: str | None) -> None:
        self._reset_backend_queue_state()
        self._external_tools_registered = False
        self._external_tools_error = "backend exited"
        self._external_tools_pending = False
        self._external_tools_ready.set()
        turn = self.active
        if turn is not None and not turn.end_sent:
            await self._emit(
                turn,
                "error",
                {
                    "code": "BACKEND_UNAVAILABLE",
                    "message": f"Letta backend exited with code {returncode}",
                },
            )
            await self._finish_turn(turn, "backend_error", {"returncode": returncode})

    def _reset_backend_queue_state(self) -> None:
        self._backend_queue.clear()
        self._backend_current_batch = None

    def _track_backend_queue_item(self, message: Mapping[str, Any]) -> None:
        item_id = str(message.get("item_id") or "")
        if not item_id:
            return
        self._backend_queue.append(
            {
                "id": item_id,
                "client_message_id": str(message.get("client_message_id") or ""),
                "source": str(message.get("source") or ""),
                "kind": str(message.get("kind") or ""),
            }
        )

    def _track_backend_queue_batch(self, message: Mapping[str, Any]) -> None:
        raw_ids = message.get("item_ids")
        item_ids = (
            [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        )
        try:
            merged_count = max(0, int(message.get("merged_count") or 0))
        except (TypeError, ValueError):
            merged_count = 0

        observed: list[dict[str, Any]] = []
        if item_ids:
            wanted = set(item_ids)
            remaining: deque[dict[str, Any]] = deque()
            for item in self._backend_queue:
                if item["id"] in wanted:
                    observed.append(item)
                    wanted.discard(item["id"])
                else:
                    remaining.append(item)
            self._backend_queue = remaining
        if not observed and merged_count:
            for _ in range(min(merged_count, len(self._backend_queue))):
                observed.append(self._backend_queue.popleft())

        missing = max(0, merged_count - len(observed))
        if not observed and missing == 0:
            self._backend_current_batch = None
            return
        # If the batch references items we never saw enqueued, assume it may
        # contain a user message so a legitimate turn result is never dropped.
        has_user = bool(missing) or any(
            item.get("source") == "user" for item in observed
        )
        self._backend_current_batch = {
            "batch_id": str(message.get("batch_id") or ""),
            "items": observed,
            "has_user": has_user,
            "merged_count": merged_count,
            "missing": missing,
        }
        LOGGER.debug(
            "backend queue batch id=%s merged=%s observed=%s missing=%s has_user=%s",
            self._backend_current_batch["batch_id"],
            merged_count,
            len(observed),
            missing,
            has_user,
        )

    def _backend_batch_is_internal(self) -> bool:
        batch = self._backend_current_batch
        return batch is not None and not bool(batch.get("has_user"))

    async def _on_backend_message(self, message: dict[str, Any]) -> None:
        if message.get("type") == "stream_event" and isinstance(message.get("event"), dict):
            message = message["event"]
        message_type = message.get("type")

        if message_type == "queue_item_enqueued":
            self._track_backend_queue_item(message)
            return
        if message_type == "queue_batch_dequeued":
            self._track_backend_queue_batch(message)
            return
        if message_type == "queue_cleared":
            self._reset_backend_queue_state()
            return

        if message_type == "control_request":
            request = message.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                task = asyncio.create_task(self._handle_tool_request(message))
                self._approval_tasks.add(task)
                task.add_done_callback(self._approval_tasks.discard)
            elif request.get("subtype") == "execute_external_tool":
                task = asyncio.create_task(self._handle_external_tool(message))
                self._external_tool_tasks.add(task)
                task.add_done_callback(self._external_tool_tasks.discard)
            return

        turn = self.active
        if message_type == "result":
            batch = self._backend_current_batch
            self._backend_current_batch = None
            if batch is not None and not bool(batch.get("has_user")):
                LOGGER.info(
                    "ignoring backend result for internal batch id=%s items=%s",
                    batch.get("batch_id"),
                    ",".join(
                        str(item.get("kind") or item.get("source") or "?")
                        for item in batch.get("items", [])
                    ),
                )
                return
            if turn is None or turn.end_sent:
                return
            turn.final_result = dict(message)
            reason = "cancelled" if message.get("stop_reason") == "cancelled" else "completed"
            if message.get("subtype") == "error" and reason != "cancelled":
                reason = "error"
            # Never await end-of-turn bridge cleanup from the backend read
            # loop: end_aki_turn needs that same loop to receive its response.
            task = asyncio.create_task(
                self._finish_turn(turn, reason),
                name=f"finish-turn:{turn.turn_id}",
            )
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)
            return

        if turn is None or turn.end_sent:
            return
        if self._backend_batch_is_internal():
            # Task notifications and cron prompts run as backend-internal
            # turns. Their stream output must not be attributed to the user
            # turn waiting behind them in the queue.
            return
        inner_type = message.get("message_type") or message_type

        if inner_type == "assistant_message":
            turn.pending_tool_results = 0
            text = _content_text(message.get("content"))
            if text:
                turn.assistant_parts.append(text)
                await self._emit(turn, "assistant_delta", {"text": text})
            return
        if inner_type == "reasoning_message":
            turn.pending_tool_results = 0
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                turn.reasoning_parts.append(reasoning)
                if turn.include_reasoning:
                    await self._emit(
                        turn, "assistant_delta", {"text": reasoning, "reasoning": True}
                    )
            return
        if inner_type in ("tool_call_message", "approval_request_message"):
            turn.pending_tool_results = 0
            call = message.get("tool_call") or {}
            if isinstance(call, dict):
                tool_call_id = str(call.get("tool_call_id") or call.get("id") or "")
                name = str(call.get("name") or "unknown")
                await self._emit_tool_call_once(
                    turn, tool_call_id, name, call.get("arguments")
                )
            return
        if inner_type == "tool_return_message":
            turn.tool_result_events += 1
            turn.pending_tool_results += 1
            await self._emit_tool_result_once(
                turn,
                str(message.get("tool_call_id") or ""),
                {
                    "tool_call_id": message.get("tool_call_id"),
                    "status": message.get("status"),
                    "tool_return": message.get("tool_return"),
                    "stdout": message.get("stdout"),
                    "stderr": message.get("stderr"),
                },
            )
            return
        if inner_type == "usage_statistics":
            usage = {
                key: message.get(key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "context_tokens",
                )
                if message.get(key) is not None
            }
            self.last_usage = usage
            await self._emit(turn, "usage", usage)
            self._schedule_status_refresh()
            return
        if inner_type == "event_message" and message.get("event_type") == "compaction":
            await self._emit(
                turn, "compaction", {"trigger": (message.get("event_data") or {}).get("trigger")}
            )
            return
        if inner_type == "summary_message":
            await self._emit(
                turn,
                "compaction",
                {
                    "summary": message.get("summary"),
                    "stats": message.get("compaction_stats"),
                },
            )
            return
        if message_type == "error" or inner_type == "error_message":
            await self._emit(
                turn,
                "error",
                {
                    "message": message.get("message") or message.get("error"),
                    "stop_reason": message.get("stop_reason"),
                },
            )
            return

    async def _handle_tool_request(self, message: dict[str, Any]) -> None:
        turn = self.active
        request_id = str(message.get("request_id") or "")
        request = message.get("request") or {}
        if turn is None or turn.end_sent or not request_id:
            if request_id:
                await self._safe_deny(request_id, "no active turn")
            return
        tool_name = str(request.get("tool_name") or "unknown")
        tool_call_id = str(request.get("tool_call_id") or request_id)
        tool_input = request.get("input")

        if self._backend_batch_is_internal():
            if tool_is_safe(tool_name) or (
                tool_name == "Bash"
                and self.config.sandbox_enabled
                and self._sandbox_blocked_reason is None
            ):
                await self._safe_allow(request_id)
            else:
                await self._safe_deny(request_id, "internal backend turn")
            return

        if tool_name == LOCK_RELEASE_TOOL:
            await self._safe_deny(
                request_id,
                "EXECUTION_LOCK_RELEASE_UNAVAILABLE: manual lock release is not "
                "available. The gateway releases the lock automatically when the "
                "turn ends; do not retry.",
            )
            return

        allowed, _owner = authorize_exclusive_tool(self.config.profile, tool_name)
        if not allowed:
            await self._safe_deny(request_id, ownership_denied_message(tool_name))
            return

        await self._emit_tool_call_once(turn, tool_call_id, tool_name, tool_input)

        if (
            requires_execution_lock(tool_name)
            and not turn.lock_held
            and tool_name not in EXTERNAL_TOOL_NAMES
            and not tool_name.startswith(MCP_TOOL_PREFIX)
        ):
            await self._safe_deny(request_id, LOCK_REQUIRED_ERROR)
            return

        if (
            tool_is_safe(tool_name)
            or tool_name in EXTERNAL_TOOL_NAMES
            or (
                tool_name == "Bash"
                and self.config.sandbox_enabled
                and self._sandbox_blocked_reason is None
            )
        ):
            await self._safe_allow(request_id)
            return
        allow, reason = await self._request_approval(
            turn,
            approval_id=request_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_input=tool_input,
            diffs=request.get("diffs"),
        )
        try:
            await self.backend.approve_tool(request_id, allow=allow, message=reason)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deliver approval decision tool=%s", tool_name)

    async def _request_approval(
        self,
        turn: Turn,
        *,
        approval_id: str,
        tool_name: str,
        tool_call_id: str,
        tool_input: Any,
        diffs: Any = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        if turn.end_sent:
            return False, "turn ended"
        if turn.conn.closed:
            return False, "initiating client disconnected"
        future: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        approval = Approval(
            approval_id=approval_id,
            turn=turn,
            future=future,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self.approvals[approval_id] = approval
        self._schedule_status_refresh()
        data: dict[str, Any] = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "input": tool_input,
            "diffs": diffs,
            "timeout_s": self.config.approval_timeout,
        }
        if turn.origin == "mqtt" and not turn.origin_is_agent:
            data["requester_id"] = turn.origin_sender_id
        if extra:
            data.update(extra)
        await self._emit(turn, "approval_request", data)
        self.operation_journal.approval_started()
        LOGGER.info(
            "approval requested id=%s tool=%s turn=%s origin=%s from_agent=%s",
            approval_id,
            tool_name,
            turn.turn_id,
            turn.origin,
            turn.origin_is_agent,
        )
        timed_out = False
        try:
            allow, reason = await asyncio.wait_for(
                future, timeout=self.config.approval_timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            allow, reason = False, f"approval timed out after {self.config.approval_timeout:g}s"
            LOGGER.info("approval timeout turn=%s tool=%s", turn.turn_id, tool_name)
        except asyncio.CancelledError:
            allow, reason = False, "turn cancelled"
        finally:
            self.operation_journal.approval_finished()
            self.approvals.pop(approval_id, None)
            self._schedule_status_refresh()
        decision = "timeout" if timed_out else ("allow" if allow else "deny")
        await self._emit(
            turn,
            "approval_resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "message": reason,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            },
        )
        return allow, reason

    async def _safe_allow(self, request_id: str) -> None:
        try:
            await self.backend.approve_tool(request_id, allow=True)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to auto-allow tool")

    async def _safe_deny(self, request_id: str, reason: str) -> None:
        try:
            await self.backend.approve_tool(request_id, allow=False, message=reason)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deny tool")

    async def _handle_external_tool(self, message: dict[str, Any]) -> None:
        request = message.get("request") or {}
        tool_call_id = str(
            request.get("tool_call_id") or message.get("request_id") or ""
        )
        tool_name = str(request.get("tool_name") or "")
        tool_input = request.get("input") or {}
        if not tool_call_id:
            LOGGER.warning("external tool request missing tool_call_id")
            return
        turn = self.active
        if turn is None or turn.end_sent:
            await self._send_external_result(
                tool_call_id, "external tool requires an active turn", True
            )
            return
        if not isinstance(tool_input, dict):
            await self._send_external_result(
                tool_call_id, "external tool input must be an object", True
            )
            return
        if self._backend_batch_is_internal():
            await self._send_external_result(
                tool_call_id, "external tool is not available in an internal backend turn", True
            )
            return
        await self._emit_tool_call_once(turn, tool_call_id, tool_name, tool_input)
        allowed, _owner = authorize_exclusive_tool(self.config.profile, tool_name)
        if not allowed:
            await self._send_external_result(
                tool_call_id, ownership_denied_message(tool_name), True
            )
            return
        if tool_name == IMAGE_GENERATION_TOOL_NAME:
            await self._handle_image_generation_tool(
                turn, tool_call_id, tool_name, tool_input
            )
            return
        if is_exclusive_tool(tool_name):
            await self._handle_exclusive_tool(
                turn, tool_call_id, tool_name, tool_input
            )
            return
        if tool_name == WAIT_OTHER_TOOL:
            await self._handle_wait_other_tool(turn, tool_call_id)
            return
        if tool_name == WAIT_USER_TOOL:
            await self._handle_wait_user_tool(turn, tool_call_id, tool_input)
            return
        if is_lock_control_tool(tool_name):
            await self._handle_execution_lock_tool(turn, tool_call_id, tool_name)
            return
        if requires_execution_lock(tool_name) and not turn.lock_held:
            await self._send_external_result(tool_call_id, LOCK_REQUIRED_ERROR, True)
            return
        if tool_name.startswith(MCP_TOOL_PREFIX):
            await self._handle_mcp_tool(turn, tool_call_id, tool_name, tool_input)
            return
        if tool_name not in (
            TOKIFACTOR_IMAGE_TOOL_NAME,
            CAMERA_TOOL_NAME,
            SCREENSHOT_TOOL_NAME,
            IMAGE_GENERATION_TOOL_NAME,
        ):
            text = f"unknown external tool: {tool_name}"
            await self._send_external_result(tool_call_id, text, True)
            return
        if tool_name == TOKIFACTOR_IMAGE_TOOL_NAME:
            await self._handle_tokifactor_image_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        elif tool_name == SCREENSHOT_TOOL_NAME:
            await self._handle_screenshot_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        elif tool_name == IMAGE_GENERATION_TOOL_NAME:
            await self._handle_image_generation_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        else:
            await self._handle_camera_tool(turn, tool_call_id, tool_name, tool_input)

    async def _handle_exclusive_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        try:
            if tool_name.startswith("aki_schedule_"):
                result = await self._run_schedule_tool(tool_name, tool_input)
            elif tool_name.startswith("aki_ledger_"):
                result = await self._run_ledger_tool(tool_name, tool_input)
            elif tool_name == "aki_zero_reply_budget":
                result = await self._run_budget_tool(tool_input)
            else:
                raise ExclusiveToolError(f"unknown exclusive tool: {tool_name}")
        except (ScheduleError, LedgerError, ExclusiveToolError) as exc:
            await self._external_tool_failure(turn, tool_call_id, str(exc))
            return
        except Exception as exc:
            LOGGER.exception("exclusive tool failed tool=%s", tool_name)
            await self._external_tool_failure(
                turn, tool_call_id, f"{tool_name} failed: {exc}"
            )
            return
        await self._external_tool_success(
            turn,
            tool_call_id,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        )

    async def _run_schedule_tool(
        self, tool_name: str, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "aki_schedule_create":
            record = await asyncio.to_thread(
                self.schedule_store.create, tool_input
            )
            return {"ok": True, "schedule": record.to_dict()}
        if tool_name == "aki_schedule_update":
            record = await asyncio.to_thread(
                self.schedule_store.update, tool_input
            )
            return {"ok": True, "schedule": record.to_dict()}
        if tool_name == "aki_schedule_delete":
            record = await asyncio.to_thread(
                self.schedule_store.delete, tool_input.get("id")
            )
            return {"ok": True, "cancelled": record.to_dict()}
        if tool_name == "aki_schedule_list":
            records = await asyncio.to_thread(
                self.schedule_store.list,
                status=str(tool_input.get("status") or "active"),
                start=tool_input.get("from"),
                end=tool_input.get("to"),
            )
            return {
                "ok": True,
                "schedules": [record.to_dict() for record in records],
            }
        raise ExclusiveToolError(f"unknown schedule tool: {tool_name}")

    async def _run_ledger_tool(
        self, tool_name: str, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "aki_ledger_add":
            result = await asyncio.to_thread(
                self.life_ledger.add, tool_input.get("text")
            )
            return {"ok": True, **result}
        if tool_name == "aki_ledger_delete":
            record = await asyncio.to_thread(
                self.life_ledger.delete, tool_input.get("id")
            )
            return {"ok": True, "deleted": record.to_dict()}
        if tool_name == "aki_ledger_list":
            records = await asyncio.to_thread(self.life_ledger.records)
            return {
                "ok": True,
                "records": [record.to_dict() for record in records],
                "bytes": self.life_ledger.snapshot()["bytes"],
            }
        raise ExclusiveToolError(f"unknown ledger tool: {tool_name}")

    async def _run_budget_tool(
        self, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        target = str(tool_input.get("target") or "").strip()
        if not target:
            raise ExclusiveToolError("BUDGET_TARGET_UNKNOWN: target is empty")
        reason = str(tool_input.get("reason") or "").strip()
        profile, config_path, socket_path = await asyncio.to_thread(
            self._resolve_budget_target, target
        )
        if not socket_path.is_socket():
            raise ExclusiveToolError(
                f"BUDGET_TARGET_OFFLINE: {profile} is not running"
            )

        def call_target() -> dict[str, Any]:
            from engine.client import GatewayClient

            with GatewayClient(
                socket_path,
                client_name="akid-aki",
                client_version=SERVER_VERSION,
            ) as client:
                client.handshake()
                result = client.call(
                    "aki.admin.zero_initiation_budget",
                    {"source_profile": self.config.profile, "reason": reason},
                    timeout=10.0,
                )
                return result if isinstance(result, dict) else {}

        try:
            result = await asyncio.to_thread(call_target)
        except Exception as exc:
            raise ExclusiveToolError(
                f"BUDGET_TARGET_OFFLINE: cannot reach {profile}: {exc}"
            ) from exc
        return {
            "ok": True,
            "target": profile,
            "target_config": str(config_path),
            **result,
        }

    def _resolve_budget_target(self, target: str) -> tuple[str, Path, Path]:
        wanted = target.casefold()
        matches: list[tuple[int, str, Path, Path]] = []
        for profile, config_path in discover_profiles().items():
            if profile == self.config.profile:
                continue
            try:
                conf = load_config(config_path, profile=profile)
            except (OSError, ValueError):
                continue
            labels = {
                profile.casefold(),
                conf.channel.name.casefold(),
                *{alias.casefold() for alias in conf.channel.aliases},
            }
            if wanted not in labels:
                continue
            socket_path = self._profile_socket(profile, conf.socket)
            score = 0 if wanted == profile.casefold() else 1
            matches.append((score, profile, config_path, socket_path))
        if not matches:
            if wanted in {"aki", self.config.channel_name.casefold(), *{
                alias.casefold() for alias in self.config.channel_aliases
            }}:
                raise ExclusiveToolError(
                    "BUDGET_SELF_FORBIDDEN: Aki cannot zero her own reply budget"
                )
            raise ExclusiveToolError(
                f"BUDGET_TARGET_UNKNOWN: no other member matches {target!r}"
            )
        _score, profile, config_path, socket_path = sorted(matches)[0]
        return profile, config_path, socket_path

    @staticmethod
    def _profile_socket(profile: str, configured: str) -> Path:
        if configured:
            return Path(configured).expanduser()
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        )
        filename = "akid.sock" if profile == "aki" else f"{profile}.sock"
        return runtime / "aki" / filename

    def _wait_targets_for_turn(self, turn: Turn) -> list[tuple[str, str, str]]:
        if turn.wait_guard_targets:
            return list(turn.wait_guard_targets)
        if turn.origin != "mqtt" or self._solo_session is not None:
            return []
        targets: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for label in self.config.channel_wait_for:
            resolved = self._resolve_label_member_id(label) or self._history_member_id(
                label, label
            )
            if not resolved:
                targets.append(("", label, label))
                continue
            if resolved == self._channel_self_key() or resolved in seen:
                continue
            member = self._channel_members.get(resolved)
            if member is not None and normalize_member_kind(member.get("kind")) != MEMBER_KIND_AGENT:
                continue
            if turn.channel_participants and resolved not in turn.channel_participants:
                continue
            seen.add(resolved)
            display = self._member_display_name(resolved, label) or label
            targets.append((resolved, display, label))
        return targets

    def _unfinished_wait_targets(
        self, turn: Turn
    ) -> list[tuple[str, str, str]]:
        unfinished: list[tuple[str, str, str]] = []
        for member_id, name, label in self._wait_targets_for_turn(turn):
            if not member_id:
                continue
            state = self._agent_channel_state(member_id)
            if state.active or state.queue_depth > 0:
                unfinished.append((member_id, name, label))
        return unfinished

    @staticmethod
    def _wait_guard_notice(targets: list[tuple[str, str, str]]) -> str:
        names = ", ".join(
            dict.fromkeys(name or label for _member_id, name, label in targets)
        )
        return (
            "[akid wait guard]\n"
            "这是系统提示，不是用户消息：你刚才没有等待应该等待的目标，"
            f"这些目标是:{names}。请先调用 `wait_other` 等待这些目标完成，"
            "再给出最终回复。不要解释或提及这条系统提示。"
        )

    def _enqueue_wait_guard_turn(
        self, turn: Turn, targets: list[tuple[str, str, str]]
    ) -> None:
        guard_turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.channel_conn,
            message=self._wait_guard_notice(targets),
            include_reasoning=False,
            client_message_id=turn.client_message_id,
            origin=WAIT_GUARD_ORIGIN,
            origin_sender_id=turn.origin_sender_id,
            origin_is_agent=False,
            lock_authorized=False,
            lock_authorization="wait-guard",
            channel_participants=turn.channel_participants,
            channel_sender=turn.channel_sender,
            wait_guard_targets=tuple(targets),
            wait_guard_attempt=turn.wait_guard_attempt + 1,
        )
        if len(self.queue) >= self.config.queue_max:
            LOGGER.warning(
                "wait guard queue exceeds max; enqueueing correction turn=%s",
                guard_turn.turn_id,
            )
        guard_turn.state = "queued"
        self.queue.append(guard_turn)
        self._schedule_status_refresh()
        LOGGER.info(
            "wait guard blocked final turn=%s correction=%s targets=%s attempt=%s",
            turn.turn_id,
            guard_turn.turn_id,
            ",".join(item[1] or item[2] for item in targets),
            guard_turn.wait_guard_attempt,
        )

    @staticmethod
    def _wait_result_from_state(
        member_id: str,
        name: str,
        state: AgentChannelState,
        *,
        fallback_status: str = "completed",
    ) -> WaitOtherResult:
        status = fallback_status
        if state.last_completion_phase == "offline":
            status = "offline"
        elif state.last_completion_phase == "no_reply":
            status = "no_reply"
        elif state.last_completion_phase == "ended" and state.last_final_text:
            status = "completed"
        return WaitOtherResult(
            member_id=member_id,
            name=name,
            status=status,
            text=state.last_final_text,
            turn_id=state.last_final_turn_id,
        )

    async def _wait_for_agent(
        self, member_id: str, name: str, turn: Turn
    ) -> WaitOtherResult:
        if not member_id:
            return WaitOtherResult(member_id="", name=name, status="unknown")
        state = self._agent_channel_state(member_id)
        was_discovered = member_id in self._channel_members
        started_at = asyncio.get_running_loop().time()
        while member_id not in self._channel_members and not state.active:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if asyncio.get_running_loop().time() - started_at >= WAIT_OTHER_START_GRACE:
                return WaitOtherResult(member_id=member_id, name=name, status="offline")
            await asyncio.sleep(WAIT_OTHER_POLL_INTERVAL)
            state = self._agent_channel_state(member_id)
            was_discovered = was_discovered or member_id in self._channel_members
        was_discovered = was_discovered or member_id in self._channel_members

        baseline = state.last_completion_seq

        while not state.active and state.queue_depth <= 0:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if member_id not in self._channel_members:
                return WaitOtherResult(member_id=member_id, name=name, status="offline")
            state = self._agent_channel_state(member_id)
            if state.last_completion_seq > baseline:
                return self._wait_result_from_state(member_id, name, state)
            if asyncio.get_running_loop().time() - started_at >= WAIT_OTHER_START_GRACE:
                return self._wait_result_from_state(
                    member_id, name, state, fallback_status="idle"
                )
            await asyncio.sleep(WAIT_OTHER_POLL_INTERVAL)

        while True:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if member_id not in self._channel_members:
                if was_discovered:
                    return WaitOtherResult(
                        member_id=member_id, name=name, status="offline"
                    )
                last_event = state.last_turn_event_sent_at
                stale_after_ms = int(CHANNEL_MEMBER_TIMEOUT * 1000)
                if (
                    not state.active
                    or last_event <= 0
                    or int(time.time() * 1000) - last_event > stale_after_ms
                ):
                    return WaitOtherResult(
                        member_id=member_id, name=name, status="offline"
                    )
            state = self._agent_channel_state(member_id)
            was_discovered = was_discovered or member_id in self._channel_members
            if state.last_completion_seq > baseline:
                return self._wait_result_from_state(member_id, name, state)
            if not state.active and state.queue_depth <= 0:
                return self._wait_result_from_state(member_id, name, state)
            await asyncio.sleep(WAIT_OTHER_POLL_INTERVAL)

    @staticmethod
    def _format_wait_other_results(results: list[WaitOtherResult]) -> str:
        lines = ["WAIT_OTHER_RESULT"]
        total = 0
        for result in results:
            lines.append(f"[{result.name}] status={result.status}")
            if result.turn_id:
                lines.append(f"turn_id={result.turn_id}")
            if result.text:
                remaining = max(0, WAIT_OTHER_MAX_TOTAL_CHARS - total)
                limit = min(WAIT_OTHER_MAX_REPLY_CHARS, remaining)
                text = result.text[:limit]
                if len(text) < len(result.text):
                    text += "\n...[truncated by akid]"
                lines.append(text)
                total += len(text)
            lines.append("---")
        lines.append(
            "Use these earlier replies to continue the discussion; do not repeat them verbatim."
        )
        return "\n".join(lines)

    @staticmethod
    def _pending_wait_user_messages(turn: Turn) -> list[dict[str, Any]]:
        sender_id = turn.origin_sender_id
        messages: list[dict[str, Any]] = []
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)):
                continue
            if str(item.get("sender_id") or "") != sender_id:
                continue
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq <= 0 or seq not in turn.wait_user_eligible_seqs:
                continue
            if seq > 0 and seq in turn.waited_user_seqs:
                continue
            messages.append(item)
        messages.sort(
            key=lambda item: (
                int(item.get("sent_at") or 0),
                int(item.get("seq") or 0),
            )
        )
        return messages

    @staticmethod
    def _consume_wait_user_messages(
        turn: Turn, messages: list[dict[str, Any]]
    ) -> None:
        for item in messages:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                turn.waited_user_seqs.add(seq)
                GatewayServer._remove_turn_overlay_file(turn, seq)
            item["waited_by_user"] = True

    def _format_wait_user_results(
        self, turn: Turn, messages: list[dict[str, Any]]
    ) -> str:
        lines = [f"WAIT_USER_RESULT status=messages count={len(messages)}"]
        total = 0
        for item in messages:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            sent_at = int(item.get("sent_at") or 0)
            sender = str(item.get("sender") or turn.channel_sender or "user")
            lines.append(
                f"[seq={seq} at={_format_channel_message_time(sent_at)} "
                f"sender={sender}]"
            )
            text = str(item.get("text") or "").strip()
            remaining = max(0, WAIT_USER_MAX_TOTAL_CHARS - total)
            limit = min(WAIT_USER_MAX_REPLY_CHARS, remaining)
            if text:
                rendered = text[:limit]
                if len(rendered) < len(text):
                    rendered += "\n...[truncated by akid]"
                lines.append(rendered)
                total += len(rendered)
            attachments = item.get("attachments") or ()
            if attachments:
                payload = [
                    {
                        "name": attachment.name,
                        "path": attachment.path,
                        **({"missing": True} if attachment.missing else {}),
                    }
                    for attachment in attachments
                ]
                lines.append(
                    "attachments="
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
            lines.append("---")
        lines.append(
            "Treat these messages as the user's continuation. Do not mention "
            "the wait or this tool in your reply."
        )
        return "\n".join(lines)

    async def _handle_wait_user_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if turn.origin != "mqtt" or turn.origin_is_agent:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_UNAVAILABLE: only human-triggered channel turns may "
                "wait for the user. Do not retry.",
            )
            return
        if not turn.origin_sender_id:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_UNAVAILABLE: the current turn has no human sender. "
                "Do not retry.",
            )
            return
        raw_timeout = tool_input.get("timeout_s")
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_INVALID_TIMEOUT: timeout_s must be an integer from "
                f"{WAIT_USER_MIN_TIMEOUT_S} to {WAIT_USER_MAX_TIMEOUT_S}.",
            )
            return
        timeout_s = int(raw_timeout)
        if not WAIT_USER_MIN_TIMEOUT_S <= timeout_s <= WAIT_USER_MAX_TIMEOUT_S:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_INVALID_TIMEOUT: timeout_s must be an integer from "
                f"{WAIT_USER_MIN_TIMEOUT_S} to {WAIT_USER_MAX_TIMEOUT_S}.",
            )
            return

        if not turn.wait_user_used:
            turn.wait_user_used = True
        turn.wait_user_call_active = True
        try:
            await self._set_wait_user_active(True)
            await self._wait_for_user_messages(turn, tool_call_id, timeout_s)
        finally:
            turn.wait_user_call_active = False
            with contextlib.suppress(Exception):
                await self._set_wait_user_active(False)

    async def _wait_for_user_messages(
        self, turn: Turn, tool_call_id: str, timeout_s: int
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    "WAIT_USER_CANCELLED: the turn ended before another user "
                    "message arrived. Do not mention this tool or the wait.",
                )
                return
            pending = self._pending_wait_user_messages(turn)
            if pending:
                self._consume_wait_user_messages(turn, pending)
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    self._format_wait_user_results(turn, pending),
                )
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    f"WAIT_USER_TIMEOUT: no new message from "
                    f"{turn.channel_sender or 'the user'} arrived within "
                    f"{timeout_s}s. Continue with the current request. Do not "
                    "mention the wait or this tool.",
                )
                return
            turn.user_message_event.clear()
            pending = self._pending_wait_user_messages(turn)
            if pending:
                self._consume_wait_user_messages(turn, pending)
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    self._format_wait_user_results(turn, pending),
                )
                return
            try:
                await asyncio.wait_for(
                    turn.user_message_event.wait(), timeout=remaining
                )
            except asyncio.TimeoutError:
                continue

    async def _handle_wait_other_tool(
        self, turn: Turn, tool_call_id: str
    ) -> None:
        targets = self._wait_targets_for_turn(turn)
        if not targets:
            await self._send_external_result(
                tool_call_id,
                "WAIT_OTHER_SKIPPED: no earlier agents are configured for this turn.",
                False,
            )
            return
        labels = tuple(name or label for _member_id, name, label in targets)
        await self._set_wait_other_active(True, labels)
        turn.waited_agent_ids.update(
            member_id for member_id, _name, _label in targets if member_id
        )
        try:
            results = await asyncio.gather(
                *(
                    self._wait_for_agent(member_id, name, turn)
                    for member_id, name, _label in targets
                )
            )
            await self._send_external_result(
                tool_call_id,
                self._format_wait_other_results(list(results)),
                False,
            )
        finally:
            await self._set_wait_other_active(False)

    async def _handle_execution_lock_tool(
        self, turn: Turn, tool_call_id: str, tool_name: str
    ) -> None:
        if tool_name == LOCK_ACQUIRE_TOOL:
            if not turn.lock_authorized:
                await self._send_external_result(
                    tool_call_id, LOCK_NOT_AUTHORIZED_ERROR, True
                )
                return
            if self.execution_lock.held_by(
                profile=self.config.profile, turn_id=turn.turn_id
            ):
                turn.lock_held = True
                await self._send_external_result(
                    tool_call_id,
                    "EXECUTION_LOCK_ACQUIRED: this turn already holds the lock.",
                    False,
                )
                return
            try:
                acquired, holder = self.execution_lock.acquire(
                    profile=self.config.profile,
                    agent_id=self.config.agent_id,
                    turn_id=turn.turn_id,
                )
            except OSError as exc:
                self.execution_lock.last_error = str(exc)
                await self._send_external_result(
                    tool_call_id,
                    f"EXECUTION_LOCK_UNAVAILABLE: {exc}. Do not retry; end this turn.",
                    True,
                )
                return
            if acquired:
                turn.lock_held = True
                await self._send_external_result(
                    tool_call_id,
                    "EXECUTION_LOCK_ACQUIRED: device-wide non-read-only execution "
                    "is now exclusive to this turn.",
                    False,
                )
                return
            held_by = str(holder.get("profile") or "another profile")
            held_turn = str(holder.get("turn_id") or "unknown turn")
            await self._send_external_result(
                tool_call_id,
                f"{LOCK_BUSY_ERROR} holder={held_by} turn={held_turn}",
                True,
            )
            return

        released = self.execution_lock.release(
            profile=self.config.profile, turn_id=turn.turn_id
        )
        turn.lock_held = False
        await self._send_external_result(
            tool_call_id,
            "EXECUTION_LOCK_RELEASED"
            if released
            else "EXECUTION_LOCK_NOT_HELD: this turn did not hold the lock.",
            False,
        )

    async def _external_tool_failure(
        self, turn: Turn, tool_call_id: str, text: str
    ) -> None:
        await self._emit_tool_result_once(
            turn,
            tool_call_id,
            {"tool_call_id": tool_call_id, "status": "error", "tool_return": text},
        )
        await self._send_external_result(tool_call_id, text, True)

    async def _external_tool_success(
        self, turn: Turn, tool_call_id: str, text: str
    ) -> None:
        await self._emit_tool_result_once(
            turn,
            tool_call_id,
            {"tool_call_id": tool_call_id, "status": "success", "tool_return": text},
        )
        await self._send_external_result(tool_call_id, text, False)

    async def _handle_mcp_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if tool_name not in self._mcp_tool_names:
            await self._external_tool_failure(
                turn, tool_call_id, f"unknown MCP tool: {tool_name}"
            )
            return
        try:
            text, is_error = await self._call_mcp_tool(tool_name, tool_input)
        except Exception as exc:
            LOGGER.warning("MCP tool call failed tool=%s error=%s", tool_name, exc)
            await self._external_tool_failure(
                turn, tool_call_id, f"MCP tool call failed: {exc}"
            )
            return
        if is_error:
            await self._external_tool_failure(turn, tool_call_id, text)
        else:
            await self._external_tool_success(turn, tool_call_id, text)

    async def _call_mcp_tool(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> tuple[str, bool]:
        if self._aki_bridge_available():
            payload = await self.backend.mcp_call(
                tool_name,
                arguments,
                agent_id=self.config.agent_id,
                timeout=self.config.mcp_tool_timeout,
            )
            result = payload.get("result")
            return _normalize_mcp_result(
                json.dumps(result if result is not None else {}, ensure_ascii=False)
            )
        payload = json.dumps(dict(arguments), ensure_ascii=False)
        command = [
            self.config.letta_bin,
            "mcp",
            "call",
            tool_name,
            "--args-file",
            "-",
            "--agent",
            self.config.agent_id,
        ]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")),
                timeout=self.config.mcp_tool_timeout,
            )
        except asyncio.TimeoutError as exc:
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()
            raise RuntimeError(
                f"MCP tool timed out after {self.config.mcp_tool_timeout:g}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"failed to start letta mcp: {exc}") from exc

        stdout_text = stdout.decode("utf-8", "replace")
        stderr_text = stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = (stderr_text or stdout_text.strip() or f"exit code {proc.returncode}")
            return _truncate_mcp_text(f"MCP tool failed: {detail}"), True
        return _normalize_mcp_result(stdout_text)

    async def _handle_tokifactor_image_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.channel is None:
            await self._external_tool_failure(
                turn, tool_call_id, "tokifactor image not sent: channel is disabled"
            )
            return
        if self.config.tokifactor_image_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={"external": True, "channel": "tokifactor"},
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"tokifactor image not sent: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_tokifactor_send_image(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"tokifactor image send failed: {exc}"
            )
            return
        text = (
            "sent tokifactor image "
            f"transfer_id={result['transfer_id']} bytes={result['bytes']} "
            f"chunks={result['chunks']} mime={result['mime']} topic={result['topic']}"
        )
        await self._external_tool_success(turn, tool_call_id, text)

    async def _handle_camera_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.config.camera_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={"external": True, "channel": "camera", "privacy": "camera"},
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"camera photo not taken: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_camera_capture(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"camera capture failed: {exc}"
            )
            return
        text = (
            "photo captured "
            f"path={result['path']} bytes={result['bytes']} "
            f"width={result['width']} height={result['height']} "
            f"device={result['device']} mime={result['mime']}"
        )
        await self._external_tool_success(turn, tool_call_id, text)

    async def _handle_screenshot_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.config.screenshot_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={
                    "external": True,
                    "channel": "desktop",
                    "privacy": "screenshot",
                },
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"screenshot not taken: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_screenshot_capture(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"screenshot capture failed: {exc}"
            )
            return
        text = (
            "screenshot captured "
            f"path={result['path']} bytes={result['bytes']} "
            f"width={result['width']} height={result['height']} "
            f"display={result['display']} backend={result['backend']} "
            f"mime={result['mime']} "
            f"cursor={str(result['include_cursor']).lower()}"
        )
        await self._external_tool_success(turn, tool_call_id, text)

    async def _handle_image_generation_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        image = self.config.image_generation
        if image is None or not image.available:
            await self._external_tool_failure(
                turn, tool_call_id, "image generation is not configured or available"
            )
            return
        if image.require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={
                    "external": True,
                    "channel": "image",
                    "provider": "minimax",
                },
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"image not generated: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_image_generation(tool_input)
        except asyncio.CancelledError:
            raise
        except (ImageGenerationError, ValueError) as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"image generation failed: {exc}"
            )
            return
        except Exception as exc:
            LOGGER.exception("image generation failed tool_call_id=%s", tool_call_id)
            await self._external_tool_failure(
                turn, tool_call_id, f"image generation failed: {exc}"
            )
            return
        images = result.get("images") if isinstance(result, dict) else None
        if not isinstance(images, list) or not images:
            await self._external_tool_failure(
                turn, tool_call_id, "image generation returned no images"
            )
            return
        lines = [
            "image generated "
            f"count={result['count']} model={result['model']} "
            f"aspect_ratio={result['aspect_ratio']}"
        ]
        for index, item in enumerate(images):
            lines.append(
                f"image[{index}] path={item['path']} bytes={item['bytes']} "
                f"mime={item['mime']}"
            )
        lines.append(
            "Use tokifactor_send_image with one path to share it in the channel."
        )
        await self._external_tool_success(turn, tool_call_id, "\n".join(lines))

    async def _execute_image_generation(
        self, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        image = self.config.image_generation
        if image is None or not image.available:
            raise ImageGenerationError("image generation is not configured or available")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        aspect_ratio = args.get("aspect_ratio")
        if aspect_ratio is None:
            aspect_ratio = image.aspect_ratio
        if not isinstance(aspect_ratio, str) or aspect_ratio not in ASPECT_RATIOS:
            raise ValueError("aspect_ratio is not supported")
        n = args.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_IMAGES:
            raise ValueError(f"n must be between 1 and {MAX_IMAGES}")
        optimizer = args.get("prompt_optimizer")
        if optimizer is None:
            optimizer = image.prompt_optimizer
        if not isinstance(optimizer, bool):
            raise ValueError("prompt_optimizer must be a boolean")
        output_dir = Path(image.output_dir).expanduser()
        result = await asyncio.to_thread(
            generate_minimax_images,
            base_url=image.base_url,
            endpoint=image.path,
            api_key=image.api_key,
            model=image.model,
            prompt=prompt,
            output_dir=output_dir,
            aspect_ratio=aspect_ratio,
            n=n,
            timeout=image.timeout,
            max_bytes=image.max_bytes,
            prompt_optimizer=optimizer,
            aigc_watermark=image.aigc_watermark,
            use_proxy=image.use_proxy,
            upstream_proxy=self.config.aki.upstream_proxy if self.config.aki else "",
        )
        return result

    def _camera_dimension(self, value: Any, default: int) -> int:
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("camera width/height must be integers")
        if value < MIN_CAMERA_DIMENSION or value > MAX_CAMERA_DIMENSION:
            raise ValueError(
                f"camera dimensions must be between {MIN_CAMERA_DIMENSION} and {MAX_CAMERA_DIMENSION}"
            )
        return value

    async def _probe_image_dimensions(
        self, path: Path, fallback_width: int, fallback_height: int
    ) -> tuple[int, int]:
        if not self.config.ffprobe_bin:
            return fallback_width, fallback_height
        ffprobe = shutil.which(self.config.ffprobe_bin)
        if not ffprobe:
            return fallback_width, fallback_height
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.CancelledError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            raise
        except (OSError, asyncio.TimeoutError):
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return fallback_width, fallback_height
        if proc.returncode != 0:
            return fallback_width, fallback_height
        parts = stdout.decode("utf-8", "replace").strip().split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
        return fallback_width, fallback_height

    async def _execute_camera_capture(self, args: Mapping[str, Any]) -> dict[str, Any]:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        if not ffmpeg:
            raise ValueError(f"ffmpeg is unavailable: {self.config.ffmpeg_bin}")
        device_value = args.get("device", self.config.camera_device)
        if not isinstance(device_value, str) or not device_value.strip():
            raise ValueError("device must be a non-empty string")
        try:
            device = Path(device_value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("camera device does not exist") from exc
        if not _is_video_device(device):
            raise ValueError("device must be a V4L2 /dev/videoN character device")
        width = self._camera_dimension(args.get("width"), self.config.camera_width)
        height = self._camera_dimension(args.get("height"), self.config.camera_height)
        output_dir = self.config.camera_output_dir or default_camera_dir()
        prepare_private_output_dir(output_dir, "camera")
        output = unique_capture_path(output_dir, "camera", ".jpg")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-input_format",
            "mjpeg",
            "-video_size",
            f"{width}x{height}",
            "-i",
            str(device),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ValueError(f"cannot start ffmpeg: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera capture timed out") from exc
        if proc.returncode != 0:
            with contextlib.suppress(OSError):
                output.unlink()
            detail = stderr.decode("utf-8", "replace").strip()[-500:]
            raise ValueError(f"ffmpeg failed: {detail or proc.returncode}")
        try:
            size = output.stat().st_size
        except OSError as exc:
            raise ValueError("camera did not produce an output file") from exc
        if size <= 0:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera produced an empty image")
        if size > MAX_CAMERA_OUTPUT_BYTES:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError(f"camera image exceeds {MAX_CAMERA_OUTPUT_BYTES} bytes")
        with contextlib.suppress(OSError):
            os.chmod(output, 0o600)
        data = await asyncio.to_thread(output.read_bytes)
        mime = detect_image_mime(data)
        if mime is None:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera output is not a supported image")
        actual_width, actual_height = await self._probe_image_dimensions(
            output, width, height
        )
        return {
            "path": str(output),
            "bytes": size,
            "width": actual_width,
            "height": actual_height,
            "device": str(device),
            "mime": mime,
        }

    def _png_dimensions(self, path: Path) -> tuple[int, int] | None:
        try:
            with path.open("rb") as handle:
                header = handle.read(24)
        except OSError:
            return None
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if header[12:16] != b"IHDR":
            return None
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0:
            return None
        return width, height

    async def _execute_screenshot_capture(
        self,
        args: Mapping[str, Any],
        *,
        output_path: Path | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        output_name = args.get("output")
        if output_name is not None:
            if not isinstance(output_name, str) or not output_name.strip():
                raise ValueError("output must be a non-empty string")
            output_name = output_name.strip()
            if len(output_name) > 128 or not all(
                char.isalnum() or char in "._-" for char in output_name
            ):
                raise ValueError("output contains unsupported characters")
        include_cursor = args.get("include_cursor", False)
        if not isinstance(include_cursor, bool):
            raise ValueError("include_cursor must be a boolean")
        backends = self._screenshot_backends()
        if output_name:
            backends = [backend for backend in backends if backend[0] == "grim"]
        if not backends:
            if output_name:
                raise ValueError("output selection requires the grim backend")
            raise ValueError("no supported screenshot backend found (spectacle/grim)")
        candidates = self._wayland_display_candidates()
        if not candidates:
            raise ValueError("no Wayland display socket found")
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        ).expanduser()
        if output_path is not None:
            output = output_path.expanduser()
            prepare_private_output_dir(output.parent, "screenshot")
        else:
            output_dir = (
                self.config.screenshot_output_dir or default_screenshot_dir()
            )
            prepare_private_output_dir(output_dir, "screenshot")
            output = unique_capture_path(output_dir, "screenshot", ".png")
        errors: list[str] = []
        for backend, executable in backends:
            for display in candidates:
                with contextlib.suppress(OSError):
                    output.unlink()
                if backend == "spectacle":
                    command = [executable, "-b", "-n", "-f"]
                    if include_cursor:
                        command.append("-p")
                    command.extend(["-o", str(output)])
                else:
                    command = [executable, "-t", "png"]
                    if include_cursor:
                        command.append("-c")
                    if output_name:
                        command.extend(["-o", output_name])
                    command.append(str(output))
                env = dict(os.environ)
                env["XDG_RUNTIME_DIR"] = str(runtime_dir)
                env["WAYLAND_DISPLAY"] = display
                env.setdefault("XDG_SESSION_TYPE", "wayland")
                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.CancelledError:
                    if proc is not None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await proc.wait()
                    raise
                except asyncio.TimeoutError:
                    if proc is not None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await proc.wait()
                    errors.append(f"{display}/{backend}: timed out")
                    continue
                except OSError as exc:
                    errors.append(f"{display}/{backend}: cannot start: {exc}")
                    continue
                detail = stderr.decode("utf-8", "replace").strip()[-300:]
                if proc.returncode != 0:
                    errors.append(
                        f"{display}/{backend}: "
                        + (detail or f"exited {proc.returncode}")
                    )
                    continue
                try:
                    size = output.stat().st_size
                except OSError:
                    errors.append(f"{display}/{backend}: produced no output")
                    continue
                if size <= 0:
                    errors.append(f"{display}/{backend}: produced an empty file")
                    continue
                if size > MAX_SCREENSHOT_OUTPUT_BYTES:
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(
                        f"{display}/{backend}: screenshot exceeds "
                        f"{MAX_SCREENSHOT_OUTPUT_BYTES} bytes"
                    )
                    continue
                dimensions = self._png_dimensions(output)
                if dimensions is None:
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(f"{display}/{backend}: output is not a valid PNG")
                    continue
                if backend == "spectacle" and await self._image_is_effectively_black(
                    output
                ):
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(f"{display}/{backend}: produced a black screenshot")
                    continue
                with contextlib.suppress(OSError):
                    os.chmod(output, 0o600)
                width, height = dimensions
                return {
                    "path": str(output),
                    "bytes": size,
                    "width": width,
                    "height": height,
                    "display": display,
                    "backend": backend,
                    "mime": "image/png",
                    "include_cursor": include_cursor,
                }
        with contextlib.suppress(OSError):
            output.unlink()
        detail = "; ".join(errors[-3:]) or "unknown error"
        raise ValueError(f"screenshot capture failed: {detail}")

    async def _image_is_effectively_black(self, path: Path) -> bool:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        if not ffmpeg:
            return False
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                "scale=1:1",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (OSError, asyncio.TimeoutError):
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            return False
        if proc.returncode != 0 or len(stdout) < 3:
            return False
        return all(value <= 2 for value in stdout[:3])

    async def _execute_tokifactor_send_image(self, args: Mapping[str, Any]) -> dict[str, Any]:
        channel = self.channel
        if channel is None:
            raise ValueError("tokifactor channel is disabled")
        path_value = args.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("path must be a non-empty string")
        caption_value = args.get("caption")
        if caption_value is not None and not isinstance(caption_value, str):
            raise ValueError("caption must be a string")
        caption = (caption_value or "").strip()
        if len(caption) > MAX_IMAGE_CAPTION_CHARS:
            raise ValueError(f"caption exceeds {MAX_IMAGE_CAPTION_CHARS} characters")

        raw = Path(path_value).expanduser()
        if not raw.is_absolute():
            raise ValueError("image path must be absolute")
        try:
            path = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError("image file does not exist") from exc
        if not path.is_file():
            raise ValueError("image path must be a file")
        roots = self.writable.external_source_roots()
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError(
                "image path must be under /tmp, ~/.local/share/tmp, or a registered writable directory"
            )
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("image file is empty")
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        data = await asyncio.to_thread(path.read_bytes)
        mime = detect_image_mime(data)
        if mime is None:
            raise ValueError("unsupported image format (png/jpeg/gif/webp only)")
        name = (caption or path.name)[:MAX_ATTACHMENT_NAME_CHARS]
        published = await channel.publish_message(
            "",
            [{"data": data, "mime": mime, "name": name}],
        )
        first = (published.get("images") or [{}])[0]
        message_id = str(published.get("message_id") or "")
        stored = self._store_channel_image(
            ImageEnvelope(
                sender=self.config.channel_name,
                data=data,
                sender_id=self._channel_self_key(),
                message_id=message_id,
                name=name,
                attachment_index=0,
                attachment_count=1,
                sent_at=int(time.time() * 1000),
                mime=mime,
                transfer_id=str(first.get("transfer_id") or ""),
            ),
            name=name,
            index=1,
        )
        if stored is not None:
            self._record_channel_message(
                sender=self.config.channel_name,
                sender_id=self._channel_self_key(),
                text="",
                from_agent=True,
                sent_at=int(time.time() * 1000),
                phase="final",
                attachments=(stored,),
            )
        result = dict(first)
        result["path"] = str(path)
        result["name"] = name
        return result

    async def _extract_agent_channel_images(
        self, text: str, *, load_data: bool = True
    ) -> tuple[str, list[dict[str, Any]]]:
        """Replace valid #[/path] instructions with outbound image attachments."""
        images: list[dict[str, Any]] = []
        spans: list[tuple[int, int]] = []
        roots = self.writable.external_source_roots()
        used_names: set[str] = set()
        total_bytes = 0
        for match in CHANNEL_IMAGE_DIRECTIVE_RE.finditer(text):
            if len(images) >= MAX_AGENT_IMAGE_DIRECTIVES:
                break
            raw_path = match.group(1).strip()
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
            except OSError:
                continue
            if not path.is_absolute() or not path.is_file():
                continue
            if not any(path == root or root in path.parents for root in roots):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > MAX_IMAGE_BYTES:
                continue
            if total_bytes + size > MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES:
                continue
            if load_data:
                try:
                    data = await asyncio.to_thread(path.read_bytes)
                except OSError:
                    continue
                mime = detect_image_mime(data)
            else:
                try:
                    with path.open("rb") as handle:
                        data = handle.read(64)
                except OSError:
                    continue
                mime = detect_image_mime(data)
            if mime is None:
                continue
            name = path.name.strip()[:MAX_ATTACHMENT_NAME_CHARS] or f"图片{len(images) + 1}"
            base = name
            suffix = 2
            while name.casefold() in used_names:
                name = f"{base} ({suffix})"
                suffix += 1
            used_names.add(name.casefold())
            total_bytes += size
            spans.append(match.span())
            if load_data:
                images.append({"data": data, "mime": mime, "name": name, "path": str(path)})
            else:
                images.append({"name": name, "path": str(path)})
        cleaned = text
        for start, end in reversed(spans):
            cleaned = cleaned[:start] + cleaned[end:]
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, images

    async def _send_external_result(
        self, tool_call_id: str, text: str, is_error: bool
    ) -> None:
        self.operation_journal.finish(
            tool_call_id,
            status="error" if is_error else "completed",
            error=text if is_error else "",
        )
        try:
            await self.backend.send_external_tool_result(
                tool_call_id,
                [{"type": "text", "text": text}],
                is_error=is_error,
            )
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deliver external tool result tool_call_id=%s", tool_call_id)

    def _approval_respond(
        self, conn: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        approval_id = require_string(params, "approval_id")
        decision = require_string(params, "decision")
        if decision not in ("allow", "deny"):
            raise RpcError(ErrorCode.INVALID_PARAMS, "decision must be 'allow' or 'deny'")
        message = optional_string(params, "message")
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise RpcError(
                ErrorCode.TURN_NOT_FOUND,
                "approval request not found",
                {"approval_id": approval_id},
            )
        if approval.turn.origin != "mqtt" and approval.turn.conn is not conn:
            raise RpcError(ErrorCode.NOT_INITIATOR, "only the initiating client may approve")
        if approval.future.done():
            raise RpcError(ErrorCode.TURN_NOT_FOUND, "approval already resolved")
        approval.future.set_result((decision == "allow", message))
        LOGGER.info(
            "local approval decision id=%s decision=%s turn=%s origin=%s",
            approval_id,
            decision,
            approval.turn.turn_id,
            approval.turn.origin,
        )
        return {"approval_id": approval_id, "decision": decision, "ok": True}

    # ---------------------------------------------------------------- identity
    def _identity_get(self) -> dict[str, Any]:
        registry_identity = (
            self._identity_registry.get(self._channel_self_key())
            if self._identity_registry is not None
            else None
        )
        identity = MemberIdentity(
            id=self._channel_self_key(),
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            kind=MEMBER_KIND_AGENT,
        )
        return {
            "id": identity.id,
            "name": identity.name,
            "aliases": list(identity.aliases),
            "kind": identity.kind,
            "created_at": registry_identity.created_at if registry_identity else "",
            "description": truncate_member_description(
                self.config.channel_description
            ),
            "profile": self.config.profile,
            "agent_id": self.config.agent_id,
            "userdata": str(self._userdata_dir()),
            "manifest": (
                str(self._identity_manifest_path)
                if self._identity_manifest_path is not None
                else str(self._userdata_dir() / "identity.json")
            ),
            "registry": (
                str(self._identity_registry.path)
                if self._identity_registry is not None
                else None
            ),
            "authority": "client-local",
        }

    async def _identity_rename(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = require_string(params, "name").strip()
        if not name:
            raise RpcError(ErrorCode.INVALID_PARAMS, "name must not be empty")
        if len(name) > 80:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "name must be at most 80 characters"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "name contains control characters"
            )
        current_id = self._channel_self_key()
        current = MemberIdentity(
            id=current_id,
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            kind=MEMBER_KIND_AGENT,
        )
        renamed = current.renamed(name)
        target_key = canonical_name(renamed.name)
        for member_id, member in self._channel_members.items():
            if member_id == current_id:
                continue
            labels = {
                canonical_name(member.get("name")),
                *(canonical_name(alias) for alias in member.get("aliases") or ()),
            }
            if target_key in labels:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    f"name {name!r} already belongs to another member",
                    {"owner": member_id},
                )
        if self._identity_registry is not None:
            try:
                renamed = self._identity_registry.rename(current_id, renamed.name)
            except IdentityConflict as exc:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS, str(exc)
                ) from exc
            except (IdentityError, OSError) as exc:
                raise RpcError(ErrorCode.INTERNAL_ERROR, str(exc)) from exc
        if self._identity_manifest_path is not None:
            try:
                write_manifest(
                    self._identity_manifest_path,
                    renamed,
                    profile=self.config.profile,
                    agent_id=self.config.agent_id,
                    userdata=self.config.aki.userdata
                    if self.config.aki is not None
                    else str(self._userdata_dir()),
                )
            except (IdentityError, OSError) as exc:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR, f"cannot write identity manifest: {exc}"
                ) from exc
        config_path = self._profile_config_path()
        try:
            sync_channel_identity(
                config_path,
                member_id=renamed.id,
                name=renamed.name,
                aliases=renamed.aliases,
            )
        except OSError as exc:
            raise RpcError(
                ErrorCode.INTERNAL_ERROR, f"cannot mirror identity into config: {exc}"
            ) from exc
        self.config.channel_name = renamed.name
        self.config.channel_aliases = renamed.aliases
        self.config.channel_member_id = renamed.id
        self.config.channel_id = renamed.id
        self.config.mqtt_sender_name = renamed.name
        self.config.mqtt_sender_id = renamed.id
        if self.channel is not None:
            self.channel.sender_name = renamed.name
            self.channel.sender_id = renamed.id
        await self._refresh_channel_identity()
        if self.channel is not None and self.channel.connected:
            with contextlib.suppress(Exception):
                await self._publish_channel_status()
        return self._identity_get()

    # ---------------------------------------------------------------- username
    def _userdata_dir(self) -> Path:
        return self.config.userdata_dir or (self.config.cwd / "userdata")

    def _profile_config_path(self) -> Path:
        if self.config.profile_config_path is not None:
            return self.config.profile_config_path
        if self.config.aki is not None:
            return self.config.aki.path
        return Path.home() / ".aki" / f"{self.config.profile}.conf"

    def _read_username(self) -> str:
        return self.config.username.strip() or "Master"

    def _username_get(self) -> dict[str, Any]:
        config_path = self._profile_config_path()
        source = self.config.username_source or "default"
        source_path = (
            str(self.config.username_path)
            if self.config.username_path is not None
            and source.startswith("legacy-")
            else str(config_path)
        )
        return {
            "username": self._read_username(),
            "path": str(config_path),
            "key": "gateway.username",
            "source": source,
            "source_path": source_path,
            "agent_id": self.config.agent_id,
            "profile": self.config.profile,
        }

    async def _username_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        username = require_string(params, "username").strip()
        if not username:
            raise RpcError(ErrorCode.INVALID_PARAMS, "username must not be empty")
        if len(username) > 64:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "username must be at most 64 characters"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in username):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "username contains control characters"
            )

        async with self._username_lock:
            if (
                self._recompiling
                or self._compacting
                or self._resetting
                or self._policy_restarting
                or self.active is not None
                or self.queue
            ):
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "cannot change username while a turn or maintenance operation is active",
                )
            userdata = self._userdata_dir()
            script = self.config.cwd / "scripts" / "sync-persona.py"
            if not userdata.is_dir():
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "userdata directory not found",
                    {"path": str(userdata)},
                )
            if not script.is_file():
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "persona sync script not found",
                    {"path": str(script)},
                )
            backup_dir = self.config.backup_dir or (Path.home() / ".letta" / "backups")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                "--json",
                "--source",
                str(userdata),
                "--memory-dir",
                str(self.memory_dir),
                "--backup-dir",
                str(backup_dir),
                "--username",
                username,
                "--message",
                f"username: set to {username}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = stderr.decode("utf-8", "replace").strip()[-500:]
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync failed",
                    {"detail": detail} if detail else None,
                )
            text = stdout.decode("utf-8", "replace").strip()
            try:
                result = json.loads(text)
            except ValueError as exc:
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync returned invalid JSON",
                    {"stdout": text[-500:]},
                ) from exc
            if not isinstance(result, dict):
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "username sync returned an unexpected result",
                )
            config_path = self._profile_config_path()
            try:
                set_key(config_path, "gateway", "username", username)
            except OSError as exc:
                raise RpcError(
                    ErrorCode.USERNAME_FAILED,
                    "persona rendered but profile config could not be updated",
                    {"path": str(config_path), "detail": str(exc)},
                ) from exc
            self.config.username = username
            self.config.username_source = "profile-config"
            self.config.username_path = None
            if self.config.aki is not None:
                self.config.aki.username = username
            result["path"] = str(config_path)
            result["key"] = "gateway.username"
            result["source"] = "profile-config"
            result["agent_id"] = self.config.agent_id
            result["profile"] = self.config.profile
            LOGGER.info(
                "username updated value=%s changed=%s commit=%s path=%s key=gateway.username",
                result.get("username"),
                result.get("changed"),
                result.get("commit"),
                config_path,
            )
            try:
                recompiled = await self._recompile_conversation()
            except RpcError as exc:
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "username saved but prompt recompile failed; run 'akictl recompile' to retry",
                    {
                        "username": username,
                        "detail": exc.message,
                        **(exc.data or {}),
                    },
                ) from exc
            result.update(recompiled)
            await self._refresh_channel_policy()
            await self._refresh_channel_wake_routing()
            result["applies"] = "now"
            return result

    # ---------------------------------------------------------- prompt rebuild
    def _prompt_block(self, path: Path, tag: str) -> str | None:
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---\n"):
            return None
        front, separator, body = text[3:].partition("\n---\n")
        if not separator:
            front, separator, body = text[3:].partition("\n---")
            if not separator:
                return None
            body = body.lstrip("\n")
        description = ""
        for line in front.splitlines():
            if line.startswith("description:"):
                description = line[len("description:") :].strip()
                break
        if not description:
            return None
        return f"<{tag}>\n<description>{description}</description>\n{body.strip()}\n</{tag}>"

    def _deferred_memory_block(self) -> str:
        lines: list[str] = []
        try:
            children = sorted(self.memory_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.name.startswith(".") or not child.is_dir():
                continue
            if (child / "MEMORY.md").is_file():
                lines.append(
                    f'<directory path="{child.name}/" index="{child.name}/MEMORY.md" />'
                )
        if not lines:
            return ""
        return "<deferred-memory>\n" + "\n".join(lines) + "\n</deferred-memory>"

    async def _memfs_revision(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(self.memory_dir),
                "rev-parse",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        revision = stdout.decode("utf-8", "replace").strip()
        return revision or None

    async def _recompile_conversation(self) -> dict[str, Any]:
        async with self._recompile_lock:
            self._recompiling = True
            try:
                return await self._recompile_conversation_locked()
            finally:
                self._recompiling = False
                if not self._shutting_down:
                    await self._maybe_start_next()

    async def _recompile_conversation_locked(self) -> dict[str, Any]:
        if self._shutting_down:
            raise RpcError(ErrorCode.RECOMPILE_FAILED, "gateway is shutting down")
        if self._resetting or self._compacting or self._policy_restarting:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED,
                "another maintenance operation is in progress",
            )
        if self.active is not None or self.queue:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED, "cannot recompile while a turn is active"
            )
        conversation_dir = self._conversation_dir()
        prompt_path = conversation_dir / "system-prompt.json"
        if not prompt_path.is_file():
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED,
                "compiled system prompt not found",
                {"path": str(prompt_path)},
            )

        backup_root = self.config.backup_dir or (Path.home() / ".letta" / "backups")
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = backup_root / f"system-prompt-recompile-{stamp}.json"
        suffix = 1
        while backup_path.exists():
            backup_path = backup_root / f"system-prompt-recompile-{stamp}-{suffix}.json"
            suffix += 1

        await self.backend.stop()
        try:
            try:
                record = json.loads(prompt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "cannot read compiled system prompt",
                ) from exc
            if not isinstance(record, dict):
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "compiled system prompt is not an object",
                )
            content = record.get("content")
            if not isinstance(content, str) or not content.strip():
                raise RpcError(
                    ErrorCode.RECOMPILE_FAILED,
                    "compiled system prompt has no content",
                )
            shutil.copy2(prompt_path, backup_path)

            blocks: dict[str, str | None] = {
                "human": self._prompt_block(self.memory_dir / "human.md", "human"),
                "memory-policy": self._prompt_block(
                    self.memory_dir / "memory-policy.md", "memory-policy"
                ),
                "persona": self._prompt_block(self.memory_dir / "persona.md", "persona"),
            }
            memory_index_path = self.memory_dir / "MEMORY.md"
            if memory_index_path.is_file():
                try:
                    memory_index = memory_index_path.read_text(encoding="utf-8").strip()
                except OSError:
                    memory_index = ""
                if memory_index:
                    deferred = self._deferred_memory_block()
                    blocks["memory"] = (
                        "<memory>\n" + memory_index + "\n" + deferred + "\n</memory>"
                        if deferred
                        else "<memory>\n" + memory_index + "\n</memory>"
                    )

            for tag, new_block in blocks.items():
                if new_block is None:
                    continue
                content, count = re.subn(
                    rf"<{tag}>.*?</{tag}>",
                    lambda _match, value=new_block: value,
                    content,
                    count=1,
                    flags=re.S,
                )
                if count != 1:
                    raise RpcError(
                        ErrorCode.RECOMPILE_FAILED,
                        f"compiled system prompt is missing the <{tag}> block",
                    )

            content = apply_channel_policy(
                content,
                render_channel_policy(
                    user_name=self.config.username or "Master",
                    emoji_entries=self._emoji_entries(),
                ),
            )
            content = clear_channel_turn(content)
            content = apply_channel_wake_routing(
                content,
                render_channel_wake_routing(self.config.username or "Master"),
            )
            if self._solo_session is not None:
                scene = self._solo_scene or load_solo_scene(
                    self.config.profile,
                    username=self.config.username,
                    explicit=(
                        self.config.aki.solo_prompt_file
                        if self.config.aki is not None
                        else ""
                    ),
                )
                self._solo_scene = scene
                content = apply_solo_mode(
                    content,
                    render_solo_mode(
                        member_name=self.config.channel_name,
                        user_name=self.config.username,
                        session_id=self._solo_session.session_id,
                        scene=scene,
                    ),
                )
            else:
                content = clear_solo_mode(content)
            content = clear_solo_transcript(content)
            memory_config = self._solo_memory_llm_config()
            if memory_config is not None:
                solo_memories_block = await self.solo_memory.render_for_prompt(
                    current_session_id=(
                        self._solo_session.session_id
                        if self._solo_session is not None
                        else ""
                    ),
                    **memory_config,
                )
            else:
                solo_memories_block = render_solo_memories(
                    [
                        session
                        for session in self.solo_memory.sessions
                        if self._solo_session is None
                        or session.session_id != self._solo_session.session_id
                    ],
                    budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
                )
            content = (
                apply_solo_memories(content, solo_memories_block)
                if solo_memories_block
                else clear_solo_memories(content)
            )
            shared_memory_block = self._public_memory_prompt_block()
            content = (
                apply_shared_memory(content, shared_memory_block)
                if shared_memory_block
                else clear_shared_memory(content)
            )

            old_core = str(record.get("coreMemory") or "")
            core_parts: list[str] = []
            for tag in ("human", "memory-policy", "persona"):
                new_block = blocks.get(tag)
                if new_block is not None:
                    core_parts.append(new_block)
                    continue
                old_block = re.search(
                    rf"<{tag}>.*?</{tag}>", old_core, re.S
                )
                if old_block:
                    core_parts.append(old_block.group(0))
            revision = await self._memfs_revision()
            record["content"] = content
            record["coreMemory"] = "\n\n".join(core_parts) + "\n"
            record["compiledAt"] = now_iso()
            if revision:
                record["memfsRevision"] = revision
            temp_path = prompt_path.with_name(prompt_path.name + ".tmp")
            temp_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, prompt_path)
        finally:
            self.backend.start()
            ready = await self._wait_backend_ready(20.0)
        if not ready:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED,
                "prompt rebuilt but backend did not become ready",
                {
                    "backup_path": str(backup_path),
                    "backend_ready": False,
                    "backend_error": getattr(self.backend, "last_error", None),
                },
            )
        LOGGER.info(
            "conversation prompt recompiled revision=%s backup=%s",
            revision,
            backup_path,
        )
        return {
            "recompiled": True,
            "conversation_id": self.config.conversation_id,
            "memfs_revision": revision,
            "compiled_at": record["compiledAt"],
            "backup_path": str(backup_path),
            "backend_ready": True,
        }

    async def _compact_conversation(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        mode = optional_string(params, "mode")
        if mode is not None and mode not in {
            "all",
            "sliding_window",
            "self_compact_all",
            "self_compact_sliding_window",
        }:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "mode must be all, sliding_window, self_compact_all, "
                "or self_compact_sliding_window",
            )
        timeout = require_int(
            params, "timeout", default=900, minimum=30, maximum=3600
        )
        async with self._compact_lock:
            self._compacting = True
            try:
                if self._shutting_down:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED, "gateway is shutting down"
                    )
                if self._resetting or self._recompiling or self._policy_restarting:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "another maintenance operation is in progress",
                    )
                if self.active is not None or self.queue:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "cannot compact while a turn is active or queued",
                    )

                if self._aki_bridge_available():
                    try:
                        result = await self._compact_conversation_bridge(
                            mode, float(timeout)
                        )
                    except RpcError:
                        raise
                    except Exception as exc:
                        raise RpcError(
                            ErrorCode.COMPACT_FAILED,
                            str(exc),
                            {"conversation_id": self.config.conversation_id},
                        ) from exc
                    LOGGER.info(
                        "conversation compacted via bridge mode=%s reflection=%s "
                        "before=%s after=%s",
                        result.get("mode"),
                        result.get("reflection"),
                        result.get("num_messages_before"),
                        result.get("num_messages_after"),
                    )
                    return {
                        **result,
                        "conversation_id": self.config.conversation_id,
                        "backend_ready": True,
                    }

                with contextlib.suppress(Exception):
                    await self.backend.stop()
                failure: Exception | None = None
                result: dict[str, Any] | None = None
                try:
                    result = await asyncio.to_thread(
                        run_letta_compact,
                        letta_bin=self.config.letta_bin,
                        agent_id=self.config.agent_id,
                        conversation_id=self.config.conversation_id,
                        cwd=self.config.cwd,
                        mode=mode,
                        timeout=float(timeout),
                        backend_dir=self.config.backend_dir,
                        letta_home=(
                            self.config.letta_home or self.writable.letta_home
                        ),
                        env=os.environ,
                    )
                except Exception as exc:
                    failure = exc
                finally:
                    try:
                        self.backend.start()
                        ready = await self._wait_backend_ready(60.0)
                    except Exception as exc:
                        LOGGER.exception("backend restart failed after compaction")
                        ready = False
                        if failure is None:
                            failure = exc

                if failure is not None:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        str(failure),
                        {
                            "conversation_id": self.config.conversation_id,
                            "backend_ready": ready,
                            "backend_error": getattr(self.backend, "last_error", None),
                        },
                    )
                if result is None:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "compaction returned no result",
                        {"backend_ready": ready},
                    )
                if not ready:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "conversation compacted but backend did not become ready",
                        {
                            **result,
                            "backend_ready": False,
                            "backend_error": getattr(self.backend, "last_error", None),
                        },
                    )
                LOGGER.info(
                    "conversation compacted mode=%s reflection=%s before=%s after=%s",
                    result.get("mode"),
                    result.get("reflection"),
                    result.get("num_messages_before"),
                    result.get("num_messages_after"),
                )
                return {
                    **result,
                    "conversation_id": self.config.conversation_id,
                    "backend_ready": True,
                }
            finally:
                self._compacting = False
                if not self._shutting_down:
                    await self._maybe_start_next()

    async def _compact_conversation_bridge(
        self, mode: str | None, timeout: float
    ) -> dict[str, Any]:
        before = await self.backend.reflection_status(
            conversation_id=self.config.conversation_id,
            agent_id=self.config.agent_id,
        )
        compacted = await self.backend.compact_conversation(
            conversation_id=self.config.conversation_id,
            agent_id=self.config.agent_id,
            mode=mode,
            timeout=timeout,
        )
        reflection = "not_launched"
        try:
            launched = await self.backend.reflect_conversation(
                conversation_id=self.config.conversation_id,
                agent_id=self.config.agent_id,
            )
            if not launched.get("launched"):
                reflection = str(launched.get("reason") or "not_launched")
            else:
                reflection = await self._wait_bridge_reflection(before, timeout)
        except Exception as exc:
            LOGGER.warning("bridge reflection failed: %s", exc)
            reflection = "failed"
        return {
            "compacted": True,
            "mode": mode or "default",
            "num_messages_before": compacted.get("num_messages_before"),
            "num_messages_after": compacted.get("num_messages_after"),
            "summary": compacted.get("summary"),
            "reflection": reflection,
        }

    async def _wait_bridge_reflection(
        self, before: Mapping[str, Any], timeout: float
    ) -> str:
        before_succeeded = str(before.get("last_reflection_succeeded_at") or "")
        before_started = str(before.get("last_reflection_started_at") or "")
        deadline = time.monotonic() + max(30.0, timeout)
        seen = False
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            try:
                state = await self.backend.reflection_status(
                    conversation_id=self.config.conversation_id,
                    agent_id=self.config.agent_id,
                )
            except Exception:
                continue
            started = str(state.get("last_reflection_started_at") or "")
            succeeded = str(state.get("last_reflection_succeeded_at") or "")
            if started and started != before_started:
                seen = True
            if succeeded and succeeded != before_succeeded:
                return "completed"
            if seen and not started:
                return "finished"
        return "timeout"

    # ------------------------------------------------------------- conversation
    def _conversation_dir(self) -> Path:
        key = _b64url(f"{self.config.conversation_id}:{self.config.agent_id}")
        return self.config.backend_dir / "conversations" / key

    def _conversation_backup_root(self) -> Path:
        return self.config.backend_dir.parent / "backups"

    def _backup_conversation(self, conversation_dir: Path) -> Path:
        root = self._conversation_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"aki-{self.config.conversation_id}-reset-{stamp}"
        candidate = root / name
        suffix = 1
        while candidate.exists():
            candidate = root / f"{name}-{suffix}"
            suffix += 1
        shutil.copytree(conversation_dir, candidate, symlinks=True)
        return candidate

    def _clear_conversation_messages(self, conversation_dir: Path) -> int:
        messages_path = conversation_dir / "messages.jsonl"
        cleared_bytes = 0
        if messages_path.exists():
            cleared_bytes = messages_path.stat().st_size
        with messages_path.open("wb"):
            pass
        os.chmod(messages_path, 0o600)

        conversation_path = conversation_dir / "conversation.json"
        if not conversation_path.exists():
            raise ValueError("conversation.json is missing")
        try:
            record = json.loads(conversation_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("conversation.json is invalid") from exc
        if not isinstance(record, dict):
            raise ValueError("conversation.json is not an object")
        record["in_context_message_ids"] = []
        record["summary"] = None
        record["last_message_at"] = None
        record["updated_at"] = now_iso()
        temp_path = conversation_path.with_name(conversation_path.name + ".tmp")
        temp_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, conversation_path)
        return cleared_bytes

    async def _reset_conversation(self) -> dict[str, Any]:
        async with self._reset_lock:
            if self._compacting or self._recompiling:
                raise RpcError(
                    ErrorCode.RESET_FAILED,
                    "another maintenance operation is in progress",
                )
            self._resetting = True
            interrupted_turn_id: str | None = None
            dropped_turn_ids: list[str] = []
            backup_path: Path | None = None
            cleared_bytes = 0
            try:
                conversation_dir = self._conversation_dir()
                if not conversation_dir.is_dir():
                    raise RpcError(
                        ErrorCode.RESET_FAILED,
                        "conversation directory not found",
                        {
                            "conversation_id": self.config.conversation_id,
                            "agent_id": self.config.agent_id,
                            "path": str(conversation_dir),
                        },
                    )

                turn = self.active
                if turn is not None and not turn.end_sent:
                    interrupted_turn_id = turn.turn_id
                    turn.cancelled = True
                    for approval in list(self.approvals.values()):
                        if approval.turn is turn and not approval.future.done():
                            approval.future.set_result((False, "conversation reset"))

                queued = list(self.queue)
                self.queue.clear()
                for item in queued:
                    dropped_turn_ids.append(item.turn_id)
                    item.cancelled = True
                    item.state = "cancelled"
                    item.end_sent = True
                    item.done_event.set()
                    with contextlib.suppress(Exception):
                        await self._emit(
                            item,
                            "turn_end",
                            {
                                "reason": "conversation_reset",
                                "cancelled": True,
                                "message": "conversation reset",
                            },
                        )

                if turn is not None and not turn.end_sent:
                    with contextlib.suppress(Exception):
                        await self._emit(
                            turn,
                            "error",
                            {
                                "code": "CONVERSATION_RESET",
                                "message": "conversation reset; turn interrupted",
                            },
                        )
                    await self._finish_turn(
                        turn,
                        "conversation_reset",
                        {"interrupted": True, "message": "conversation reset"},
                    )

                with contextlib.suppress(Exception):
                    await self.backend.stop()
                backup_path = self._backup_conversation(conversation_dir)
                cleared_bytes = self._clear_conversation_messages(conversation_dir)
                self.last_usage = None
                self._schedule_status_refresh()
                self.backend.start()
                ready = await self._wait_backend_ready(20.0)
                if not ready:
                    raise RpcError(
                        ErrorCode.RESET_FAILED,
                        "conversation cleared but backend did not become ready",
                        {
                            "backup_path": str(backup_path),
                            "cleared_messages_bytes": cleared_bytes,
                            "backend_ready": False,
                            "backend_error": getattr(self.backend, "last_error", None),
                        },
                    )
                LOGGER.info(
                    "conversation reset id=%s backup=%s cleared_bytes=%s",
                    self.config.conversation_id,
                    backup_path,
                    cleared_bytes,
                )
                return {
                    "conversation_id": self.config.conversation_id,
                    "agent_id": self.config.agent_id,
                    "backup_path": str(backup_path),
                    "cleared_messages_bytes": cleared_bytes,
                    "interrupted_turn_id": interrupted_turn_id,
                    "dropped_turn_ids": dropped_turn_ids,
                    "backend_ready": True,
                }
            except RpcError:
                raise
            except Exception as exc:
                LOGGER.exception("conversation reset failed")
                raise RpcError(
                    ErrorCode.RESET_FAILED, f"conversation reset failed: {exc}"
                ) from exc
            finally:
                self._resetting = False
                if not self._shutting_down and not bool(
                    getattr(self.backend, "ready", False)
                ):
                    with contextlib.suppress(Exception):
                        self.backend.start()
                        await self._wait_backend_ready(20.0)
                if not self._shutting_down:
                    await self._maybe_start_next()

    # ----------------------------------------------------------------- memory
    async def _run_letta_json_value(
        self,
        args: list[str],
        timeout: float = 20.0,
        *,
        label: str = "letta command",
    ) -> Any:
        command = [self.config.letta_bin, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, f"{label} failed") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:300]
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"{label} failed",
                {"detail": detail} if detail else None,
            )
        try:
            return json.loads(stdout.decode("utf-8"))
        except ValueError as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "letta returned invalid JSON") from exc

    async def _run_letta_json(self, args: list[str], timeout: float = 20.0) -> dict[str, Any]:
        data = await self._run_letta_json_value(
            args, timeout=timeout, label="letta memory command"
        )
        if not isinstance(data, dict):
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "letta returned unexpected JSON")
        return data

    async def _memory_status(self) -> dict[str, Any]:
        if self._aki_bridge_available():
            return await self.backend.memory_status(agent_id=self.config.agent_id)
        return await self._run_letta_json(
            ["memory", "status", "--agent", self.config.agent_id]
        )

    async def _memory_tokens(self) -> dict[str, Any]:
        data, error = await self._memory_tokens_cached(allow_error=False)
        if data is None:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, error or "memory tokens unavailable")
        return data

    async def _memory_tokens_cached(
        self, *, allow_error: bool = False
    ) -> tuple[dict[str, Any] | None, str | None]:
        async with self._token_lock:
            created, cached, error = self._token_cache
            if cached is not None and time.monotonic() - created < 30:
                return cached, error
            try:
                if self._aki_bridge_available():
                    data = await self.backend.memory_tokens(
                        agent_id=self.config.agent_id
                    )
                else:
                    data = await self._run_letta_json(
                        [
                            "memory",
                            "tokens",
                            "--agent",
                            self.config.agent_id,
                            "--format",
                            "json",
                        ]
                    )
            except RpcError as exc:
                self._token_cache = (time.monotonic(), None, exc.message)
                if allow_error:
                    return None, exc.message
                raise
            self._token_cache = (time.monotonic(), data, None)
            return data, None

    def _memory_tree(self, params: Mapping[str, Any]) -> dict[str, Any]:
        depth = require_int(params, "depth", default=4, minimum=1, maximum=10)
        root = self._memory_root()
        entries: list[dict[str, Any]] = []

        def walk(directory: Path, prefix: str, remaining: int) -> None:
            if remaining <= 0:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError:
                return
            for child in children:
                if child.name.startswith("."):
                    continue
                try:
                    real = child.resolve(strict=False)
                except OSError:
                    continue
                if real != root and root not in real.parents:
                    continue
                rel = f"{prefix}/{child.name}" if prefix else child.name
                try:
                    st = child.stat()
                except OSError:
                    continue
                is_dir = child.is_dir()
                entries.append(
                    {
                        "path": rel,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
                if is_dir:
                    walk(child, rel, remaining - 1)

        walk(root, "", depth)
        return {"root": ".", "entries": entries}

    def _memory_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        relative = require_string(params, "path")
        path = resolve_memory_path(self.memory_dir, relative)
        if not path.exists() or not path.is_file():
            raise RpcError(ErrorCode.INVALID_PATH, "file does not exist")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "cannot stat file") from exc
        if size > MAX_MEMORY_FILE:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "file exceeds 1 MiB",
                {"size": size, "max": MAX_MEMORY_FILE},
            )
        try:
            content = path.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "file is not valid UTF-8") from exc
        except OSError as exc:
            raise RpcError(ErrorCode.INVALID_PATH, "cannot read file") from exc
        return {"path": relative, "content": content, "size": size}

    def _memory_search(self, params: Mapping[str, Any]) -> dict[str, Any]:
        query = require_string(params, "query")
        relative = optional_string(params, "path") or ""
        limit = require_int(params, "limit", default=50, minimum=1, maximum=200)
        root = self._memory_root()
        start = root if not relative else resolve_memory_path(root, relative)
        if start.is_file():
            files = [start]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    path = Path(dirpath) / name
                    try:
                        real = path.resolve(strict=False)
                    except OSError:
                        continue
                    if real != root and root not in real.parents:
                        continue
                    try:
                        if path.stat().st_size > MAX_MEMORY_FILE:
                            continue
                    except OSError:
                        continue
                    files.append(path)
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for path in sorted(files):
            try:
                text = path.read_text("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "line": number,
                            "text": line,
                        }
                    )
                    if len(matches) >= limit:
                        return {"query": query, "matches": matches, "truncated": True}
        return {"query": query, "matches": matches, "truncated": False}

    def _memory_root(self) -> Path:
        try:
            return self.memory_dir.resolve(strict=True)
        except OSError as exc:
            raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "memory root is unavailable") from exc


async def _serve(config: GatewayConfig) -> None:
    server = GatewayServer(config)
    await server.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await server.stop()


def _agent_identity(agent_id: str) -> tuple[str, str]:
    folder = default_backend_dir() / "agents"
    try:
        paths = folder.glob("*.json")
    except OSError:
        return "", ""
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("id") != agent_id:
            continue
        name = data.get("name")
        description = data.get("description")
        return (
            name.strip() if isinstance(name, str) else "",
            description.strip() if isinstance(description, str) else "",
        )
    return "", ""


def _channel_identity(aki: AkiConfig) -> tuple[str, str, tuple[str, ...], str]:
    agent_name, agent_description = _agent_identity(aki.agent_id)
    name = aki.channel.name or agent_name or ("Aki" if aki.profile == "aki" else aki.profile)
    description = aki.channel.description or agent_description
    aliases: list[str] = []
    for alias in (*aki.channel.aliases, name):
        text = alias.strip()
        if text and text not in aliases:
            aliases.append(text)
    return aki.channel.id or aki.agent_id, name, tuple(aliases), description


def build_config_from_env(profile: str | None = None) -> GatewayConfig:
    profile_name, config_path = resolve_config_path(profile)
    if profile is not None and profile_name != "aki" and not config_path.exists():
        raise ConfigError(f"profile {profile_name!r} has no config at {config_path}")
    explicit_config = os.environ.get("AKI_CONFIG")
    migrate_from = None
    if profile is None and not explicit_config and not config_path.exists():
        migrate_from = legacy_state_dir()
    aki = ensure_config(
        config_path,
        migrate_from=migrate_from,
        profile=profile_name,
    )
    channel_id, channel_name, channel_aliases, channel_description = _channel_identity(aki)
    userdata_label = aki.userdata
    userdata_dir = Path(userdata_label).expanduser()
    if not userdata_dir.is_absolute():
        userdata_dir = Path(os.environ.get("AKI_CWD", Path.cwd())) / userdata_dir
    identity_registry_path = state_dir() / "identities.json"
    try:
        backup_migration_state(
            state_dir(),
            paths=(
                config_path,
                identity_registry_path,
                userdata_dir / "identity.json",
                state_dir() / "channel-history",
            ),
        )
        identity, identity_manifest_path = ensure_manifest(
            userdata_dir,
            profile=aki.profile,
            agent_id=aki.agent_id,
            fallback_name=channel_name,
            fallback_aliases=channel_aliases,
            userdata_label=userdata_label,
        )
    except IdentityError as exc:
        raise ConfigError(str(exc)) from exc
    channel_id = identity.id
    channel_name = identity.name
    channel_aliases = identity.aliases
    try:
        registry = IdentityRegistry(identity_registry_path)
        registry.observe(identity, source=f"profile:{aki.profile}")
    except OSError:
        pass
    if (
        aki.channel.id != identity.id
        or aki.channel.name != identity.name
        or tuple(aki.channel.aliases) != tuple(identity.aliases)
    ):
        try:
            sync_channel_identity(
                config_path,
                member_id=identity.id,
                name=identity.name,
                aliases=identity.aliases,
            )
        except OSError as exc:
            raise ConfigError(f"cannot mirror identity into {config_path}: {exc}") from exc
    if os.environ.get("AKI_SOCKET"):
        socket_path = Path(os.environ["AKI_SOCKET"]).expanduser()
    elif aki.socket:
        socket_path = Path(aki.socket).expanduser()
    else:
        socket_path = default_socket_path(aki.profile)
    cwd = Path(os.environ.get("AKI_CWD", Path.cwd()))
    username = aki.username.strip()
    username_source = "profile-config" if username else ""
    username_path: Path | None = None
    if not username:
        candidates: list[tuple[Path, str]] = []
        override = os.environ.get("AKI_USERNAME_PATH")
        if override:
            candidates.append((Path(override).expanduser(), "legacy-profile-file"))
        candidates.append((default_username_path(aki.agent_id), "legacy-profile-file"))
        candidates.append((userdata_dir / "username.txt", "legacy-userdata"))
        for candidate, source in candidates:
            try:
                if candidate.is_file():
                    username = candidate.read_text(encoding="utf-8").strip()
                    if username:
                        username_source = source
                        username_path = candidate
                        break
            except OSError:
                continue
    if not username:
        username = "Master"
        username_source = "default"
    image_require_approval = os.environ.get(
        "AKI_TOKIFACTOR_IMAGE_REQUIRE_APPROVAL", "0"
    ).strip().lower() in ("1", "true", "yes", "on")
    letta_bin = os.environ.get("AKI_LETTA_BIN", "").strip() or aki.letta_bin
    layer_enabled = os.environ.get("AKI_LAYER_ENABLED")
    if layer_enabled is None:
        layer_enabled = aki.profile == "aki"
    else:
        layer_enabled = layer_enabled.strip().lower() in ("1", "true", "yes", "on")
    layer_bin = Path(
        os.environ.get("AKI_LAYER_BIN", "").strip() or default_layer_binary()
    ).expanduser()
    layer_socket_value = os.environ.get("AKI_LAYER_SOCKET", "").strip()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip() or f"/run/user/{os.getuid()}"
    layer_socket = (
        Path(layer_socket_value).expanduser()
        if layer_socket_value
        else Path(runtime_dir) / "aki-layer.sock"
    )
    layer_notify_value = os.environ.get("AKI_LAYER_NOTIFY_ENABLED")
    if layer_notify_value is None:
        layer_notify_enabled = layer_enabled
    else:
        layer_notify_enabled = layer_notify_value.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    try:
        layer_preview_chars = max(
            1, int(os.environ.get("AKI_LAYER_PREVIEW_CHARS", "80"))
        )
    except ValueError:
        layer_preview_chars = 80
    mcp_native_servers = tuple(
        name.strip()
        for name in os.environ.get("AKI_MCP_NATIVE_SERVERS", "").split(",")
        if name.strip()
    )
    state_root = state_dir()
    schedule_state = (
        Path(os.environ["AKI_SCHEDULE_STATE"]).expanduser()
        if os.environ.get("AKI_SCHEDULE_STATE")
        else state_root / "schedules" / f"{aki.profile}.json"
    )
    ledger_state = (
        Path(os.environ["AKI_LEDGER_STATE"]).expanduser()
        if os.environ.get("AKI_LEDGER_STATE")
        else state_root / "ledger" / f"{aki.profile}.json"
    )
    presence_state = (
        Path(os.environ["AKI_PRESENCE_STATE"]).expanduser()
        if os.environ.get("AKI_PRESENCE_STATE")
        else state_root / "presence" / f"{aki.profile}.json"
    )
    return GatewayConfig(
        socket_path=socket_path,
        profile=aki.profile,
        agent_id=aki.agent_id,
        conversation_id=aki.conversation,
        letta_bin=letta_bin,
        cwd=cwd,
        emoji_dir=(
            Path(os.environ["AKI_EMOJI_DIR"]).expanduser()
            if os.environ.get("AKI_EMOJI_DIR")
            else cwd / "assets" / "emoji"
        ),
        userdata_dir=userdata_dir,
        memory_dir=(Path(os.environ["AKI_MEMORY_DIR"]).expanduser() if os.environ.get("AKI_MEMORY_DIR") else None),
        backup_dir=(Path(os.environ["AKI_BACKUP_DIR"]).expanduser() if os.environ.get("AKI_BACKUP_DIR") else None),
        backend_dir=default_backend_dir(),
        queue_max=int(os.environ.get("AKI_QUEUE_MAX", DEFAULT_QUEUE_MAX)),
        approval_timeout=float(
            os.environ.get("AKI_APPROVAL_TIMEOUT", DEFAULT_APPROVAL_TIMEOUT)
        ),
        mqtt_enabled=aki.mqtt_enabled,
        mqtt_host=aki.mqtt_host,
        mqtt_port=aki.mqtt_port,
        mqtt_topic=aki.mqtt_topic,
        mqtt_magic=aki.mqtt_magic,
        mqtt_sender_name=os.environ.get("AKI_MQTT_SENDER_NAME", channel_name),
        mqtt_sender_id=(
            configured_sender
            if is_member_id(
                configured_sender := os.environ.get("AKI_MQTT_SENDER_ID", "")
            )
            else channel_id
        ),
        channel_id=channel_id,
        channel_member_id=identity.id,
        channel_name=channel_name,
        channel_description=channel_description,
        channel_aliases=channel_aliases,
        channel_wait_for=aki.channel_wait_for,
        identity_manifest_path=identity_manifest_path,
        identity_registry_path=identity_registry_path,
        mqtt_state=config_path,
        channel_history_dir=(
            Path(os.environ["AKI_CHANNEL_HISTORY_DIR"]).expanduser()
            if os.environ.get("AKI_CHANNEL_HISTORY_DIR")
            else None
        ),
        channel_media_dir=(
            Path(os.environ["AKI_CHANNEL_MEDIA_DIR"]).expanduser()
            if os.environ.get("AKI_CHANNEL_MEDIA_DIR")
            else state_dir() / "channel-media"
        ),
        sandbox_enabled=aki.sandbox,
        bwrap_bin=os.environ.get("AKI_BWRAP_BIN", "bwrap"),
        writable_state=config_path,
        letta_home=(
            Path(os.environ["LETTA_HOME"]).expanduser()
            if os.environ.get("LETTA_HOME")
            else None
        ),
        tokifactor_image_require_approval=image_require_approval,
        camera_enabled=aki.camera_enabled,
        camera_require_approval=aki.camera_require_approval,
        camera_device=aki.camera_device,
        camera_width=aki.camera_width,
        camera_height=aki.camera_height,
        camera_output_dir=Path(aki.camera_output_dir),
        ffmpeg_bin=os.environ.get("AKI_FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=(os.environ.get("AKI_FFPROBE_BIN", "ffprobe").strip() or None),
        screenshot_enabled=aki.screenshot_enabled,
        screenshot_require_approval=aki.screenshot_require_approval,
        grim_bin=os.environ.get("AKI_GRIM_BIN", "grim"),
        spectacle_bin=os.environ.get("AKI_SPECTACLE_BIN", "spectacle"),
        screenshot_output_dir=Path(aki.screenshot_output_dir),
        screenshot_wayland_display=aki.screenshot_wayland_display or None,
        live_context_enabled=aki.live_enabled,
        live_context_budget_tokens=clamp_budget_tokens(aki.live_budget_tokens),
        mcp_native_tools_enabled=aki.mcp_native_tools,
        mcp_native_servers=mcp_native_servers,
        mcp_native_excluded_servers=aki.mcp_excluded,
        mcp_native_state=config_path,
        mcp_native_max_tools=aki.mcp_max_tools,
        mcp_tool_timeout=aki.mcp_timeout,
        zsh_history_path=(
            Path(os.environ["AKI_ZSH_HISTORY"]).expanduser()
            if os.environ.get("AKI_ZSH_HISTORY")
            else None
        ),
        live_context_timeout=aki.live_timeout,
        ps_bin=os.environ.get("AKI_PS_BIN", "ps"),
        qdbus_bin=(os.environ.get("AKI_QDBUS_BIN", "").strip() or None),
        layer_enabled=layer_enabled,
        layer_bin=layer_bin,
        layer_notify_enabled=layer_notify_enabled,
        layer_socket=layer_socket,
        layer_preview_chars=layer_preview_chars,
        avatar_path=Path(aki.avatar_path).expanduser() if aki.avatar_path else default_avatar_path(),
        username=username,
        username_source=username_source,
        username_path=username_path,
        profile_config_path=config_path,
        aki=aki,
        schedule_enabled=aki.schedule_enabled,
        schedule_tick_seconds=aki.schedule_tick_seconds,
        schedule_missed_grace_seconds=aki.schedule_missed_grace_seconds,
        schedule_max_records=aki.schedule_max_records,
        schedule_state=schedule_state,
        presence_enabled=aki.presence_enabled,
        presence_session_id=aki.presence_session_id,
        presence_loginctl_bin=aki.presence_loginctl_bin,
        presence_poll_seconds=aki.presence_poll_seconds,
        presence_idle_reset_seconds=aki.presence_idle_reset_seconds,
        presence_work_warn_seconds=aki.presence_work_warn_seconds,
        presence_work_repeat_seconds=aki.presence_work_repeat_seconds,
        presence_night_start=aki.presence_night_start,
        presence_night_end=aki.presence_night_end,
        presence_night_history_days=aki.presence_night_history_days,
        presence_return_idle_seconds=aki.presence_return_idle_seconds,
        presence_return_debounce_seconds=aki.presence_return_debounce_seconds,
        solo_state=(
            Path(os.environ["AKI_SOLO_STATE"]).expanduser()
            if os.environ.get("AKI_SOLO_STATE")
            else state_root / "solo" / f"{aki.profile}.json"
        ),
        presence_state=presence_state,
        ledger_state=ledger_state,
        tool_policy=aki.tool_policy,
        image_generation=aki.image,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aki local gateway")
    parser.add_argument("--profile", help="profile name from ~/.aki/<profile>.conf")
    parser.add_argument("--socket", help="Unix socket path")
    parser.add_argument("--agent-id", help="Letta agent id")
    parser.add_argument("--conversation-id", help="Letta conversation id")
    parser.add_argument("--letta-bin", help="Path to the letta executable")
    parser.add_argument("--mqtt-host", help="tokifactor MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, help="tokifactor MQTT broker port")
    parser.add_argument("--mqtt-topic", help="tokifactor topic")
    parser.add_argument("--mqtt-magic", help="tokifactor magic string")
    parser.add_argument("--mqtt-state", help="Persistent MQTT settings JSON path")
    parser.add_argument("--no-mqtt", action="store_true", help="disable the MQTT channel")
    parser.add_argument("--bwrap", help="Path to the bwrap executable")
    parser.add_argument("--writable-state", help="Writable directory registry JSON path")
    parser.add_argument("--letta-home", help="Letta internal home directory")
    parser.add_argument("--avatar-path", help="Aki avatar image path")
    parser.add_argument("--no-camera", action="store_true", help="disable the camera tool")
    parser.add_argument("--camera-device", help="V4L2 camera device (default /dev/video0)")
    parser.add_argument("--camera-width", type=int, help="default camera capture width")
    parser.add_argument("--camera-height", type=int, help="default camera capture height")
    parser.add_argument("--camera-output-dir", help="directory for captured photos")
    parser.add_argument("--ffmpeg-bin", help="Path to the ffmpeg executable")
    parser.add_argument("--ffprobe-bin", help="Path to the ffprobe executable")
    parser.add_argument("--no-screenshot", action="store_true", help="disable the screenshot tool")
    parser.add_argument("--grim-bin", help="Path to the grim executable")
    parser.add_argument("--spectacle-bin", help="Path to the spectacle executable")
    parser.add_argument("--screenshot-output-dir", help="directory for desktop screenshots")
    parser.add_argument(
        "--screenshot-wayland-display",
        help="preferred WAYLAND_DISPLAY for grim (default: auto-detect)",
    )
    parser.add_argument(
        "--screenshot-require-approval",
        action="store_true",
        help="require approval for desktop_take_screenshot (default: auto-approve)",
    )
    parser.add_argument(
        "--no-live-context", action="store_true", help="disable desktop live context"
    )
    parser.add_argument(
        "--live-context-budget-tokens",
        type=int,
        help="live context token ceiling (default: 65536)",
    )
    parser.add_argument("--zsh-history", help="path to the zsh history file")
    parser.add_argument("--ps-bin", help="Path to the ps executable")
    parser.add_argument("--qdbus-bin", help="Path to qdbus6/qdbus")
    parser.add_argument("--no-layer", action="store_true", help="disable the aki-layer window")
    parser.add_argument("--layer-bin", help="Path to the aki-layer executable")
    parser.add_argument(
        "--no-layer-notify",
        action="store_true",
        help="disable chat-message delivery to aki-layer bubbles",
    )
    parser.add_argument("--layer-socket", help="Path to the aki-layer control socket")
    parser.add_argument(
        "--layer-preview-chars",
        type=int,
        help="maximum notification bubble preview length (default 80)",
    )
    parser.add_argument(
        "--live-context-timeout",
        type=float,
        help="per-source timeout for live context collection",
    )
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox (unsafe)")
    parser.add_argument(
        "--tokifactor-image-require-approval",
        action="store_true",
        help="require approval for tokifactor_send_image (default: auto-approve)",
    )
    parser.add_argument("--log-level", default=os.environ.get("AKI_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = build_config_from_env(args.profile)
    except (ConfigError, OSError) as exc:
        print(f"cannot load profile config: {exc}", file=sys.stderr)
        return 2
    if args.socket:
        config.socket_path = Path(args.socket).expanduser()
    if args.agent_id:
        config.agent_id = args.agent_id
        if config.memory_dir is None:
            config.memory_dir = default_memory_dir(config.agent_id)
    if args.conversation_id:
        config.conversation_id = args.conversation_id
    if args.letta_bin:
        config.letta_bin = args.letta_bin
    if args.mqtt_host:
        config.mqtt_host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt_port = args.mqtt_port
    if args.mqtt_topic:
        config.mqtt_topic = args.mqtt_topic
    if args.mqtt_magic:
        config.mqtt_magic = args.mqtt_magic
    if args.mqtt_state:
        config.mqtt_state = Path(args.mqtt_state).expanduser()
        saved_host, saved_port = load_mqtt_server(config.mqtt_state)
        config.mqtt_host = saved_host or config.mqtt_host
        config.mqtt_port = saved_port or config.mqtt_port
    if args.no_mqtt:
        config.mqtt_enabled = False
    if args.bwrap:
        config.bwrap_bin = args.bwrap
    if args.writable_state:
        config.writable_state = Path(args.writable_state).expanduser()
    if args.letta_home:
        config.letta_home = Path(args.letta_home).expanduser()
    if args.avatar_path:
        config.avatar_path = Path(args.avatar_path).expanduser()
    if args.no_camera:
        config.camera_enabled = False
    if args.camera_device:
        config.camera_device = args.camera_device
    if args.camera_width:
        config.camera_width = args.camera_width
    if args.camera_height:
        config.camera_height = args.camera_height
    if args.camera_output_dir:
        config.camera_output_dir = Path(args.camera_output_dir).expanduser()
    if args.ffmpeg_bin:
        config.ffmpeg_bin = args.ffmpeg_bin
    if args.ffprobe_bin:
        config.ffprobe_bin = args.ffprobe_bin
    if args.no_screenshot:
        config.screenshot_enabled = False
    if args.grim_bin:
        config.grim_bin = args.grim_bin
    if args.spectacle_bin:
        config.spectacle_bin = args.spectacle_bin
    if args.screenshot_output_dir:
        config.screenshot_output_dir = Path(args.screenshot_output_dir).expanduser()
    if args.screenshot_wayland_display:
        config.screenshot_wayland_display = args.screenshot_wayland_display
    if args.screenshot_require_approval:
        config.screenshot_require_approval = True
    if args.no_live_context:
        config.live_context_enabled = False
    if args.live_context_budget_tokens is not None:
        config.live_context_budget_tokens = clamp_budget_tokens(
            args.live_context_budget_tokens
        )
    if args.zsh_history:
        config.zsh_history_path = Path(args.zsh_history).expanduser()
    if args.ps_bin:
        config.ps_bin = args.ps_bin
    if args.qdbus_bin:
        config.qdbus_bin = args.qdbus_bin
    if args.no_layer:
        config.layer_enabled = False
    if args.layer_bin:
        config.layer_bin = Path(args.layer_bin).expanduser()
    if args.no_layer_notify:
        config.layer_notify_enabled = False
    if args.layer_socket:
        config.layer_socket = Path(args.layer_socket).expanduser()
    if args.layer_preview_chars is not None:
        config.layer_preview_chars = max(1, args.layer_preview_chars)
    if args.live_context_timeout is not None:
        config.live_context_timeout = args.live_context_timeout
    if args.no_sandbox:
        config.sandbox_enabled = False
    if args.tokifactor_image_require_approval:
        config.tokifactor_image_require_approval = True
    try:
        asyncio.run(_serve(config))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
