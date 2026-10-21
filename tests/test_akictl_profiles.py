from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from engine import akictl
from engine.aki_config import AkiConfig, atomic_write, template_text


class AkictlProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_up_starts_proxy_once_then_gateway_and_waits(self) -> None:
        config = mock.Mock()
        with mock.patch(
            "engine.akictl._preflight_profile",
            return_value=(config, self.root / "fuyu.sock"),
        ), mock.patch("engine.akictl._systemctl") as systemctl, mock.patch(
            "engine.akictl._wait_port", return_value=True
        ), mock.patch(
            "engine.akictl._health_ready", return_value={"ready": True}
        ) as health:
            systemctl.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = akictl._profile_up("fuyu")
        self.assertEqual(result, 0)
        calls = [call.args for call in systemctl.call_args_list]
        self.assertEqual(
            calls,
            [
                ("enable", "--now", "akid-proxy@fuyu.service"),
                ("enable", "--now", "akid@fuyu.service"),
            ],
        )
        health.assert_called_once()

    def test_down_disables_gateway_and_proxy_without_deleting_files(self) -> None:
        with mock.patch("engine.akictl._systemctl") as systemctl:
            systemctl.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            result = akictl._profile_down("fuyu")
        self.assertEqual(result, 0)
        systemctl.assert_called_once_with(
            "disable",
            "--now",
            "akid@fuyu.service",
            "akid-proxy@fuyu.service",
        )

    def test_profiles_lists_default_and_configured_profiles(self) -> None:
        home = self.root / "home"
        (home / ".aki").mkdir(parents=True)
        atomic_write(home / ".aki" / "aki.conf", template_text())
        atomic_write(home / ".aki" / "fuyu.conf", template_text(profile="fuyu"))
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), mock.patch(
            "engine.akictl._unit_active", side_effect=lambda unit: "fuyu" in unit
        ), mock.patch("engine.akictl.print") as printer:
            result = akictl._profiles()
        self.assertEqual(result, 0)
        rendered = "\n".join(str(call.args[0]) for call in printer.call_args_list if call.args)
        self.assertIn("aki", rendered)
        self.assertIn("fuyu", rendered)
        self.assertIn("running", rendered)

    def test_invalid_profile_is_rejected_by_main(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = akictl.main(["--profile", "Bad Profile", "status"])
        self.assertEqual(result, 2)
        self.assertIn("profile must match", stderr.getvalue())

    def test_preflight_rejects_missing_conversation(self) -> None:
        home = self.root / "home"
        (home / ".aki").mkdir(parents=True)
        atomic_write(home / ".aki" / "fuyu.conf", template_text(profile="fuyu"))
        backend = self.root / "backend"
        (backend / "agents").mkdir(parents=True)
        (backend / "agents" / "agent.json").write_text(
            '{"id": "agent-fuyu"}\n', encoding="utf-8"
        )
        with mock.patch.dict(
            os.environ,
            {"HOME": str(home), "AKI_LETTA_BACKEND_DIR": str(backend)},
            clear=False,
        ), mock.patch("engine.akictl._unit_active", return_value=False), mock.patch(
            "engine.akictl._socket_conflict", return_value=None
        ), mock.patch("engine.akictl._port_in_use", return_value=False):
            # Point the profile at the fixture agent before loading.
            from engine.aki_config import set_key

            set_key(home / ".aki" / "fuyu.conf", "gateway", "agent_id", "agent-fuyu")
            with self.assertRaisesRegex(Exception, "conversation"):
                akictl._preflight_profile("fuyu")


if __name__ == "__main__":
    unittest.main()
