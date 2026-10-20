from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "patch-letta-compaction.py"
spec = importlib.util.spec_from_file_location("patch_letta_compaction", SCRIPT)
assert spec is not None and spec.loader is not None
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


ORIGINAL = """function before() {}
function contextCompactionThreshold(contextWindow) {
  if (typeof contextWindow !== "number" || !Number.isFinite(contextWindow) || contextWindow <= 0) {
    return;
  }
  const reserveTokens = Math.min(LOCAL_CONTEXT_COMPACTION_RESERVE_TOKENS, Math.max(1, Math.floor(contextWindow * LOCAL_SMALL_CONTEXT_COMPACTION_RESERVE_RATIO)));
  return Math.max(0, contextWindow - reserveTokens);
}
function after() {}
"""


class LettaCompactionPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tempdir.name) / "letta.js"
        self.bundle.write_text(ORIGINAL, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_patches_to_half_window_and_is_idempotent(self) -> None:
        first = patcher.patch_bundle(self.bundle, 0.5)
        self.assertIn("ratio=0.5", first)
        text = self.bundle.read_text(encoding="utf-8")
        self.assertIn("Math.floor(contextWindow * 0.5)", text)
        self.assertIn("function before()", text)
        self.assertIn("function after()", text)
        self.assertEqual(patcher.current_ratio(text), 0.5)
        self.assertEqual(patcher.patch_bundle(self.bundle, 0.5), patcher.patch_bundle(self.bundle, 0.5))
        backup = self.bundle.with_name("letta.js.aki-original")
        self.assertEqual(backup.read_text(encoding="utf-8"), ORIGINAL)

    def test_restore_returns_original(self) -> None:
        patcher.patch_bundle(self.bundle, 0.5)
        patcher.restore_bundle(self.bundle)
        self.assertEqual(self.bundle.read_text(encoding="utf-8"), ORIGINAL)
        self.assertEqual(patcher.current_ratio(self.bundle.read_text(encoding="utf-8")), None)

    def test_status_reports_patch_state(self) -> None:
        self.assertTrue(patcher.status(self.bundle).startswith("unpatched:"))
        patcher.patch_bundle(self.bundle, 0.5)
        self.assertTrue(patcher.status(self.bundle).startswith("patched:"))


if __name__ == "__main__":
    unittest.main()
