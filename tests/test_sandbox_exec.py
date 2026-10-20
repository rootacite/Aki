from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.writable import probe_bwrap

ROOT = Path(__file__).resolve().parent.parent
SANDBOX_EXEC = ROOT / "engine" / "sandbox_exec.py"


@unittest.skipUnless(probe_bwrap()[0], "bwrap unavailable")
class SandboxExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="akid-sbx-", dir=str(Path.home() / ".local" / "share")))
        self.home = self.base / "home"
        self.home.mkdir()
        self.allowed = self.base / "allowed"
        self.allowed.mkdir()
        self.denied = self.base / "denied"
        self.denied.mkdir()
        self.letta_home = self.base / "letta"
        self.letta_home.mkdir()
        self.backend = self.base / "backend"
        self.backend.mkdir()
        self.state = self.base / "writable.json"
        self.state.write_text(
            json.dumps(
                {
                    "version": 1,
                    "revision": 1,
                    "roots": [{"path": str(self.allowed), "added_at": "now"}],
                }
            )
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def _command(self, *command: str, bwrap: str = "bwrap") -> list[str]:
        return [
            sys.executable,
            str(SANDBOX_EXEC),
            "--state",
            str(self.state),
            "--bwrap",
            bwrap,
            "--letta-home",
            str(self.letta_home),
            "--backend-dir",
            str(self.backend),
            "--",
            *command,
        ]

    def test_registered_dir_is_writable_and_other_dir_is_not(self) -> None:
        script = "import sys; open(sys.argv[1], 'w').write('ok')"
        allowed_file = self.allowed / "file.txt"
        denied_file = self.denied / "file.txt"
        allowed = subprocess.run(
            self._command(sys.executable, "-c", script, str(allowed_file)),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(allowed_file.read_text(), "ok")

        denied = subprocess.run(
            self._command(sys.executable, "-c", script, str(denied_file)),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(denied_file.exists())
        self.assertIn("Read-only file system", denied.stderr)

    def test_reads_remain_available(self) -> None:
        result = subprocess.run(
            self._command("/bin/cat", "/etc/hostname"),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())

    def test_cache_environment_is_redirected(self) -> None:
        script = "import os; print(os.environ.get('XDG_CACHE_HOME','')); print(os.environ.get('AKI_SANDBOXED',''))"
        result = subprocess.run(
            self._command(sys.executable, "-c", script),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        cache, sandboxed = result.stdout.splitlines()[:2]
        self.assertIn(str(Path.home() / ".local" / "share" / "tmp" / "akid"), cache)
        self.assertEqual(sandboxed, "1")

    def test_missing_registered_root_fails_closed(self) -> None:
        self.state.write_text(
            json.dumps(
                {
                    "version": 1,
                    "revision": 2,
                    "roots": [{"path": str(self.base / "missing"), "added_at": "now"}],
                }
            )
        )
        result = subprocess.run(
            self._command("/bin/true"),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unavailable", result.stderr)

    def test_missing_bwrap_fails_closed(self) -> None:
        result = subprocess.run(
            self._command("/bin/true", bwrap="/definitely/missing/bwrap"),
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("sandbox unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
