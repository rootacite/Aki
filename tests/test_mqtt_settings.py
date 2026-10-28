from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.akid import build_config_from_env
from engine.aki_config import atomic_write, set_key, template_text
from engine.mqtt_settings import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    load_mqtt_server,
    save_mqtt_server,
)


class MqttSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state = self.root / "aki.conf"
        self.cwd = self.root / "repo"
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_save_load_and_permissions(self) -> None:
        save_mqtt_server("10.0.0.5", 1884, self.state)
        self.assertEqual(load_mqtt_server(self.state), ("10.0.0.5", 1884))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        text = self.state.read_text(encoding="utf-8")
        self.assertIn("host = 10.0.0.5", text)
        self.assertIn("port = 1884", text)

    def test_defaults_are_local_broker(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AKI_")}
        env["AKI_CONFIG"] = str(self.state)
        env["AKI_CWD"] = str(self.cwd)
        env["HOME"] = str(self.root)
        env["XDG_RUNTIME_DIR"] = str(self.root / "run")
        with mock.patch.dict(os.environ, env, clear=True):
            config = build_config_from_env()
        self.assertEqual(config.mqtt_host, DEFAULT_MQTT_HOST)
        self.assertEqual(config.mqtt_port, DEFAULT_MQTT_PORT)
        self.assertEqual(config.mqtt_host, "127.0.0.1")
        self.assertEqual(config.mqtt_port, 1883)

    def test_conf_wins_over_environment(self) -> None:
        atomic_write(self.state, template_text())
        set_key(self.state, "mqtt", "host", "10.0.0.9")
        set_key(self.state, "mqtt", "port", "2883")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AKI_")}
        env.update(
            {
                "AKI_CONFIG": str(self.state),
                "AKI_CWD": str(self.cwd),
                "HOME": str(self.root),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
                "AKI_MQTT_HOST": "env.example",
                "AKI_MQTT_PORT": "1999",
            }
        )
        with mock.patch.dict(os.environ, env, clear=True):
            config = build_config_from_env()
        self.assertEqual(config.mqtt_host, "10.0.0.9")
        self.assertEqual(config.mqtt_port, 2883)


if __name__ == "__main__":
    unittest.main()
