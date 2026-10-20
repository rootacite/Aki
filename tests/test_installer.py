from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_installer():
    spec = importlib.util.spec_from_file_location("aki_installer", ROOT / "install-daemon.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.backend = self.root / "backend"
        (self.backend / "agents").mkdir(parents=True)
        (self.backend / "agents" / "agent.json").write_text(
            json.dumps({"id": "test-agent", "model": "test/model"}), encoding="utf-8"
        )
        self.env = mock.patch.dict(os.environ, {"AKI_LETTA_BACKEND_DIR": str(self.backend)})
        self.env.start()
        self.installer = load_installer()

    def tearDown(self) -> None:
        self.env.stop()
        self.tempdir.cleanup()

    def test_conversation_bootstrap_writes_schema_not_chat(self) -> None:
        target = self.installer.ensure_conversation("test-agent", "default", self.root)
        manifest = json.loads((target / "manifest.json").read_text("utf-8"))
        conversation = json.loads((target / "conversation.json").read_text("utf-8"))
        lines = (target / "messages.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(conversation["id"], "default")
        self.assertEqual(conversation["agent_id"], "test-agent")
        self.assertEqual(len(lines), 1)
        session = json.loads(lines[0])
        self.assertEqual(session["type"], "session")
        self.assertNotIn("message_type", session)

    def test_conversation_bootstrap_is_idempotent(self) -> None:
        first = self.installer.ensure_conversation("test-agent", "default", self.root)
        second = self.installer.ensure_conversation("test-agent", "default", self.root)
        self.assertEqual(first, second)
        self.assertEqual(len((first / "messages.jsonl").read_text("utf-8").splitlines()), 1)

    def test_install_akictl_creates_idempotent_symlink(self) -> None:
        repo = self.root / "repo"
        (repo / "bin").mkdir(parents=True)
        entry = repo / "bin" / "akictl"
        entry.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        install_dir = self.root / "local-bin"
        first = self.installer.install_akictl(repo, install_dir=install_dir)
        second = self.installer.install_akictl(repo, install_dir=install_dir)
        self.assertEqual(first, second)
        self.assertTrue(first.is_symlink())
        self.assertEqual(first.resolve(), entry.resolve())
        self.assertTrue(os.access(entry, os.X_OK))

    def test_install_akictl_rejects_existing_file(self) -> None:
        repo = self.root / "repo"
        (repo / "bin").mkdir(parents=True)
        (repo / "bin" / "akictl").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        install_dir = self.root / "local-bin"
        install_dir.mkdir()
        conflict = install_dir / "akictl"
        conflict.write_text("not ours\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.installer.install_akictl(repo, install_dir=install_dir)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "not ours\n")

    def test_uninstall_akictl_only_removes_own_link(self) -> None:
        repo = self.root / "repo"
        (repo / "bin").mkdir(parents=True)
        entry = repo / "bin" / "akictl"
        entry.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        install_dir = self.root / "local-bin"
        installed = self.installer.install_akictl(repo, install_dir=install_dir)
        self.assertTrue(self.installer.uninstall_akictl(repo, install_dir=install_dir))
        self.assertFalse(installed.exists())

        other = self.root / "other-akictl"
        other.write_text("other\n", encoding="utf-8")
        installed.symlink_to(other)
        self.assertFalse(self.installer.uninstall_akictl(repo, install_dir=install_dir))
        self.assertTrue(installed.is_symlink())

        installed.unlink()
        installed.write_text("regular file\n", encoding="utf-8")
        self.assertFalse(self.installer.uninstall_akictl(repo, install_dir=install_dir))
        self.assertTrue(installed.exists())

    def test_unit_has_required_hardening(self) -> None:
        unit = self.installer.service_unit(
            python_bin="/usr/bin/python3",
            repo=ROOT,
            agent_id="test-agent",
            conversation_id="default",
            letta_bin="/usr/bin/letta",
            socket_path=Path("/tmp/aki/akid.sock"),
        )
        for fragment in (
            "Restart=on-failure",
            "RestartSec=2",
            "TimeoutStopSec=15",
            "KillMode=control-group",
            "RuntimeDirectory=aki",
            "RuntimeDirectoryMode=0700",
            "UMask=0077",
            'Environment="XDG_RUNTIME_DIR=%t"',
            'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"',
        ):
            self.assertIn(fragment, unit)
        self.assertNotIn("AKI_", unit)


if __name__ == "__main__":
    unittest.main()
