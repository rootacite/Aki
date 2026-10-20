"""Single user configuration file for akid: ``~/.aki/aki.conf``."""

from __future__ import annotations

import configparser
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"

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
}
_APIS = {"openai-completions", "openai-responses"}


class ConfigError(ValueError):
    """``aki.conf`` is missing a required value or contains an invalid one."""


@dataclass(frozen=True)
class Upstream:
    name: str
    base_url: str
    api_key: str
    use_proxy: bool = True


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

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class AkiConfig:
    path: Path
    agent_id: str
    conversation: str
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

    @property
    def enabled_models(self) -> tuple[ModelRoute, ...]:
        return tuple(model for model in self.models if model.enabled)

    @property
    def proxy_base_url(self) -> str:
        return f"http://{self.proxy_host}:{self.proxy_port}/v1"


def default_config_path(home: Path | None = None) -> Path:
    configured = os.environ.get("AKI_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".aki" / "aki.conf"


def legacy_state_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local" / "share" / "aki"


def template_text(agent_id: str = DEFAULT_AGENT_ID) -> str:
    return f"""[gateway]
agent_id = {agent_id}
conversation = default
letta_bin = letta
# 留空则用 $XDG_RUNTIME_DIR/aki/akid.sock
socket =
sandbox = true

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


def load_config(path: Path) -> AkiConfig:
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
        upstreams[name] = Upstream(
            name=name,
            base_url=base_url,
            api_key=_require(parser, section, "api_key").strip(),
            use_proxy=_bool_or(parser, section, "use_proxy", True),
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
            )
        )

    return AkiConfig(
        path=path,
        agent_id=_require(parser, "gateway", "agent_id").strip() or DEFAULT_AGENT_ID,
        conversation=_require(parser, "gateway", "conversation").strip() or "default",
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
        atomic_write(path, template_text())
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
        atomic_write(path, template_text())
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
        atomic_write(path, template_text())
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


def ensure_config(path: Path, *, migrate_from: Path | None = None, agent_id: str = DEFAULT_AGENT_ID) -> AkiConfig:
    created = False
    if not path.exists():
        atomic_write(path, template_text(agent_id))
        created = True
    if created and migrate_from is not None:
        _migrate_legacy(path, migrate_from)
    ensure_proxy_token(path)
    return load_config(path)


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
