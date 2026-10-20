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
SCRIPT = ROOT / "scripts" / "sync-persona.py"
ALLOWLIST = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)


class SyncPersonaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "userdata"
        self.memory = self.root / "memory"
        self.backups = self.root / "backups"
        shutil.copytree(ROOT / "userdata", self.source, ignore=shutil.ignore_patterns("README.md", "MANIFEST.json"))
        self.memory.mkdir()
        for rel in ALLOWLIST:
            target = self.memory / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.source / rel, target)
        subprocess.run(["git", "init", "-q", str(self.memory)], check=True)
        subprocess.run(["git", "-C", str(self.memory), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(self.memory), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.memory), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.memory), "commit", "-qm", "initial"], check=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_sync(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--source",
                str(self.source),
                "--memory-dir",
                str(self.memory),
                "--backup-dir",
                str(self.backups),
                *args,
            ],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_sync_updates_backs_up_and_commits(self) -> None:
        persona = self.source / "persona.md"
        persona.write_text(persona.read_text(encoding="utf-8") + "\n<!-- test change -->\n", encoding="utf-8")
        result = self.run_sync("--message", "test persona sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("test change", (self.memory / "persona.md").read_text(encoding="utf-8"))
        backups = list(self.backups.glob("userdata-sync-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "persona.md").is_file())
        self.assertTrue((backups[0] / "manifest.json").is_file())
        log = subprocess.check_output(
            ["git", "-C", str(self.memory), "log", "-1", "--pretty=%s"], text=True
        ).strip()
        self.assertEqual(log, "test persona sync")

    def test_dry_run_does_not_write(self) -> None:
        persona = self.source / "persona.md"
        persona.write_text(persona.read_text(encoding="utf-8") + "\n<!-- dry run -->\n", encoding="utf-8")
        before = (self.memory / "persona.md").read_text(encoding="utf-8")
        result = self.run_sync("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would update", result.stdout)
        self.assertEqual((self.memory / "persona.md").read_text(encoding="utf-8"), before)

    def test_private_files_are_ignored(self) -> None:
        (self.source / "human.md").write_text("private\n", encoding="utf-8")
        (self.source / "profile").mkdir()
        (self.source / "profile" / "identity.md").write_text("private\n", encoding="utf-8")
        result = self.run_sync("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("private-looking files are ignored", result.stderr)
        self.assertFalse((self.memory / "human.md").exists())
        self.assertFalse((self.memory / "profile" / "identity.md").exists())

    def test_invalid_frontmatter_is_rejected(self) -> None:
        (self.source / "persona.md").write_text("# no frontmatter\n", encoding="utf-8")
        result = self.run_sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing frontmatter", result.stderr)


if __name__ == "__main__":
    unittest.main()
