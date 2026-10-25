from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate-channel-turn-times.py"
spec = importlib.util.spec_from_file_location("migrate_channel_turn_times", SCRIPT)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)


class ChannelTurnTimeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.messages = self.root / "conversation" / "messages.jsonl"
        self.messages.parent.mkdir(parents=True)
        self.backup = self.root / "backup"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rows(self, rows: list[dict]) -> None:
        self.messages.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )

    def channel_row(self, *, seq: int | None, body: str, timestamp: str) -> dict:
        seq_line = f"seq: {seq}\n" if seq is not None else ""
        prompt = (
            "[akid MQTT channel message]\n"
            "wake-reminder: test\n"
            "sender: Acite Lee (user-1)\n"
            f"{seq_line}"
            "message:\n"
            f"{body}\n"
        )
        return {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }

    def prompt_text(self, row: dict) -> str:
        return row["message"]["content"][0]["text"]

    def test_seq_match_uses_channel_timestamp(self) -> None:
        row = self.channel_row(
            seq=226,
            body="hello",
            timestamp="2026-10-25T15:38:00Z",
        )
        self.write_rows([row])
        report = migration.migrate_profile(
            profile="aki",
            messages_path=self.messages,
            entries=[{"seq": 226, "sent_at": 1792942663759, "text": "hello"}],
            backup_root=self.backup,
            apply=True,
        )
        updated = json.loads(self.messages.read_text())
        text = self.prompt_text(updated)
        self.assertIn("message-time: 2026-10-25 23:37", text)
        self.assertEqual(report["stats"]["channel-history"], 1)
        self.assertTrue((self.backup / "aki" / "messages.jsonl").is_file())

    def test_old_prompt_falls_back_to_turn_timestamp_once(self) -> None:
        row = self.channel_row(
            seq=None,
            body="old message",
            timestamp="2026-10-20T15:51:32Z",
        )
        self.write_rows([row])
        report = migration.migrate_profile(
            profile="aki",
            messages_path=self.messages,
            entries=[],
            backup_root=self.backup,
            apply=True,
        )
        text = self.prompt_text(json.loads(self.messages.read_text()))
        self.assertIn("message-time: 2026-10-20 23:51", text)
        self.assertEqual(report["stats"]["turn-timestamp"], 1)

        second = migration.migrate_profile(
            profile="aki",
            messages_path=self.messages,
            entries=[],
            backup_root=self.backup,
            apply=True,
        )
        self.assertEqual(second["stats"]["changed"], 0)
        self.assertEqual(second["stats"]["already"], 1)

    def test_dry_run_does_not_write_or_backup(self) -> None:
        row = self.channel_row(
            seq=1,
            body="hello",
            timestamp="2026-10-25T15:38:00Z",
        )
        self.write_rows([row])
        before = self.messages.read_text()
        migration.migrate_profile(
            profile="aki",
            messages_path=self.messages,
            entries=[{"seq": 1, "sent_at": 1792942663759, "text": "hello"}],
            backup_root=None,
            apply=False,
        )
        self.assertEqual(self.messages.read_text(), before)
        self.assertFalse(self.backup.exists())


if __name__ == "__main__":
    unittest.main()
