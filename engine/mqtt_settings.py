"""Persistent MQTT broker settings for akid. Stored in ~/.aki/aki.conf."""

from __future__ import annotations

from pathlib import Path

from engine.aki_config import default_config_path, read_mqtt, write_mqtt

DEFAULT_MQTT_HOST = "127.0.0.1"
DEFAULT_MQTT_PORT = 1883


def default_mqtt_state_path(home: Path | None = None) -> Path:
    return default_config_path(home)


def load_mqtt_server(state_path: Path | None = None) -> tuple[str | None, int | None]:
    path = (state_path or default_mqtt_state_path()).expanduser()
    return read_mqtt(path)


def save_mqtt_server(host: str, port: int, state_path: Path | None = None) -> Path:
    path = (state_path or default_mqtt_state_path()).expanduser()
    write_mqtt(path, host, port)
    return path
