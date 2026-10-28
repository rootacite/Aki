from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from engine.live_context import (
    ChannelTranscriptEntry,
    apply_shared_memory,
    clear_shared_memory,
    render_shared_memory,
)
from engine.public_memory import PublicMemoryStore


def message(
    seq: int,
    text: str,
    *,
    message_id: str = "",
    sender_id: str = "master",
    from_agent: bool = False,
) -> ChannelTranscriptEntry:
    return ChannelTranscriptEntry(
        seq=seq,
        sender="Master" if not from_agent else "Aki",
        sender_id=sender_id,
        text=text,
        from_agent=from_agent,
        timestamp=f"2026-10-28T10:00:{seq:02d}Z",
        sent_at=1790000000000 + seq,
        message_id=message_id,
        phase="final",
    )


class PublicMemoryTests(unittest.TestCase):
    def test_store_deduplicates_and_writes_index(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = PublicMemoryStore(root / "shared" / "messages.jsonl")
            first = message(1, "first public message", message_id="m1")
            second = message(2, "second public message", message_id="m2")
            self.assertTrue(store.record(first))
            self.assertTrue(store.record(second))
            self.assertFalse(store.record(first))
            snapshot = store.snapshot()
            self.assertEqual(
                [item["text"] for item in snapshot],
                ["first public message", "second public message"],
            )
            self.assertEqual(store.status()["records"], 2)
            index = store.index_path.read_text(encoding="utf-8")
            self.assertIn("Records: 2", index)
            raw = store.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(raw), 2)
            self.assertEqual(json.loads(raw[0])["id"], "message:m1")

    def test_store_deduplicates_content_without_message_id(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = PublicMemoryStore(Path(tempdir) / "messages.jsonl")
            first = message(1, "same content")
            second = replace(message(2, "same content"), sent_at=first.sent_at)
            self.assertTrue(store.record(first))
            self.assertFalse(store.record(second))
            self.assertEqual(store.status()["records"], 1)

    def test_shared_memory_block_renders_and_is_replaceable(self) -> None:
        records = [
            {
                "id": "m1",
                "sender": "Master",
                "sender_id": "master",
                "from_agent": False,
                "text": "<hello> & welcome",
                "timestamp": "2026-10-28T10:00:00Z",
            },
            {
                "id": "m2",
                "sender": "Aki",
                "sender_id": "aki",
                "from_agent": True,
                "text": "reply",
                "timestamp": "2026-10-28T10:00:01Z",
            },
        ]
        block = render_shared_memory(
            records,
            path="/tmp/shared/messages.jsonl",
            updated_at="2026-10-28T10:00:01Z",
            recent_limit=2,
        )
        self.assertIn('<shared-memory path="/tmp/shared/messages.jsonl"', block)
        self.assertIn("&lt;hello&gt; &amp; welcome", block)
        self.assertIn("records=\"2\"", block)
        system = "base\n\n<live-context>old</live-context>\n"
        updated = apply_shared_memory(system, block)
        self.assertEqual(updated.count("<shared-memory"), 1)
        self.assertLess(updated.index("<shared-memory"), updated.index("<live-context>"))
        cleared = clear_shared_memory(updated)
        self.assertNotIn("<shared-memory", cleared)
        self.assertIn("<live-context>old</live-context>", cleared)


if __name__ == "__main__":
    unittest.main()
