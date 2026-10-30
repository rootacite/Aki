"""Configuration data classes, profile names, and path helpers."""

from __future__ import annotations

import json
import os
import re
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path


DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"

DEFAULT_PROFILE = "aki"

PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


_KNOWN_SECTIONS = {
    "gateway",
    "tools",
    "mqtt",
    "writable",
    "mcp",
    "camera",
    "screenshot",
    "image",
    "comfyui",
    "live",
    "schedule",
    "presence",
    "solo",
    "avatar",
    "proxy",
    "channel",
}

_APIS = {"openai-completions", "openai-responses"}

_AUTH_PROVIDERS = {"", "grok-oauth"}

_TOOLSETS = {"auto", "letta", "none", "default", "codex"}

_EXTRA_BODY_RESERVED = {"model", "messages"}



class ConfigError(ValueError):
    """A profile config is missing a required value or contains an invalid one."""



def validate_profile_name(profile: str) -> str:
    name = profile.strip()
    if not PROFILE_PATTERN.fullmatch(name):
        raise ConfigError(
            "profile must match [a-z0-9][a-z0-9_-]{0,31}"
        )
    return name



def provider_id_for_profile(profile: str) -> str:
    profile = validate_profile_name(profile)
    return DEFAULT_PROFILE if profile == DEFAULT_PROFILE else f"aki-{profile}"



def profile_from_config_path(path: Path) -> str:
    stem = Path(path).stem
    return stem if PROFILE_PATTERN.fullmatch(stem) else DEFAULT_PROFILE



@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    api_key: str
    use_proxy: bool = True
    auth: str = ""



@dataclass(frozen=True)
class ImageGeneration:
    """Optional text-to-image provider configuration for one profile."""

    enabled: bool
    upstream_name: str
    model: str
    path: str
    base_url: str
    url: str
    api_key: str
    use_proxy: bool
    output_dir: str
    require_approval: bool
    timeout: float
    max_bytes: int
    aspect_ratio: str
    prompt_optimizer: bool
    aigc_watermark: bool

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_key.strip())


@dataclass(frozen=True)
class ComfyUI:
    """Optional managed ComfyUI process and local ComfyUI API settings."""

    startup_script: str = ""
    endpoint: str = "http://127.0.0.1:8188"
    output_dir: str = "/tmp/aki/comfyui"
    require_approval: bool = True
    startup_timeout: float = 180.0
    shutdown_timeout: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.startup_script.strip())



@dataclass(frozen=True)
class ChannelIdentity:
    """User-facing identity for one akid instance on the shared channel."""

    name: str = ""
    id: str = ""
    aliases: tuple[str, ...] = ()
    description: str = ""



@dataclass(frozen=True)
class ModelRoute:
    model_id: str
    upstream_name: str
    upstream_model: str
    api: str
    path: str
    context_window: int
    max_tokens: int
    inputs: tuple[str, ...]
    url: str
    api_key: str
    use_proxy: bool = True
    auth: str = ""
    extra_body: dict[str, object] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.auth == "grok-oauth"



@dataclass(frozen=True)
class ToolPolicy:
    """Per-profile tool surface and write roots.

    ``explicit`` is true only when the profile has a ``[tools]`` section.  An
    implicit policy keeps the historical auto-selected toolset so Aki and Fuyu
    behave exactly as before.
    """

    toolset: str = "auto"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    explicit: bool = False

    @property
    def allow_set(self) -> frozenset[str]:
        return frozenset(self.allow)



@dataclass
class AkiConfig:
    path: Path
    profile: str
    agent_id: str
    conversation: str
    username: str
    letta_bin: str
    socket: str
    sandbox: bool
    mqtt_enabled: bool
    mqtt_host: str
    mqtt_port: int
    mqtt_topic: str
    mqtt_magic: str
    writable_dirs: tuple[str, ...]
    writable_revision: int
    mcp_native_tools: bool
    mcp_excluded: tuple[str, ...]
    mcp_max_tools: int
    mcp_timeout: float
    camera_enabled: bool
    camera_require_approval: bool
    camera_device: str
    camera_width: int
    camera_height: int
    camera_output_dir: str
    screenshot_enabled: bool
    screenshot_require_approval: bool
    screenshot_output_dir: str
    screenshot_wayland_display: str
    live_enabled: bool
    live_budget_tokens: int
    live_timeout: float
    avatar_path: str
    proxy_host: str
    proxy_port: int
    proxy_token: str
    upstream_proxy: str
    upstreams: dict[str, Upstream]
    models: tuple[ModelRoute, ...]
    channel: ChannelIdentity = field(default_factory=ChannelIdentity)
    channel_wait_for: tuple[str, ...] = ()
    userdata: str = "userdata"
    schedule_enabled: bool = True
    schedule_tick_seconds: int = 15
    schedule_missed_grace_seconds: int = 600
    schedule_max_records: int = 256
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
    solo_transcript_budget_tokens: int = 64 * 1024
    solo_prompt_file: str = ""
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)
    image: ImageGeneration | None = None
    comfyui: ComfyUI | None = None

    @property
    def enabled_models(self) -> tuple[ModelRoute, ...]:
        return tuple(model for model in self.models if model.enabled)

    @property
    def proxy_base_url(self) -> str:
        return f"http://{self.proxy_host}:{self.proxy_port}/v1"

    @property
    def provider_id(self) -> str:
        return provider_id_for_profile(self.profile)

    @property
    def provider_mod_name(self) -> str:
        return f"aki-proxy-{self.profile}.ts"



def state_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".aki"



def default_config_path(
    home: Path | None = None, *, profile: str | None = None
) -> Path:
    if profile is not None:
        name = validate_profile_name(profile)
        return state_dir(home) / f"{name}.conf"
    configured = os.environ.get("AKI_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return state_dir(home) / f"{DEFAULT_PROFILE}.conf"



def profile_config_path(profile: str, home: Path | None = None) -> Path:
    return default_config_path(home, profile=profile)



def discover_profiles(home: Path | None = None) -> dict[str, Path]:
    """Return valid ``~/.aki/<profile>.conf`` files, keyed by profile."""
    root = state_dir(home)
    profiles: dict[str, Path] = {}
    try:
        paths = sorted(root.glob("*.conf"))
    except OSError:
        paths = []
    for path in paths:
        if not path.is_file() or not PROFILE_PATTERN.fullmatch(path.stem):
            continue
        profiles[path.stem] = path
    return dict(sorted(profiles.items(), key=lambda item: (item[0] != DEFAULT_PROFILE, item[0])))



def resolve_config_path(
    profile: str | None = None,
    home: Path | None = None,
) -> tuple[str, Path]:
    """Resolve a profile name and config path.

    An explicit profile wins over ``AKI_CONFIG``; otherwise ``AKI_CONFIG`` is
    the explicit path override and the default profile is ``aki``.
    """
    if profile is not None:
        name = validate_profile_name(profile)
        return name, profile_config_path(name, home)
    configured = os.environ.get("AKI_CONFIG")
    path = Path(configured).expanduser() if configured else default_config_path(home)
    return profile_from_config_path(path), path



def default_socket_path(
    profile: str = DEFAULT_PROFILE, home: Path | None = None
) -> Path:
    configured = os.environ.get("AKI_SOCKET")
    if configured:
        return Path(configured).expanduser()
    name = validate_profile_name(profile)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = Path(runtime) / "aki"
    else:
        root = Path(f"/tmp/aki-{os.getuid()}")
    filename = "akid.sock" if name == DEFAULT_PROFILE else f"{name}.sock"
    return root / filename



def default_username_path(agent_id: str, home: Path | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id)
    return state_dir(home) / "usernames" / f"{safe}.txt"



def read_agent_identity(
    agent_id: str, home: Path | None = None
) -> tuple[str, str]:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    backend = (
        Path(configured).expanduser()
        if configured
        else (home or Path.home()) / ".letta" / "lc-local-backend"
    )
    try:
        paths = (backend / "agents").glob("*.json")
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



def legacy_state_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local" / "share" / "aki"
