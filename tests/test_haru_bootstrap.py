from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("git"), "git is required for MemFS bootstrap")
class HaruBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "userdata3"
        shutil.copytree(ROOT / "userdata3", self.source)
        (self.source / "identity.json").unlink(missing_ok=True)
        self.backend = self.home / ".letta" / "lc-local-backend"
        self.config = self.home / ".aki" / "haru.conf"
        self.settings = self.home / ".letta" / "settings.json"
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "LETTA_HOME": str(self.home / ".letta"),
                "LETTA_LOCAL_BACKEND_DIR": str(self.backend),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
            }
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_bootstrap(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootstrap-member.py"),
                "--config",
                str(self.config),
                "--source",
                str(self.source),
                "--json",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_bootstrap_creates_records_once_and_is_idempotent(self) -> None:
        created = self.run_bootstrap()
        self.assertEqual(created["status"], "created")
        agent_id = created["agent_id"]
        self.assertTrue(self.config.is_file())
        self.assertTrue(created["identity"]["id"].startswith("legacy_"))
        self.assertEqual(created["identity"]["name"], "Haru")
        self.assertTrue(Path(created["memory_dir"], "persona.md").is_file())
        self.assertTrue(Path(created["memory_dir"], "MEMORY.md").is_file())
        self.assertFalse(
            Path(created["memory_dir"], "MEMORY.md").read_text(encoding="utf-8").startswith("---")
        )
        self.assertTrue(Path(created["conversation_dir"], "system-prompt.json").is_file())
        self.assertTrue(Path(created["agent_file"]).is_file())

        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        entry = next(item for item in settings["agents"] if item["agentId"] == agent_id)
        self.assertTrue(entry["memfs"])
        self.assertEqual(entry["systemPromptPreset"], "custom")

        agent = json.loads(Path(created["agent_file"]).read_text(encoding="utf-8"))
        self.assertEqual(agent["model"], "aki-haru/gemini-3.8-flash")
        self.assertEqual(agent["model_settings"]["provider_type"], "aki-haru")
        self.assertEqual(agent["tags"][-1], "profile:haru")
        self.assertEqual(agent["system"].count("{CORE_MEMORY}"), 1)

        config_text = self.config.read_text(encoding="utf-8")
        log_before = subprocess.run(
            ["git", "-C", created["memory_dir"], "log", "--oneline"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        second = self.run_bootstrap()
        self.assertEqual(second["status"], "exists")
        self.assertFalse(second["changed"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), config_text)
        log_after = subprocess.run(
            ["git", "-C", created["memory_dir"], "log", "--oneline"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(log_after, log_before)


if __name__ == "__main__":
    unittest.main()
