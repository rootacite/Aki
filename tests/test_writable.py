from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.protocol import ErrorCode, RpcError
from engine.writable import WritableRegistry, probe_bwrap


class WritableRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="akid-writable-", dir=str(Path.home() / ".local" / "share")))
        self.home = self.base / "home"
        self.home.mkdir()
        self.letta_home = self.home / ".letta"
        self.letta_home.mkdir()
        self.backend = self.letta_home / "lc-local-backend"
        self.backend.mkdir()
        self.state = self.home / ".local" / "share" / "aki" / "writable-dirs.json"
        self.registry = WritableRegistry(
            state_path=self.state,
            home=self.home,
            letta_home=self.letta_home,
            backend_dir=self.backend,
        )
        self.registry.load()

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def test_defaults_and_internal_roots(self) -> None:
        self.assertIn(Path("/tmp"), self.registry.defaults)
        self.assertIn(self.home / ".local" / "share" / "tmp", self.registry.defaults)
        self.assertEqual(self.registry.internal, (self.letta_home, self.backend))
        self.registry.ensure_runtime_dirs()
        self.assertTrue((self.home / ".local" / "share" / "tmp" / "akid").is_dir())

    def test_add_persist_reload_remove(self) -> None:
        allowed = self.home / "projects" / "alpha"
        allowed.mkdir(parents=True)
        result = self.registry.add(str(allowed))
        self.assertTrue(result["added"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

        loaded = WritableRegistry(
            state_path=self.state,
            home=self.home,
            letta_home=self.letta_home,
            backend_dir=self.backend,
        )
        loaded.load()
        self.assertEqual([entry.path for entry in loaded._roots], [str(allowed.resolve())])
        self.assertEqual(loaded.revision, 1)

        removed = loaded.remove(str(allowed))
        self.assertTrue(removed["removed"])
        self.assertEqual(removed["revision"], 2)
        self.assertEqual(json.loads(self.state.read_text())["roots"], [])

    def test_rejects_root_missing_and_files(self) -> None:
        with self.assertRaises(RpcError) as ctx:
            self.registry.add("/")
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PATH)
        with self.assertRaises(RpcError) as ctx:
            self.registry.add(str(self.home / "missing"))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PATH)
        file_path = self.home / "file.txt"
        file_path.write_text("x")
        with self.assertRaises(RpcError) as ctx:
            self.registry.add(str(file_path))
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PATH)

    def test_default_covered_path_is_already_writable(self) -> None:
        nested = self.home / ".local" / "share" / "tmp" / "nested"
        nested.mkdir(parents=True)
        result = self.registry.add(str(nested))
        self.assertFalse(result["added"])
        self.assertTrue(result["already_writable"])
        self.assertEqual(result["covered_by"], str(self.home / ".local" / "share" / "tmp"))
        with self.assertRaises(RpcError) as ctx:
            self.registry.remove(str(nested))
        self.assertEqual(ctx.exception.code, ErrorCode.PATH_IS_DEFAULT)

    def test_duplicate_add_is_rejected(self) -> None:
        allowed = self.home / "work"
        allowed.mkdir()
        self.registry.add(str(allowed))
        with self.assertRaises(RpcError) as ctx:
            self.registry.add(str(allowed))
        self.assertEqual(ctx.exception.code, ErrorCode.PATH_ALREADY_REGISTERED)

    def test_nested_explicit_roots_can_be_revoked_independently(self) -> None:
        parent = self.home / "work"
        child = parent / "project"
        child.mkdir(parents=True)
        self.registry.add(str(parent))
        self.registry.add(str(child))
        self.registry.remove(str(parent))
        self.assertEqual([entry.path for entry in self.registry._roots], [str(child.resolve())])

    def test_invalid_persisted_path_is_reported_and_removable(self) -> None:
        missing = self.home / "gone"
        self.registry.save()
        data = json.loads(self.state.read_text())
        data["roots"] = [{"path": str(missing), "added_at": "now"}]
        self.state.write_text(json.dumps(data))
        loaded = WritableRegistry(
            state_path=self.state,
            home=self.home,
            letta_home=self.letta_home,
            backend_dir=self.backend,
        )
        loaded.load()
        self.assertEqual(len(loaded._invalid), 1)
        self.assertNotIn(missing, loaded.effective_roots())
        self.assertTrue(loaded.remove(str(missing))["removed"])

    @unittest.skipUnless(probe_bwrap()[0], "bwrap unavailable")
    def test_probe_reports_bwrap(self) -> None:
        available, path, error = self.registry.probe(refresh=True)
        self.assertTrue(available, error)
        self.assertTrue(path)


if __name__ == "__main__":
    unittest.main()
