"""Gateway defaults, tool schemas, and the NO_REPLY marker."""

from __future__ import annotations

import logging
import re
from engine.execution_policy import (
    LOCK_ACQUIRE_TOOL,
    WAIT_OTHER_TOOL,
    WAIT_USER_TOOL,
)
from engine.tool_ownership import EXCLUSIVE_TOOL_NAMES
from engine.tokifactor import (
    MAX_MESSAGE_ATTACHMENTS,
    MAX_MESSAGE_IMAGE_BYTES,
)
from engine.protocol import PROTOCOL_VERSION


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

TOKIFACTOR_IMAGE_TOOL_NAME = "tokifactor_send_image"

TOKIFACTOR_FILE_TOOL_NAME = "tokifactor_send_file"

CAMERA_TOOL_NAME = "camera_take_photo"

SCREENSHOT_TOOL_NAME = "desktop_take_screenshot"

IMAGE_GENERATION_TOOL_NAME = "generate_image"

COMFYUI_STARTUP_TOOL_NAME = "comfyui_startup"

COMFYUI_GENERATE_TOOL_NAME = "comfyui_generate"

COMFYUI_END_TOOL_NAME = "comfyui_end"

COMFYUI_TOOL_NAMES = frozenset(
    {
        COMFYUI_STARTUP_TOOL_NAME,
        COMFYUI_GENERATE_TOOL_NAME,
        COMFYUI_END_TOOL_NAME,
    }
)

SEARCH_CHAT_HISTORY_TOOL_NAME = "search_chat_history"

EXTERNAL_TOOL_NAMES = {
    TOKIFACTOR_IMAGE_TOOL_NAME,
    TOKIFACTOR_FILE_TOOL_NAME,
    CAMERA_TOOL_NAME,
    SCREENSHOT_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    *COMFYUI_TOOL_NAMES,
    LOCK_ACQUIRE_TOOL,
    WAIT_OTHER_TOOL,
    WAIT_USER_TOOL,
    SEARCH_CHAT_HISTORY_TOOL_NAME,
} | set(EXCLUSIVE_TOOL_NAMES)

HIDDEN_TOOL_NAMES = frozenset(
    {WAIT_OTHER_TOOL, WAIT_USER_TOOL, SEARCH_CHAT_HISTORY_TOOL_NAME}
)

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


TOKIFACTOR_FILE_TOOL = {
    "name": TOKIFACTOR_FILE_TOOL_NAME,
    "label": "Send tokifactor file",
    "description": (
        "Send an existing local file to the shared tokifactor channel using "
        "tokifactor's file-transfer protocol. The path must be absolute and "
        "readable; the containing directory does not need to be writable or "
        "registered. The transfer is recorded in group or solo channel history "
        "as a short message containing only the sender and file path; it does "
        "not start any member turn. This tool does not require the execution "
        "lock. Returns the transfer id, byte count, chunk count, and name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to an existing local file.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 600000,
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


SEARCH_CHAT_HISTORY_TOOL = {
    "name": SEARCH_CHAT_HISTORY_TOOL_NAME,
    "label": "Search chat history",
    "description": (
        "Search this profile's own chat history by case-insensitive substring. "
        "Includes visible group chat, the current solo conversation, and "
        "archived solo sessions. Solo results are private and must never be "
        "quoted or disclosed to other members. This tool is hidden from the "
        "user and does not require the execution lock."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Text to find in message bodies.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 8,
                "description": "Maximum matches to return, newest first.",
            },
            "scope": {
                "type": "string",
                "enum": ["all", "group", "solo"],
                "default": "all",
                "description": "Which chat records to search.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "auto_background": False,
    "timeout_ms": 10_000,
}


CAPABILITIES = {
    "streaming": True,
    "approvals": True,
    "history": True,
    "memory_readonly": True,
    "multiclient": True,
    "writable_dirs": True,
    "tokifactor_image_tool": True,
    "tokifactor_file_tool": True,
    "camera_tool": True,
    "screenshot_tool": True,
    "image_generation_tool": True,
    "comfyui_tools": True,
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
