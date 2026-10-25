from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.channel_prompt import (
    ConversationBoundary,
    capture_conversation_boundary,
    read_journal,
    restore_persisted_channel_prompt,
    strip_persisted_channel_prompt,
)


def prompt_text(body: str = "hello") -> str:
    return (
        "[akid MQTT channel message]\n"
        "message-time: 2026-10-26 00:00\n"
        "wake-reminder: test\n"
        "sender: Acite Lee (legacy_48487683f202ce845f7df0e56c94b11d)\n"
        "seq: 1\n"
        '<channel-context>\n<members online="1">\n'
        '<member id="legacy_48487683f202ce845f7df0e56c94b11d" '
        'name="Acite Lee" kind="user"/>\n'
        "</members>\n</channel-context>\n"
        "message:\n"
        f"{body}\n"
    )


def row(prompt: str, *, message_id: str = "ui-msg-1", entry_id: str = "entry-1") -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": None,
        "timestamp": "2026-10-26T00:00:00.000Z",
        "message": {
            "id": message_id,
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>context</system-reminder>"},
                {"type": "text", "text": prompt},
            ],
        },
    }


class ChannelPromptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conversation = self.root / "conversation"
        self.conversation.mkdir()
        self.messages = self.conversation / "messages.jsonl"
        self.record = self.conversation / "conversation.json"
        self.journal = self.root / "journal" / "aki.jsonl"
        self.messages.write_text('{"type":"session","id":"default"}\n', encoding="utf-8")
        self.record.write_text(
            json.dumps(
                {"id": "default", "updated_at": "2026-10-25T00:00:00.000Z"},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.boundary = capture_conversation_boundary(self.messages)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append(self, value: str | dict) -> None:
        with self.messages.open("a", encoding="utf-8") as handle:
            if isinstance(value, str):
                handle.write(value)
            else:
                handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")

    def strip(self, prompt: str, *, boundary: ConversationBoundary | None = None):
        return strip_persisted_channel_prompt(
            conversation_dir=self.conversation,
            boundary=boundary or self.boundary,
            expected_prompt=prompt,
            turn_id="turn-1",
            profile="aki",
            journal_path=self.journal,
            speaker_name="Acite Lee",
            speaker_kind="user",
        )

    def test_only_target_line_changes_and_cache_is_invalidated(self) -> None:
        prompt = prompt_text()
        assistant = (
            '{"type":"message","id":"assistant-entry","message":'
            '{"id":"ui-msg-2","role":"assistant","content":[{"type":"text","text":"r"}]}}\n'
        )
        self.append(row(prompt))
        self.append(assistant)
        prefix = self.messages.read_bytes()[: self.boundary.size]
        before_updated = json.loads(self.record.read_text())["updated_at"]

        result = self.strip(prompt)

        self.assertEqual(result.status, "applied")
        data = self.messages.read_bytes()
        self.assertTrue(data.startswith(prefix))
        self.assertIn(assistant.encode("utf-8"), data)
        self.assertNotIn(b"<channel-context>", data)
        row_after = json.loads(data.splitlines()[1])
        target_text = row_after["message"]["content"][1]["text"]
        self.assertIn("message-time: 2026-10-26 00:00", target_text)
        self.assertIn("message:\nhello", target_text)
        after_updated = json.loads(self.record.read_text())["updated_at"]
        self.assertNotEqual(after_updated, before_updated)
        records = read_journal(self.journal)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["message_id"], "ui-msg-1")
        self.assertEqual(records[0]["original_text"], prompt)
        self.assertEqual(records[0]["turn_id"], "turn-1")

    def test_changed_prefix_does_not_modify_transcript(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt))
        before = self.messages.read_bytes()
        invalid = ConversationBoundary(
            size=self.boundary.size,
            prefix_sha256="0" * 64,
            prefix_length=max(1, self.boundary.prefix_length),
        )
        result = self.strip(prompt, boundary=invalid)
        self.assertEqual(result.status, "boundary_invalid")
        self.assertEqual(self.messages.read_bytes(), before)
        self.assertFalse(self.journal.exists())

    def test_ambiguous_prompt_is_not_modified(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt, message_id="ui-msg-1", entry_id="entry-1"))
        self.append(row(prompt, message_id="ui-msg-2", entry_id="entry-2"))
        before = self.messages.read_bytes()
        result = self.strip(prompt)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.messages.read_bytes(), before)
        self.assertFalse(self.journal.exists())

    def test_missing_target_is_not_modified(self) -> None:
        self.append(row(prompt_text("different")))
        before = self.messages.read_bytes()
        result = self.strip(prompt_text("hello"))
        self.assertEqual(result.status, "no_target")
        self.assertEqual(self.messages.read_bytes(), before)

    def test_journal_failure_prevents_transcript_write(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt))
        before = self.messages.read_bytes()
        with mock.patch(
            "engine.channel_prompt.append_journal",
            side_effect=OSError("disk full"),
        ):
            result = self.strip(prompt)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.messages.read_bytes(), before)

    def test_conversation_failure_rolls_back_transcript(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt))
        before = self.messages.read_bytes()
        with mock.patch(
            "engine.channel_prompt.bump_conversation_updated_at",
            side_effect=ValueError("race"),
        ):
            result = self.strip(prompt)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.messages.read_bytes(), before)

    def test_restore_round_trip(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt))
        result = self.strip(prompt)
        self.assertEqual(result.status, "applied")
        journal = read_journal(self.journal)[0]
        restored = restore_persisted_channel_prompt(
            conversation_dir=self.conversation,
            message_id="ui-msg-1",
            original_text=journal["original_text"],
            stripped_text=journal["stripped_text"],
            apply=True,
        )
        self.assertEqual(restored.status, "restored")
        row_after = json.loads(self.messages.read_text().splitlines()[1])
        self.assertEqual(row_after["message"]["content"][1]["text"], prompt)

    def test_restore_dry_run_does_not_write(self) -> None:
        prompt = prompt_text()
        self.append(row(prompt))
        self.strip(prompt)
        journal = read_journal(self.journal)[0]
        before = self.messages.read_bytes()
        restored = restore_persisted_channel_prompt(
            conversation_dir=self.conversation,
            message_id="ui-msg-1",
            original_text=journal["original_text"],
            stripped_text=journal["stripped_text"],
            apply=False,
        )
        self.assertEqual(restored.status, "would_restore")
        self.assertEqual(self.messages.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
