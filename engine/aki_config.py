"""Profile-aware configuration files for akid (``~/.aki/<profile>.conf``)."""

from __future__ import annotations

import configparser
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
DEFAULT_PROFILE = "aki"
PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_KNOWN_SECTIONS = {
    "gateway",
    "mqtt",
    "writable",
    "mcp",
    "camera",
    "screenshot",
    "live",
    "avatar",
    "proxy",
    "channel",
}
_APIS = {"openai-completions", "openai-responses"}
_AUTH_PROVIDERS = {"", "grok-oauth"}


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

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.auth == "grok-oauth"


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


def template_text(
    agent_id: str = DEFAULT_AGENT_ID, *, profile: str = DEFAULT_PROFILE
) -> str:
    name = validate_profile_name(profile)
    channel_name = "Aki" if name == DEFAULT_PROFILE else name.replace("-", " ").title()
    channel_aliases = (
        "Aki,aki,秋,小秋,秋（aki）"
        if name == DEFAULT_PROFILE
        else f"{channel_name},{name}"
    )
    channel_description = (
        "Aki（秋 / 小秋），住在用户电脑里的赛博生命体。"
        if name == DEFAULT_PROFILE
        else ""
    )
    return f"""[gateway]
agent_id = {agent_id}
conversation = default
username = Master
letta_bin = letta
# 留空则用 $XDG_RUNTIME_DIR/aki/akid.sock（非 aki profile 使用 <profile>.sock）
socket =
sandbox = true

[channel]
name = {channel_name}
id = {profile}
aliases = {channel_aliases}
description = {channel_description}

[mqtt]
enabled = true
host = 192.168.3.1
port = 1883
topic = tokifactor/text
magic = akid-default-channel

[writable]
# 多行，一行一个目录。akictl allow / disallow 只改这一键。
revision = 0
dirs =

[mcp]
native_tools = true
excluded = local
max_tools = 64
timeout = 300

[camera]
enabled = true
require_approval = true
device = /dev/video0
width = 1280
height = 720
output_dir = /tmp/aki/camera

[screenshot]
enabled = true
require_approval = false
output_dir = /tmp/aki/screenshots
wayland_display =

[live]
enabled = true
budget_tokens = 65536
timeout = 3

[avatar]
path =

[proxy]
listen = 127.0.0.1:7721
# Letta 调用本机代理时的令牌。留空则首次启动生成，之后保持不变。
token =
# 访问上游模型用的 HTTP 代理。留空则直连。
upstream_proxy = http://192.168.4.1:7890

[upstream.deepseek]
base_url = https://api.deepseek.com
api_key =

[upstream.xai]
base_url = https://api.x.ai/v1
api_key =

[upstream.gemini]
base_url = https://generativelanguage.googleapis.com/v1beta/openai
api_key =

[upstream.openrouter]
base_url = https://openrouter.ai/api/v1
api_key =

[model.deepseek-flash]
upstream = deepseek
upstream_model = deepseek-flash
api = openai-completions
path = /chat/completions
context_window = 1000000
max_tokens = 384000
input = text,image

[model.grok-4.7]
upstream = xai
upstream_model = grok-4.7
api = openai-responses
path = /responses
context_window = 500000
max_tokens = 500000
input = text,image

[model.gemini-2.5-flash]
upstream = gemini
upstream_model = gemini-2.5-flash
api = openai-completions
path = /chat/completions
context_window = 1048576
max_tokens = 65536
input = text,image

[model.gemini-3.8-flash]
upstream = openrouter
upstream_model = google/gemini-3.8-flash
api = openai-completions
path = /chat/completions
context_window = 1048576
max_tokens = 65536
input = text,image
"""


def _template_for(path: Path) -> str:
    return template_text(profile=profile_from_config_path(path))


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


def _list(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    for line in raw.splitlines() or [raw]:
        for part in line.split(","):
            text = part.strip()
            if text:
                items.append(text)
    return tuple(items)


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
            )
        )

    resolved_profile = validate_profile_name(profile) if profile else profile_from_config_path(path)
    agent_id = _require(parser, "gateway", "agent_id").strip() or DEFAULT_AGENT_ID
    username = (
        parser.get("gateway", "username", fallback="").strip()
        or parser.get("channel", "username", fallback="").strip()
    )
    channel_name = parser.get("channel", "name", fallback="").strip()
    channel_id = parser.get("channel", "id", fallback="").strip() or agent_id
    channel_description = parser.get("channel", "description", fallback="").strip()
    if not channel_name or not channel_description:
        agent_name, agent_description = read_agent_identity(agent_id)
        channel_name = channel_name or agent_name
        channel_description = channel_description or agent_description
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
    )


def _format_value(value: str) -> list[str]:
    lines = value.split("\n")
    if len(lines) == 1:
        return [f"{lines[0]}"]
    rendered = [lines[0]]
    rendered.extend(f"    {line}" for line in lines[1:])
    return rendered


def set_key(path: Path, section: str, key: str, value: str) -> None:
    """Replace one key without rewriting the rest of the file."""
    if not path.exists():
        atomic_write(path, f"[{section}]\n{key} =\n")
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    header = f"[{section}]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    new_value_lines = [f"{key} = {_format_value(value)[0]}"]
    extra = _format_value(value)[1:]
    new_value_lines.extend(extra)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        lines.extend(new_value_lines)
        atomic_write(path, "\n".join(lines) + "\n")
        return
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("[") and lines[index].rstrip().endswith("]"):
            end = index
            break
    key_at = None
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=") or stripped.startswith(f"{key}\t"):
            key_at = index
            break
    if key_at is None:
        lines[end:end] = new_value_lines
        atomic_write(path, "\n".join(lines) + "\n")
        return
    remove_until = key_at + 1
    while remove_until < end and (lines[remove_until].startswith(" ") or lines[remove_until].startswith("\t")):
        remove_until += 1
    lines[key_at:remove_until] = new_value_lines
    atomic_write(path, "\n".join(lines) + "\n")


def read_writable(path: Path) -> tuple[int, tuple[str, ...]]:
    if not path.exists():
        return 0, ()
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        import json

        try:
            data = json.loads(text)
        except ValueError:
            return 0, ()
        revision = data.get("revision", 0) if isinstance(data, dict) else 0
        if not isinstance(revision, int) or revision < 0:
            revision = 0
        dirs: list[str] = []
        for item in (data.get("roots") if isinstance(data, dict) else []) or []:
            if isinstance(item, str):
                dirs.append(item)
            elif isinstance(item, dict) and isinstance(item.get("path"), str):
                dirs.append(item["path"])
        return revision, tuple(dirs)
    try:
        config = load_config(path)
    except ConfigError:
        parser = _read_parser(path)
        revision = 0
        if parser.has_option("writable", "revision"):
            raw = parser.get("writable", "revision").strip()
            revision = int(raw) if raw.isdigit() else 0
        return revision, _list(parser.get("writable", "dirs", fallback=""))
    return config.writable_revision, config.writable_dirs


def write_writable(path: Path, dirs: list[str], revision: int) -> None:
    if not path.exists():
        atomic_write(path, _template_for(path))
    set_key(path, "writable", "revision", str(revision))
    body = "\n".join(dirs)
    set_key(path, "writable", "dirs", body)


def read_mqtt(path: Path) -> tuple[str | None, int | None]:
    if not path.exists():
        return None, None
    parser = _read_parser(path)
    if not parser.has_section("mqtt"):
        return None, None
    host = parser.get("mqtt", "host", fallback="").strip() or None
    port_text = parser.get("mqtt", "port", fallback="").strip()
    try:
        port = int(port_text)
    except ValueError:
        port = None
    if port is not None and not 1 <= port <= 65535:
        port = None
    return host, port


def write_mqtt(path: Path, host: str, port: int) -> None:
    if not path.exists():
        atomic_write(path, _template_for(path))
    set_key(path, "mqtt", "host", host)
    set_key(path, "mqtt", "port", str(port))


def read_mcp_excluded(path: Path) -> tuple[str, ...] | None:
    if not path.exists():
        return None
    parser = _read_parser(path)
    if not parser.has_option("mcp", "excluded"):
        return None
    return _list(parser.get("mcp", "excluded"))


def write_mcp_excluded(path: Path, excluded: list[str]) -> None:
    if not path.exists():
        atomic_write(path, _template_for(path))
    set_key(path, "mcp", "excluded", ", ".join(excluded))


def ensure_proxy_token(path: Path) -> str:
    config = load_config(path)
    if config.proxy_token:
        return config.proxy_token
    token = secrets.token_urlsafe(32)
    set_key(path, "proxy", "token", token)
    return token


def _move_legacy(source: Path, dest_dir: Path) -> None:
    if not source.exists():
        return
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = dest_dir / source.name
    if target.exists():
        target = dest_dir / f"{source.stem}-{source.stat().st_mtime_ns}{source.suffix}"
    shutil.move(str(source), str(target))


def ensure_config(
    path: Path,
    *,
    migrate_from: Path | None = None,
    agent_id: str = DEFAULT_AGENT_ID,
    profile: str | None = None,
) -> AkiConfig:
    created = False
    resolved_profile = (
        validate_profile_name(profile) if profile else profile_from_config_path(path)
    )
    if not path.exists():
        atomic_write(path, template_text(agent_id, profile=resolved_profile))
        created = True
    if created and migrate_from is not None:
        _migrate_legacy(path, migrate_from)
    ensure_proxy_token(path)
    return load_config(path, profile=resolved_profile)


def _migrate_legacy(path: Path, source_dir: Path) -> None:
    import json

    mqtt_path = source_dir / "mqtt.json"
    if mqtt_path.exists():
        try:
            payload = json.loads(mqtt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            host = payload.get("host")
            port = payload.get("port")
            if isinstance(host, str) and host.strip() and isinstance(port, int) and 1 <= port <= 65535:
                write_mqtt(path, host.strip(), port)
    writable_path = source_dir / "writable-dirs.json"
    if writable_path.exists():
        try:
            payload = json.loads(writable_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        dirs: list[str] = []
        revision = 0
        if isinstance(payload, dict):
            raw_revision = payload.get("revision", 0)
            if isinstance(raw_revision, int) and raw_revision >= 0:
                revision = raw_revision
            for item in payload.get("roots") or []:
                if isinstance(item, str) and item.strip():
                    dirs.append(item.strip())
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    dirs.append(item["path"])
        write_writable(path, dirs, revision)
    mcp_path = source_dir / "mcp-native.json"
    if mcp_path.exists():
        try:
            payload = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("excluded"), list):
            excluded = [str(item).strip() for item in payload["excluded"] if str(item).strip()]
            write_mcp_excluded(path, excluded)
    migrated = path.parent / "migrated"
    for name in ("mqtt.json", "writable-dirs.json", "mcp-native.json"):
        _move_legacy(source_dir / name, migrated)
