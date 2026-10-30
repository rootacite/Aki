"""Parse a profile file into an AkiConfig."""

from __future__ import annotations

import configparser
import json
import os
import tempfile
from urllib.parse import urlsplit
from engine.member_profile import truncate_member_description
from .models import (
    AkiConfig,
    ChannelIdentity,
    ComfyUI,
    ConfigError,
    DEFAULT_AGENT_ID,
    ImageGeneration,
    ModelRoute,
    ToolPolicy,
    Upstream,
    _APIS,
    _AUTH_PROVIDERS,
    _EXTRA_BODY_RESERVED,
    _KNOWN_SECTIONS,
    _TOOLSETS,
    profile_from_config_path,
    read_agent_identity,
    validate_profile_name,
)



def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise



def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[method-assign]
    return parser



def _read_parser(path: Path) -> configparser.ConfigParser:
    parser = _parser()
    try:
        loaded = parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if not loaded:
        raise ConfigError(f"cannot read {path}")
    return parser



def _bool(parser: configparser.ConfigParser, section: str, key: str) -> bool:
    raw = _require(parser, section, key).strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigError(f"[{section}] {key} must be true or false")



def _bool_or(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    raw = parser.get(section, key, fallback="true" if default else "false").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ConfigError(f"[{section}] {key} must be true or false")



def _require(parser: configparser.ConfigParser, section: str, key: str) -> str:
    if not parser.has_section(section) or not parser.has_option(section, key):
        raise ConfigError(f"[{section}] {key} is required")
    return parser.get(section, key)



def _int(parser: configparser.ConfigParser, section: str, key: str, *, minimum: int, maximum: int | None = None) -> int:
    raw = _require(parser, section, key).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"[{section}] {key} is out of range")
    return value



def _int_or(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    raw = parser.get(section, key, fallback=str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"[{section}] {key} is out of range")
    return value



def _list(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    for line in raw.splitlines() or [raw]:
        for part in line.split(","):
            text = part.strip()
            if text:
                items.append(text)
    return tuple(items)



def _json_object(
    parser: configparser.ConfigParser,
    section: str,
    key: str,
    *,
    reserved: frozenset[str] | set[str] = frozenset(),
) -> dict[str, object]:
    raw = parser.get(section, key, fallback="").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"[{section}] {key} must be a JSON object")
    blocked = sorted(reserved.intersection(value))
    if blocked:
        raise ConfigError(
            f"[{section}] {key} must not override {', '.join(blocked)}"
        )
    return value



def parse_upstream_proxy(value: str) -> str:
    """Return a normalized ``http://host:port`` or an empty string."""
    text = value.strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ConfigError("[proxy] upstream_proxy must look like http://host:port")
    if parsed.username or parsed.password:
        raise ConfigError("[proxy] upstream_proxy does not take credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigError("[proxy] upstream_proxy must not include a path")
    port = parsed.port or 80
    if not 1 <= port <= 65535:
        raise ConfigError("[proxy] upstream_proxy port is out of range")
    host = parsed.hostname
    return f"http://{host}:{port}"



def parse_listen(value: str) -> tuple[str, int]:
    text = value.strip()
    host, separator, port_text = text.rpartition(":")
    if not separator or not host or not port_text:
        raise ConfigError("[proxy] listen must look like 127.0.0.1:7721")
    if host != "127.0.0.1":
        raise ConfigError("[proxy] listen must bind 127.0.0.1")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError("[proxy] listen port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("[proxy] listen port is out of range")
    return host, port



def join_url(base: str, path: str) -> str:
    if not path.startswith("/"):
        raise ConfigError("model path must start with /")
    parsed = urlsplit(base.strip())
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ConfigError(f"upstream base_url is invalid: {base}")
    return base.strip().rstrip("/") + path



def load_config(path: Path, *, profile: str | None = None) -> AkiConfig:
    parser = _read_parser(path)
    for section in parser.sections():
        if section in _KNOWN_SECTIONS:
            continue
        if section.startswith("upstream.") or section.startswith("model."):
            name = section.split(".", 1)[1].strip()
            if not name or "/" in name:
                raise ConfigError(f"invalid section [{section}]")
            continue
        raise ConfigError(f"unknown section [{section}]")

    proxy_host, proxy_port = parse_listen(_require(parser, "proxy", "listen"))
    upstreams: dict[str, Upstream] = {}
    for section in parser.sections():
        if not section.startswith("upstream."):
            continue
        name = section.split(".", 1)[1]
        base_url = _require(parser, section, "base_url").strip()
        if not base_url:
            raise ConfigError(f"[{section}] base_url is required")
        join_url(base_url, "/")
        auth = parser.get(section, "auth", fallback="").strip()
        if auth not in _AUTH_PROVIDERS:
            raise ConfigError(
                f"[{section}] auth must be empty or grok-oauth"
            )
        api_key = parser.get(section, "api_key", fallback="").strip()
        upstreams[name] = Upstream(
            name=name,
            base_url=base_url,
            api_key=api_key,
            use_proxy=_bool_or(parser, section, "use_proxy", True),
            auth=auth,
        )

    models: list[ModelRoute] = []
    for section in parser.sections():
        if not section.startswith("model."):
            continue
        model_id = section.split(".", 1)[1]
        upstream_name = _require(parser, section, "upstream").strip()
        upstream = upstreams.get(upstream_name)
        if upstream is None:
            raise ConfigError(f"[{section}] upstream {upstream_name} is not defined")
        api = _require(parser, section, "api").strip()
        if api not in _APIS:
            raise ConfigError(f"[{section}] api must be openai-completions or openai-responses")
        route_path = _require(parser, section, "path").strip()
        upstream_model = parser.get(section, "upstream_model", fallback="").strip() or model_id
        inputs = _list(parser.get(section, "input", fallback="text"))
        if not inputs or any(item not in ("text", "image") for item in inputs):
            raise ConfigError(f"[{section}] input must be text and/or image")
        models.append(
            ModelRoute(
                model_id=model_id,
                upstream_name=upstream_name,
                upstream_model=upstream_model,
                api=api,
                path=route_path,
                context_window=_int(parser, section, "context_window", minimum=1),
                max_tokens=_int(parser, section, "max_tokens", minimum=1),
                inputs=inputs,
                url=join_url(upstream.base_url, route_path),
                api_key=upstream.api_key,
                use_proxy=upstream.use_proxy,
                auth=upstream.auth,
                extra_body=_json_object(
                    parser,
                    section,
                    "extra_body",
                    reserved=_EXTRA_BODY_RESERVED,
                ),
            )
        )

    image: ImageGeneration | None = None
    if parser.has_section("image"):
        image_upstream_name = _require(parser, "image", "upstream").strip()
        image_upstream = upstreams.get(image_upstream_name)
        if image_upstream is None:
            raise ConfigError(
                f"[image] upstream {image_upstream_name} is not defined"
            )
        image_path = _require(parser, "image", "path").strip()
        if not image_path.startswith("/"):
            raise ConfigError("[image] path must start with /")
        output_dir = _require(parser, "image", "output_dir").strip()
        if not output_dir.startswith("/") and not output_dir.startswith("~/"):
            raise ConfigError("[image] output_dir must be absolute or start with ~/")
        aspect_ratio = (
            parser.get("image", "aspect_ratio", fallback="").strip() or "1:1"
        )
        if aspect_ratio not in (
            "1:1",
            "16:9",
            "4:3",
            "3:2",
            "2:3",
            "3:4",
            "9:16",
            "21:9",
        ):
            raise ConfigError("[image] aspect_ratio is not supported")
        try:
            image_timeout = float(
                parser.get("image", "timeout", fallback="180").strip() or "180"
            )
        except ValueError as exc:
            raise ConfigError("[image] timeout must be a number") from exc
        if image_timeout <= 0:
            raise ConfigError("[image] timeout must be positive")
        image = ImageGeneration(
            enabled=_bool(parser, "image", "enabled"),
            upstream_name=image_upstream_name,
            model=_require(parser, "image", "model").strip(),
            path=image_path,
            base_url=image_upstream.base_url,
            url=join_url(image_upstream.base_url, image_path),
            api_key=image_upstream.api_key,
            use_proxy=image_upstream.use_proxy,
            output_dir=output_dir,
            require_approval=_bool(parser, "image", "require_approval"),
            timeout=image_timeout,
            max_bytes=_int(
                parser,
                "image",
                "max_bytes",
                minimum=1,
                maximum=64 * 1024 * 1024,
            ),
            aspect_ratio=aspect_ratio,
            prompt_optimizer=_bool_or(
                parser, "image", "prompt_optimizer", False
            ),
            aigc_watermark=_bool_or(parser, "image", "aigc_watermark", False),
        )

    comfyui: ComfyUI | None = None
    if parser.has_section("comfyui"):
        endpoint = (
            parser.get("comfyui", "endpoint", fallback="").strip()
            or "http://127.0.0.1:8188"
        )
        parsed_endpoint = urlsplit(endpoint)
        if (
            parsed_endpoint.scheme not in ("http", "https")
            or not parsed_endpoint.netloc
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ConfigError(
                "[comfyui] endpoint must be an http(s) URL without query or fragment"
            )
        output_dir = (
            parser.get("comfyui", "output_dir", fallback="").strip()
            or "/tmp/aki/comfyui"
        )
        if not output_dir.startswith("/") and not output_dir.startswith("~/"):
            raise ConfigError(
                "[comfyui] output_dir must be absolute or start with ~/"
            )
        try:
            startup_timeout = float(
                parser.get("comfyui", "startup_timeout", fallback="180").strip()
                or "180"
            )
        except ValueError as exc:
            raise ConfigError("[comfyui] startup_timeout must be a number") from exc
        if startup_timeout <= 0:
            raise ConfigError("[comfyui] startup_timeout must be positive")
        try:
            shutdown_timeout = float(
                parser.get("comfyui", "shutdown_timeout", fallback="15").strip()
                or "15"
            )
        except ValueError as exc:
            raise ConfigError("[comfyui] shutdown_timeout must be a number") from exc
        if shutdown_timeout <= 0:
            raise ConfigError("[comfyui] shutdown_timeout must be positive")
        comfyui = ComfyUI(
            startup_script=(
                parser.get("comfyui", "startup_script", fallback="").strip()
            ),
            endpoint=endpoint.rstrip("/"),
            output_dir=output_dir,
            require_approval=_bool_or(
                parser, "comfyui", "require_approval", True
            ),
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
        )

    tool_policy = ToolPolicy()
    if parser.has_section("tools"):
        toolset = parser.get("tools", "toolset", fallback="auto").strip() or "auto"
        if toolset not in _TOOLSETS:
            raise ConfigError(
                "[tools] toolset must be one of " + ", ".join(sorted(_TOOLSETS))
            )
        write_roots = _list(parser.get("tools", "write_roots", fallback=""))
        for root in write_roots:
            if not root.startswith("/") and not root.startswith("~/"):
                raise ConfigError(
                    "[tools] write_roots entries must be absolute or start with ~/"
                )
        tool_policy = ToolPolicy(
            toolset=toolset,
            allow=_list(parser.get("tools", "allow", fallback="")),
            deny=_list(parser.get("tools", "deny", fallback="")),
            write_roots=write_roots,
            explicit=True,
        )

    resolved_profile = validate_profile_name(profile) if profile else profile_from_config_path(path)
    agent_id = _require(parser, "gateway", "agent_id").strip() or DEFAULT_AGENT_ID
    username = (
        parser.get("gateway", "username", fallback="").strip()
        or parser.get("channel", "username", fallback="").strip()
    )
    channel_name = parser.get("channel", "name", fallback="").strip()
    channel_id = parser.get("channel", "id", fallback="").strip() or agent_id
    channel_description = truncate_member_description(
        parser.get("channel", "description", fallback="").strip()
    )
    if not channel_name or not channel_description:
        agent_name, agent_description = read_agent_identity(agent_id)
        channel_name = channel_name or agent_name
        channel_description = channel_description or truncate_member_description(
            agent_description
        )
    aliases = _list(parser.get("channel", "aliases", fallback=""))
    if channel_name and channel_name not in aliases:
        aliases = (channel_name, *aliases)
    return AkiConfig(
        path=path,
        profile=resolved_profile,
        agent_id=agent_id,
        conversation=_require(parser, "gateway", "conversation").strip() or "default",
        username=username,
        letta_bin=_require(parser, "gateway", "letta_bin").strip() or "letta",
        socket=_require(parser, "gateway", "socket").strip(),
        sandbox=_bool(parser, "gateway", "sandbox"),
        mqtt_enabled=_bool(parser, "mqtt", "enabled"),
        mqtt_host=_require(parser, "mqtt", "host").strip(),
        mqtt_port=_int(parser, "mqtt", "port", minimum=1, maximum=65535),
        mqtt_topic=_require(parser, "mqtt", "topic").strip(),
        mqtt_magic=_require(parser, "mqtt", "magic").strip(),
        writable_dirs=_list(parser.get("writable", "dirs", fallback="")),
        writable_revision=_int(parser, "writable", "revision", minimum=0) if parser.has_option("writable", "revision") else 0,
        mcp_native_tools=_bool(parser, "mcp", "native_tools"),
        mcp_excluded=_list(parser.get("mcp", "excluded", fallback="")),
        mcp_max_tools=_int(parser, "mcp", "max_tools", minimum=0),
        mcp_timeout=float(_require(parser, "mcp", "timeout").strip() or "300"),
        camera_enabled=_bool(parser, "camera", "enabled"),
        camera_require_approval=_bool(parser, "camera", "require_approval"),
        camera_device=_require(parser, "camera", "device").strip() or "/dev/video0",
        camera_width=_int(parser, "camera", "width", minimum=1),
        camera_height=_int(parser, "camera", "height", minimum=1),
        camera_output_dir=_require(parser, "camera", "output_dir").strip() or "/tmp/aki/camera",
        screenshot_enabled=_bool(parser, "screenshot", "enabled"),
        screenshot_require_approval=_bool(parser, "screenshot", "require_approval"),
        screenshot_output_dir=_require(parser, "screenshot", "output_dir").strip() or "/tmp/aki/screenshots",
        screenshot_wayland_display=_require(parser, "screenshot", "wayland_display").strip(),
        live_enabled=_bool(parser, "live", "enabled"),
        live_budget_tokens=_int(parser, "live", "budget_tokens", minimum=1),
        live_timeout=float(_require(parser, "live", "timeout").strip() or "3"),
        avatar_path=_require(parser, "avatar", "path").strip(),
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_token=_require(parser, "proxy", "token").strip(),
        upstream_proxy=parse_upstream_proxy(parser.get("proxy", "upstream_proxy", fallback="")),
        upstreams=upstreams,
        models=tuple(models),
        channel=ChannelIdentity(
            name=channel_name,
            id=channel_id,
            aliases=aliases,
            description=channel_description,
        ),
        channel_wait_for=_list(parser.get("channel", "wait_for", fallback="")),
        userdata=parser.get("gateway", "userdata", fallback="").strip() or "userdata",
        schedule_enabled=_bool_or(parser, "schedule", "enabled", True),
        schedule_tick_seconds=_int_or(
            parser, "schedule", "tick_seconds", 15, minimum=1, maximum=3600
        ),
        schedule_missed_grace_seconds=_int_or(
            parser, "schedule", "missed_grace_seconds", 600, minimum=0
        ),
        schedule_max_records=_int_or(
            parser, "schedule", "max_records", 256, minimum=1
        ),
        presence_enabled=_bool_or(parser, "presence", "enabled", True),
        presence_session_id=parser.get("presence", "session_id", fallback="").strip(),
        presence_loginctl_bin=(
            parser.get("presence", "loginctl_bin", fallback="").strip() or "loginctl"
        ),
        presence_poll_seconds=_int_or(
            parser, "presence", "poll_seconds", 15, minimum=1, maximum=600
        ),
        presence_idle_reset_seconds=_int_or(
            parser, "presence", "idle_reset_seconds", 300, minimum=1
        ),
        presence_work_warn_seconds=_int_or(
            parser, "presence", "work_warn_seconds", 7200, minimum=60
        ),
        presence_work_repeat_seconds=_int_or(
            parser, "presence", "work_repeat_seconds", 3600, minimum=60
        ),
        presence_night_start=(
            parser.get("presence", "night_start", fallback="").strip() or "00:30"
        ),
        presence_night_end=(
            parser.get("presence", "night_end", fallback="").strip() or "05:30"
        ),
        presence_night_history_days=_int_or(
            parser, "presence", "night_history_days", 30, minimum=1
        ),
        presence_return_idle_seconds=_int_or(
            parser, "presence", "return_idle_seconds", 2700, minimum=60
        ),
        presence_return_debounce_seconds=_int_or(
            parser, "presence", "return_debounce_seconds", 10, minimum=0
        ),
        solo_transcript_budget_tokens=_int_or(
            parser, "solo", "transcript_budget_tokens", 64 * 1024, minimum=1024
        ),
        solo_prompt_file=parser.get("solo", "prompt_file", fallback="").strip(),
        tool_policy=tool_policy,
        image=image,
        comfyui=comfyui,
    )
