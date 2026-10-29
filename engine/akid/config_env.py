"""Build a GatewayConfig from the profile file and process environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from engine.aki_config import (
    ConfigError,
    default_username_path,
    ensure_config,
    legacy_state_dir,
    resolve_config_path,
    state_dir,
    sync_channel_identity,
)
from engine.layer_process import default_layer_binary
from engine.avatar import default_avatar_path
from engine.live_context import clamp_budget_tokens
from engine.member_identity import (
    IdentityError,
    IdentityRegistry,
    backup_migration_state,
    ensure_manifest,
    is_member_id,
)
from .config import GatewayConfig
from .constants import (
    DEFAULT_APPROVAL_TIMEOUT,
    DEFAULT_QUEUE_MAX,
)
from .paths import (
    default_backend_dir,
    default_socket_path,
)



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
