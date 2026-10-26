"""Checks for the vendored Letta lock, patches and installed runtime."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBMODULE = ROOT / "vendor" / "letta-code"
LOCK = ROOT / "vendor" / "letta-code.lock.json"
PATCHES = ROOT / "patches" / "letta-code"
RUNTIME = Path.home() / ".local" / "lib" / "aki" / "letta-code" / "current"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LettaVendorTests(unittest.TestCase):
    def test_submodule_matches_lock(self) -> None:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        head = subprocess.run(
            ["git", "-C", str(SUBMODULE), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(head, lock["commit"])
        self.assertEqual(lock["tag"], "v0.34.24")

    def test_patch_hashes_match_lock(self) -> None:
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        actual = {
            path.name: sha256(path) for path in sorted(PATCHES.glob("*.patch"))
        }
        self.assertEqual(actual, lock["patches"])

    def test_installed_runtime_when_present(self) -> None:
        if not RUNTIME.exists():
            self.skipTest("vendored runtime is not installed on this machine")
        lock = json.loads(LOCK.read_text(encoding="utf-8"))
        bundle = RUNTIME / "letta.js"
        manifest = json.loads(
            (RUNTIME / "aki-runtime-manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(bundle.is_file())
        self.assertEqual(manifest["commit"], lock["commit"])
        self.assertEqual(manifest["version"], lock["version"])
        node = os.environ.get("AKI_NODE_BIN", "/usr/bin/node")
        result = subprocess.run(
            [node, str(bundle), "--version"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn(lock["version"], result.stdout)


if __name__ == "__main__":
    unittest.main()
