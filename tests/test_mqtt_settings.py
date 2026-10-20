from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.akid import build_config_from_env
from engine.mqtt_settings import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    load_mqtt_server,
    save_mqtt_server,
)


class MqttSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = Path(self.tempdir.name) / "mqtt.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_save_load_and_permissions(self) -> None:
        save_mqtt_server("10.0.0.5", 1884, self.state)
        self.assertEqual(load_mqtt_server(self.state), ("10.0.0.5", 1884))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        data = json.loads(self.state.read_text())
        self.assertEqual(data["host"], "10.0.0.5")
        self.assertEqual(data["port"], 1884)

    def test_defaults_are_lan_broker(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AKI_MQTT_")}
        env["AKI_MQTT_STATE"] = str(self.state)
        with mock.patch.dict(os.environ, env, clear=True):
            config = build_config_from_env()
        self.assertEqual(config.mqtt_host, DEFAULT_MQTT_HOST)
        self.assertEqual(config.mqtt_port, DEFAULT_MQTT_PORT)
        self.assertEqual(config.mqtt_host, "192.168.3.1")
        self.assertEqual(config.mqtt_port, 1883)

    def test_persisted_settings_override_environment(self) -> None:
        save_mqtt_server("10.0.0.9", 2883, self.state)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AKI_MQTT_")}
        env.update(
            {
                "AKI_MQTT_STATE": str(self.state),
                "AKI_MQTT_HOST": "env.example",
                "AKI_MQTT_PORT": "1999",
            }
        )
        with mock.patch.dict(os.environ, env, clear=True):
            config = build_config_from_env()
        self.assertEqual(config.mqtt_host, "10.0.0.9")
        self.assertEqual(config.mqtt_port, 2883)

    def test_environment_used_without_persisted_settings(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AKI_MQTT_")}
        env.update(
            {
                "AKI_MQTT_STATE": str(self.state),
                "AKI_MQTT_HOST": "env.example",
                "AKI_MQTT_PORT": "1999",
            }
        )
        with mock.patch.dict(os.environ, env, clear=True):
            config = build_config_from_env()
        self.assertEqual(config.mqtt_host, "env.example")
        self.assertEqual(config.mqtt_port, 1999)


if __name__ == "__main__":
    unittest.main()
