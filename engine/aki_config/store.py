"""Update config keys and migrate legacy state files."""

from __future__ import annotations

import secrets
import shutil
from .load import (
    _list,
    _read_parser,
    atomic_write,
    load_config,
)
from .models import (
    ConfigError,
    DEFAULT_AGENT_ID,
    profile_from_config_path,
    validate_profile_name,
)
from .template import (
    _template_for,
    template_text,
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



def sync_channel_identity(
    path: Path,
    *,
    member_id: str,
    name: str,
    aliases: tuple[str, ...] | list[str],
) -> None:
    """Mirror an authoritative identity manifest into ``[channel]``."""
    if not path.is_file():
        return
    set_key(path, "channel", "id", member_id)
    set_key(path, "channel", "name", name)
    set_key(path, "channel", "aliases", ",".join(str(item) for item in aliases))



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
