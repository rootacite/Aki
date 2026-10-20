"""Persistent MQTT broker settings for akid."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_MQTT_HOST = "192.168.3.1"
DEFAULT_MQTT_PORT = 1883
STATE_VERSION = 1


def default_mqtt_state_path(home: Path | None = None) -> Path:
    configured = os.environ.get("AKI_MQTT_STATE")
    if configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".local" / "share" / "aki" / "mqtt.json"


def load_mqtt_server(state_path: Path | None = None) -> tuple[str | None, int | None]:
    path = (state_path or default_mqtt_state_path()).expanduser()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    host = data.get("host")
    port = data.get("port")
    if not isinstance(host, str) or not host.strip():
        host = None
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        port = None
    return (host.strip() if host else None, port)


def save_mqtt_server(host: str, port: int, state_path: Path | None = None) -> Path:
    path = (state_path or default_mqtt_state_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    payload = {"version": STATE_VERSION, "host": host, "port": port}
    fd, temp_name = tempfile.mkstemp(prefix=".mqtt.", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return path
