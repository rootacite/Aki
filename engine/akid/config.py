"""GatewayConfig and the Letta stream-json command line."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from engine.aki_config import ComfyUI, ToolPolicy
from engine.layer_process import default_layer_binary
from engine.avatar import default_avatar_path
from engine.channel import (
    DEFAULT_MAGIC,
    TEXT_TOPIC,
)
from engine.live_context import (
    DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    DEFAULT_BUDGET_TOKENS as DEFAULT_LIVE_CONTEXT_BUDGET_TOKENS,
)
from engine.mqtt_settings import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
)
from .constants import (
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
    IDEMPOTENCY_TTL,
)
from .paths import default_backend_dir


REFLECTION_TRIGGER = "step-count"
REFLECTION_STEP_COUNT = 25


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
    channel_mention_only: bool = False
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
    tokifactor_file_require_approval: bool = False
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
    comfyui: ComfyUI | None = None
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
        "--no-system-info-reminder",
        "--reflection-trigger",
        REFLECTION_TRIGGER,
        "--reflection-step-count",
        str(REFLECTION_STEP_COUNT),
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
