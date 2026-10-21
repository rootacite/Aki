from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.akid import build_config_from_env
from engine.aki_config import (
    ConfigError,
    set_key,
    atomic_write,
    default_socket_path,
    discover_profiles,
    load_config,
    resolve_config_path,
    template_text,
    validate_profile_name,
)


class ProfileConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_profiles_are_discovered_and_default_first(self) -> None:
        root = self.home / ".aki"
        atomic_write(root / "fuyu.conf", template_text(profile="fuyu"))
        atomic_write(root / "aki.conf", template_text())
        atomic_write(root / "Bad Name.conf", template_text())
        profiles = discover_profiles(self.home)
        self.assertEqual(list(profiles), ["aki", "fuyu"])
        self.assertEqual(profiles["aki"], root / "aki.conf")
        self.assertEqual(profiles["fuyu"], root / "fuyu.conf")

    def test_profile_name_validation(self) -> None:
        self.assertEqual(validate_profile_name("fuyu-2"), "fuyu-2")
        for value in ("", "Fuyu", "-fuyu", "a" * 33, "aki/evil"):
            with self.assertRaises(ConfigError):
                validate_profile_name(value)

    def test_profile_wins_over_explicit_config_env(self) -> None:
        explicit = self.home / "elsewhere" / "custom.conf"
        with mock.patch.dict(os.environ, {"AKI_CONFIG": str(explicit)}, clear=False):
            name, path = resolve_config_path("fuyu", self.home)
            self.assertEqual(name, "fuyu")
            self.assertEqual(path, self.home / ".aki" / "fuyu.conf")
            name, path = resolve_config_path(None, self.home)
            self.assertEqual(name, "custom")
            self.assertEqual(path, explicit)

    def test_default_socket_is_profile_aware(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": str(self.home / "run")},
            clear=False,
        ):
            os.environ.pop("AKI_SOCKET", None)
            self.assertEqual(
                default_socket_path("aki", self.home),
                self.home / "run" / "aki" / "akid.sock",
            )
            self.assertEqual(
                default_socket_path("fuyu", self.home),
                self.home / "run" / "aki" / "fuyu.sock",
            )

    def test_channel_falls_back_to_letta_agent_record(self) -> None:
        path = self.home / "fuyu.conf"
        atomic_write(path, template_text(profile="fuyu"))
        # Remove the explicit channel values to exercise agent-record fallback.
        text = path.read_text(encoding="utf-8")
        text = text.replace("name = Fuyu\n", "name =\n")
        text = text.replace("id = fuyu\n", "id =\n")
        text = text.replace("description = \n", "description =\n")
        atomic_write(path, text)
        backend = self.home / "backend"
        (backend / "agents").mkdir(parents=True)
        (backend / "agents" / "agent.json").write_text(
            json.dumps(
                {
                    "id": "agent-fuyu",
                    "name": "冬",
                    "description": "agent record description",
                }
            ),
            encoding="utf-8",
        )
        from engine.aki_config import set_key

        set_key(path, "gateway", "agent_id", "agent-fuyu")
        with mock.patch.dict(
            os.environ,
            {
                "AKI_CONFIG": str(path),
                "AKI_LETTA_BACKEND_DIR": str(backend),
                "XDG_RUNTIME_DIR": str(self.home / "run"),
            },
            clear=False,
        ):
            os.environ.pop("AKI_SOCKET", None)
            config = build_config_from_env()
        self.assertEqual(config.profile, "fuyu")
        self.assertEqual(config.channel_id, "agent-fuyu")
        self.assertEqual(config.channel_name, "冬")
        self.assertEqual(config.channel_description, "agent record description")
        self.assertIn("冬", config.channel_aliases)

    def test_channel_defaults_to_agent_and_aliases_include_name(self) -> None:
        path = self.home / ".aki" / "fuyu.conf"
        atomic_write(path, template_text(profile="fuyu"))
        config = load_config(path, profile="fuyu")
        self.assertEqual(config.profile, "fuyu")
        self.assertEqual(config.provider_id, "aki-fuyu")
        self.assertEqual(config.channel.id, "fuyu")
        self.assertEqual(config.username, "Master")
        self.assertIn("Fuyu", config.channel.aliases)
        self.assertTrue(config.provider_mod_name.endswith("aki-proxy-fuyu.ts"))

    def test_username_is_read_from_profile_and_migrates_legacy_file(self) -> None:
        path = self.home / "aki.conf"
        atomic_write(path, template_text())
        text = path.read_text(encoding="utf-8").replace("username = Master\n", "")
        atomic_write(path, text)
        cwd = self.home / "repo"
        (cwd / "userdata").mkdir(parents=True)
        (cwd / "userdata" / "username.txt").write_text("OldName\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "AKI_CONFIG": str(path),
                "AKI_CWD": str(cwd),
                "XDG_RUNTIME_DIR": str(self.home / "run"),
            },
            clear=False,
        ):
            os.environ.pop("AKI_SOCKET", None)
            migrated = build_config_from_env()
        self.assertEqual(migrated.username, "OldName")
        self.assertEqual(migrated.username_source, "legacy-userdata")
        self.assertEqual(migrated.username_path, cwd / "userdata" / "username.txt")

        set_key(path, "gateway", "username", "Master")
        with mock.patch.dict(
            os.environ,
            {
                "AKI_CONFIG": str(path),
                "AKI_CWD": str(cwd),
                "XDG_RUNTIME_DIR": str(self.home / "run"),
            },
            clear=False,
        ):
            os.environ.pop("AKI_SOCKET", None)
            configured = build_config_from_env()
        self.assertEqual(configured.username, "Master")
        self.assertEqual(configured.username_source, "profile-config")
        self.assertIsNone(configured.username_path)


if __name__ == "__main__":
    unittest.main()
