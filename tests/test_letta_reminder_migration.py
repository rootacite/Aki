from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.letta_reminders import (
    build_conversation_migration,
    build_transcript_migration,
    create_backup,
)


ROOT = Path(__file__).resolve().parent.parent

ENVIRONMENT_REMINDER = (
    "<system-reminder>This is an automated message providing context about "
    "the user's environment.\n## Device Information\n...</system-reminder>"
)
AGENT_INFO_REMINDER = (
    "<system-reminder>This is an automated message providing information "
    "about you.\n- **Agent ID**: agent-test</system-reminder>"
)
MCP_REMINDER = (
    "<system-reminder>MCP servers with available tools: None</system-reminder>"
)
PERMISSION_REMINDER = (
    "<system-reminder>Permission mode active: standard. Normal approval "
    "flow.</system-reminder>"
)
AGENT_MESSAGE_REMINDER = (
    "<system-reminder>This message is from agent parent-agent.</system-reminder>"
)
MEMORY_REMINDER = (
    "<system-reminder>You are performing background memory maintenance."
    "</system-reminder>"
)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class LettaReminderMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="letta-reminder-migration-")
        self.root = Path(self.temp.name)
        self.backend = self.root / "backend"
        self.transcripts = self.root / "transcripts"
        self.config_root = self.root / "config"
        self.config_root.mkdir()
        (self.config_root / "aki.conf").write_text(
            "[gateway]\nagent_id = agent-test\nconversation = default\n",
            encoding="utf-8",
        )
        self.conversation = (
            self.backend
            / "conversations"
            / b64url("default:agent-test")
        )
        self.conversation.mkdir(parents=True)
        self.messages = self.conversation / "messages.jsonl"
        self.record = self.conversation / "conversation.json"
        self.transcript_dir = self.transcripts / "agent-test" / "default"
        self.transcript_dir.mkdir(parents=True)
        self.transcript = self.transcript_dir / "transcript.jsonl"
        self.state = self.transcript_dir / "state.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_fixture(self) -> None:
        rows = [
            {
                "type": "message",
                "id": "row-1",
                "parentId": None,
                "timestamp": "2026-10-30T00:00:00.000Z",
                "message": {
                    "id": "ui-1",
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ENVIRONMENT_REMINDER},
                        {"type": "text", "text": AGENT_INFO_REMINDER},
                        {"type": "text", "text": MCP_REMINDER},
                        {"type": "text", "text": PERMISSION_REMINDER},
                        {"type": "text", "text": "hello"},
                    ],
                },
            },
            {
                "type": "message",
                "id": "row-2",
                "parentId": "row-1",
                "timestamp": "2026-10-30T00:00:01.000Z",
                "message": {
                    "id": "ui-2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "world"}],
                },
            },
            {
                "type": "message",
                "id": "row-3",
                "parentId": "row-2",
                "timestamp": "2026-10-30T00:00:02.000Z",
                "message": {
                    "id": "ui-3",
                    "role": "user",
                    "content": [{"type": "text", "text": ENVIRONMENT_REMINDER}],
                },
            },
            {
                "type": "message",
                "id": "row-4",
                "parentId": "row-3",
                "timestamp": "2026-10-30T00:00:03.000Z",
                "message": {
                    "id": "ui-4",
                    "role": "user",
                    "content": [{"type": "text", "text": MEMORY_REMINDER}],
                },
            },
        ]
        self.messages.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        self.record.write_text(
            json.dumps(
                {
                    "id": "default",
                    "updated_at": "2026-10-30T00:00:00.000Z",
                    "last_message_at": "2026-10-30T00:00:03.000Z",
                    "in_context_message_ids": ["ui-1", "ui-2", "ui-3", "ui-4"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.transcript.write_text(
            "\n".join(
                [
                    json.dumps({"kind": "user", "text": ENVIRONMENT_REMINDER}),
                    json.dumps({"kind": "user", "text": f"{MCP_REMINDER} hello"}),
                    json.dumps(
                        {
                            "kind": "assistant",
                            "text": "a1",
                            "source_message_id": "ui-2",
                        }
                    ),
                    json.dumps({"kind": "assistant", "text": "a2"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": "v3_assistant_steps",
                    "reflected_through_message_id": "ui-2",
                    "total_completed_steps": 2,
                    "reflected_completed_steps": 1,
                    "steps_since_last_successful_reflection": 1,
                    "last_reflection_started_at": "2026-10-29T00:00:00Z",
                    "last_reflection_succeeded_at": "2026-10-29T00:01:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_plans_remove_only_suppressed_reminders(self) -> None:
        self.write_fixture()
        plan = build_conversation_migration(self.messages)
        self.assertTrue(plan.changed)
        self.assertEqual(plan.removed_message_ids, ["ui-3"])
        text = plan.output_text
        self.assertNotIn("automated message providing context", text)
        self.assertNotIn("automated message providing information about you", text)
        self.assertNotIn("MCP servers with available tools", text)
        self.assertNotIn("Permission mode active", text)
        self.assertIn(MEMORY_REMINDER, text)
        self.assertIn("hello", text)

        transcript = build_transcript_migration(self.transcript, self.state)
        self.assertEqual(transcript.removed_rows, 1)
        self.assertIn("hello", transcript.output_text)
        self.assertNotIn(ENVIRONMENT_REMINDER, transcript.output_text)
        self.assertEqual(
            transcript.output_state["reflected_through_message_id"], "ui-2"
        )
        self.assertEqual(transcript.output_state["total_completed_steps"], 2)
        self.assertEqual(transcript.output_state["reflected_completed_steps"], 1)
        self.assertEqual(
            transcript.output_state["steps_since_last_successful_reflection"], 1
        )

    def test_dry_run_does_not_write_and_apply_backs_up(self) -> None:
        self.write_fixture()
        before_messages = self.messages.read_bytes()
        before_transcript = self.transcript.read_bytes()
        command = [
            sys.executable,
            str(ROOT / "scripts" / "migrate-letta-reminders.py"),
            "--profile",
            "aki",
            "--config-root",
            str(self.config_root),
            "--backend-dir",
            str(self.backend),
            "--transcript-root",
            str(self.transcripts),
        ]
        dry = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn('"mode": "dry-run"', dry.stdout)
        self.assertEqual(self.messages.read_bytes(), before_messages)
        self.assertEqual(self.transcript.read_bytes(), before_transcript)

        backup_root = self.root / "backup"
        applied = subprocess.run(
            [*command, "--apply", "--backup-root", str(backup_root)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertNotEqual(self.messages.read_bytes(), before_messages)
        self.assertTrue((backup_root / "SHA256SUMS").is_file())
        ids = json.loads(self.record.read_text())["in_context_message_ids"]
        self.assertEqual(ids, ["ui-1", "ui-2", "ui-4"])
        remaining = [json.loads(line) for line in self.messages.read_text().splitlines()]
        self.assertNotIn("row-3", {row["id"] for row in remaining})
        self.assertEqual(
            next(row for row in remaining if row["id"] == "row-4")["parentId"],
            "row-2",
        )

    def test_backup_checksums_match_copied_files(self) -> None:
        self.write_fixture()
        backup = create_backup(
            self.root / "checksum-backup",
            [(self.messages, "aki/messages.jsonl")],
        )
        checksums = (self.root / "checksum-backup" / "SHA256SUMS").read_text()
        self.assertIn("aki/messages.jsonl", checksums)
        self.assertEqual(backup["files"][0]["sha256"], checksums.split()[0])


if __name__ == "__main__":
    unittest.main()
