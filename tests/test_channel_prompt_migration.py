from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate-channel-prompts.py"
spec = importlib.util.spec_from_file_location("migrate_channel_prompts", SCRIPT)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)


def prompt_text(
    *,
    sender: str = "Acite Lee (legacy_48487683f202ce845f7df0e56c94b11d)",
    kind_attr: str = ' kind="user"',
    include_wake: bool = True,
    include_time: bool = True,
    body: str = "hello",
) -> str:
    lines = ["[akid MQTT channel message]"]
    if include_time:
        lines.append("message-time: 2026-10-26 00:00")
    if include_wake:
        lines.append("wake-reminder: old")
    lines.extend(
        [
            f"sender: {sender}",
            "seq: 7",
            '<channel-context>',
            '<members online="1">',
            '<member id="legacy_48487683f202ce845f7df0e56c94b11d" '
            f'name="Acite Lee"{kind_attr}/>',
            "</members>",
            "</channel-context>",
            "message:",
            body,
        ]
    )
    return "\n".join(lines) + "\n"


def message_row(prompt: str, *, message_id: str = "ui-msg-1", row_id: str = "entry-1") -> dict:
    return {
        "type": "message",
        "id": row_id,
        "timestamp": "2026-10-26T00:00:00.000Z",
        "message": {
            "id": message_id,
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>keep</system-reminder>"},
                {"type": "text", "text": prompt},
            ],
        },
    }


class ChannelPromptMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conversation = self.root / "conversation"
        self.conversation.mkdir()
        self.messages = self.conversation / "messages.jsonl"
        self.record = self.conversation / "conversation.json"
        self.record.write_text(
            json.dumps({"id": "default", "updated_at": "2026-10-25T00:00:00.000Z"})
            + "\n",
            encoding="utf-8",
        )
        self.identity = lambda member_id, name: (
            "user" if name == "Acite Lee" else ""
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_rows(self, rows: list[dict]) -> None:
        self.messages.write_text(
            '{"type":"session","id":"default"}\n'
            + "\n".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                for row in rows
            )
            + "\n",
            encoding="utf-8",
        )

    def plan(self, entries=None):
        return migration.build_profile_plan(
            profile="aki",
            messages_path=self.messages,
            entries=entries or [],
            identity_lookup=self.identity,
            username="Master",
        )

    def test_roster_kind_wins_over_history(self) -> None:
        self.write_rows([message_row(prompt_text())])
        plan = self.plan(
            entries=[{"seq": 7, "text": "hello", "from_agent": True}]
        )
        self.assertTrue(plan.changed)
        self.assertEqual(plan.stats["kind_sources"], {"roster": 1})
        self.assertIn("当前正在和你说话的是 Master。", plan.output_text)
        self.assertNotIn("用户", plan.output_text)
        self.assertNotIn("<channel-context>", plan.output_text)
        self.assertIn("<system-reminder>keep</system-reminder>", plan.output_text)

    def test_old_format_gets_wake_reminder_after_message_time(self) -> None:
        self.write_rows(
            [
                message_row(
                    prompt_text(kind_attr="", include_wake=False, include_time=True)
                )
            ]
        )
        plan = self.plan(
            entries=[{"seq": 7, "text": "hello", "from_agent": False}]
        )
        self.assertTrue(plan.changed)
        self.assertEqual(plan.stats["kind_sources"], {"channel-history": 1})
        row = json.loads(plan.output_text.splitlines()[1])
        lines = row["message"]["content"][1]["text"].splitlines()
        time_index = lines.index("message-time: 2026-10-26 00:00")
        self.assertEqual(
            lines[time_index + 1],
            "wake-reminder: 当前正在和你说话的是 Master。",
        )
        self.assertEqual(plan.stats["speaker_added"], 1)

    def test_identity_registry_is_used_when_roster_and_history_are_sparse(self) -> None:
        self.write_rows(
            [message_row(prompt_text(kind_attr="", include_wake=False))]
        )
        plan = self.plan(entries=[])
        self.assertTrue(plan.changed)
        self.assertEqual(plan.stats["kind_sources"], {"identity-registry": 1})

    def test_missing_sender_is_reported_without_guessing(self) -> None:
        text = prompt_text().replace(
            "sender: Acite Lee (legacy_48487683f202ce845f7df0e56c94b11d)\n", ""
        )
        self.write_rows([message_row(text)])
        plan = self.plan()
        self.assertFalse(plan.changed)
        self.assertEqual(plan.stats["unmatched"], 1)
        self.assertEqual(plan.changes[0]["status"], "unmatched")

    def test_migration_is_idempotent(self) -> None:
        self.write_rows([message_row(prompt_text())])
        first = self.plan()
        self.messages.write_text(first.output_text, encoding="utf-8")
        second = self.plan()
        self.assertFalse(second.changed)
        self.assertEqual(second.stats["already"], 1)

    def test_backup_and_apply_preserve_originals(self) -> None:
        self.write_rows([message_row(prompt_text())])
        (self.conversation / "system-prompt.json").write_text(
            '{"content":"base"}\n', encoding="utf-8"
        )
        (self.conversation / "manifest.json").write_text(
            '{"schema_version":2}\n', encoding="utf-8"
        )
        plan = self.plan()
        backup = migration.backup_profile(plan, self.root / "backup" / "aki")
        self.assertTrue((self.root / "backup" / "aki" / "messages.jsonl").is_file())
        self.assertEqual(backup["files"]["manifest.json"]["present"], True)
        result = migration.apply_profile_plan(plan, backup)
        self.assertTrue(result["written"])
        self.assertNotIn("<channel-context>", self.messages.read_text())
        record = json.loads(self.record.read_text())
        self.assertNotEqual(record["updated_at"], "2026-10-25T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
