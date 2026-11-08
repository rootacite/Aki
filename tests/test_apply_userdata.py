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
APPLY_SCRIPT = ROOT / "apply_userdata.py"
SYNC_SCRIPT = ROOT / "scripts" / "sync-persona.py"


class ApplyUserdataCliTests(unittest.TestCase):
    def test_help_contains_agent_flags(self) -> None:
        res = subprocess.run(
            [sys.executable, str(APPLY_SCRIPT), "--help"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--aki", res.stdout)
        self.assertIn("--fuyu", res.stdout)
        self.assertIn("--haru", res.stdout)
        self.assertIn("--natsu", res.stdout)
        self.assertIn("--diff", res.stdout)
        self.assertIn("-y", res.stdout)

    def test_fuyu_already_in_sync(self) -> None:
        res = subprocess.run(
            [sys.executable, str(APPLY_SCRIPT), "--fuyu"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("fuyu", res.stdout)
        self.assertIn("完全一致", res.stdout)

    def test_cancel_does_not_modify(self) -> None:
        # Run aki with "n" input to cancel
        res = subprocess.run(
            [sys.executable, str(APPLY_SCRIPT), "--aki"],
            input="n\n",
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("用户取消同步", res.stdout)

    def test_sync_only_touches_persona_and_preserves_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_backend = temp_path / "lc-local-backend"
            fake_agent_id = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
            fake_memory = fake_backend / "memfs" / fake_agent_id / "memory"
            fake_memory.mkdir(parents=True, exist_ok=True)

            # Copy userdata into fake_memory
            shutil.copytree(ROOT / "userdata", fake_memory, dirs_exist_ok=True)
            # Add dynamic memory files
            human_file = fake_memory / "human.md"
            human_file.write_text("## Critical Human Memory\nFact: Acite is Master.\n", encoding="utf-8")
            profile_dir = fake_memory / "profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            project_memory = profile_dir / "notes.md"
            project_memory.write_text("## Important Project Memory\n", encoding="utf-8")

            # Init git
            subprocess.run(["git", "init", "-q", str(fake_memory)], check=True)
            subprocess.run(["git", "-C", str(fake_memory), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(fake_memory), "config", "user.email", "test@test.com"], check=True)
            subprocess.run(["git", "-C", str(fake_memory), "add", "."], check=True)
            subprocess.run(["git", "-C", str(fake_memory), "commit", "-qm", "initial"], check=True)

            # Make local persona.md in fake_memory different from project
            persona_target = fake_memory / "persona.md"
            persona_target.write_text("Old deployed persona\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(fake_memory), "commit", "-aqm", "old persona"], check=True)

            env = dict(os.environ)
            env["AKI_LETTA_BACKEND_DIR"] = str(fake_backend)

            # Run with "n" -> should not change
            res_cancel = subprocess.run(
                [sys.executable, str(APPLY_SCRIPT), "--aki"],
                input="n\n",
                env=env,
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(res_cancel.returncode, 0)
            self.assertEqual(persona_target.read_text(encoding="utf-8"), "Old deployed persona\n")
            self.assertEqual(human_file.read_text(encoding="utf-8"), "## Critical Human Memory\nFact: Acite is Master.\n")
            self.assertEqual(project_memory.read_text(encoding="utf-8"), "## Important Project Memory\n")


if __name__ == "__main__":
    unittest.main()
