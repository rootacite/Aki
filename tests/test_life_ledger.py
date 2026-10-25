from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from engine.live_context import LiveContextSnapshot, render_live_context
from engine.life_ledger import LedgerError, LifeLedger, render_ledger_block, serialized_bytes


class LifeLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ledger.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_add_list_delete_and_cap(self) -> None:
        ledger = LifeLedger(self.path, max_bytes=1024, max_record_bytes=300)
        first = ledger.add("old " + "a" * 120)
        second = ledger.add("new " + "b" * 120)
        self.assertEqual(first["evicted_ids"], [])
        self.assertEqual(len(ledger.records()), 2)

        evicted = []
        for index in range(5):
            result = ledger.add(f"newest {index} " + "c" * 120)
            evicted.extend(result["evicted_ids"])
        self.assertTrue(evicted)
        self.assertLessEqual(serialized_bytes(ledger.records()), 1024)
        self.assertLessEqual(self.path.stat().st_size, 1024)
        self.assertEqual(ledger.records()[-1].text, "newest 4 " + "c" * 120)

        deleted = ledger.delete(ledger.records()[-1].id)
        self.assertTrue(deleted.text.startswith("newest"))
        self.assertFalse(any(record.id == deleted.id for record in ledger.records()))

    def test_record_too_large_rejected_without_write(self) -> None:
        ledger = LifeLedger(self.path, max_record_bytes=64)
        with self.assertRaises(LedgerError):
            ledger.add("x" * 65)
        self.assertEqual(ledger.records(), [])

    def test_snapshot_and_render(self) -> None:
        ledger = LifeLedger(self.path)
        ledger.add("喜欢热牛奶", now="2026-10-24T00:00:00Z")
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["count"], 1)
        block = render_ledger_block(ledger.records())
        self.assertIn("<life-ledger", block)
        self.assertIn("喜欢热牛奶", block)
        self.assertIn('id="ledger-', block)

    def test_ledger_is_in_live_context_even_with_tiny_budget(self) -> None:
        ledger = LifeLedger(self.path)
        ledger.add("身体状况：今天有点累")
        snapshot = LiveContextSnapshot(
            captured_at=datetime(2026, 10, 24, 8, 0, tzinfo=timezone.utc)
        )
        text = render_live_context(
            snapshot,
            budget_tokens=1024,
            ledger_block=render_ledger_block(ledger.records()),
        )
        self.assertIn("<life-ledger", text)
        self.assertIn("今天有点累", text)


if __name__ == "__main__":
    unittest.main()
