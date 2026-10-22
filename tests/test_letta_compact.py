from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

from engine.letta_compact import (
    _WebSocket,
    _parse_compact_output,
    _reflection_running,
    _reflection_state_path,
    _touch_agent_record,
    run_letta_compact,
)


class LettaCompactTests(unittest.TestCase):
    def test_parse_compact_output(self) -> None:
        parsed = _parse_compact_output(
            "Compaction completed (mode: all). Message buffer length "
            "reduced from 12 to 3.\n\nSummary: older context condensed"
        )
        self.assertTrue(parsed["compacted"])
        self.assertEqual(parsed["num_messages_before"], 12)
        self.assertEqual(parsed["num_messages_after"], 3)
        self.assertEqual(parsed["summary"], "older context condensed")

    def test_parse_compact_output_unchanged(self) -> None:
        parsed = _parse_compact_output(
            "Compaction run, but the number of messages is the same"
        )
        self.assertFalse(parsed["compacted"])
        self.assertIsNone(parsed["num_messages_before"])

    def test_reflection_running_detects_agent_task(self) -> None:
        status = {
            "background_processes": [
                {"kind": "bash", "description": "reflection"},
                {
                    "kind": "agent_task",
                    "task_type": "reflection",
                    "description": "Reflect on recent conversations",
                },
            ]
        }
        self.assertTrue(_reflection_running(status))
        self.assertFalse(_reflection_running({"background_processes": []}))
        self.assertFalse(_reflection_running(None))

    def test_reflection_state_path_respects_transcript_root(self) -> None:
        path = _reflection_state_path(
            {"LETTA_TRANSCRIPT_ROOT": "/tmp/transcripts"}, "agent/a", "conv b"
        )
        self.assertEqual(
            str(path), "/tmp/transcripts/agent_a/conv_b/state.json"
        )

    def test_touch_agent_record_refreshes_mtime_without_changing_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            agents = Path(tempdir) / "agents"
            agents.mkdir()
            path = agents / "agent.json"
            path.write_text('{"id": "agent-x", "model": "aki/model"}\n', encoding="utf-8")
            before = path.stat()
            self.assertTrue(_touch_agent_record(tempdir, "agent-x"))
            after = path.stat()
            self.assertEqual(path.read_text(encoding="utf-8"), '{"id": "agent-x", "model": "aki/model"}\n')
            self.assertGreater(after.st_mtime_ns, before.st_mtime_ns)
            self.assertFalse(_touch_agent_record(tempdir, "missing"))

    def test_invalid_mode_rejected_before_spawning(self) -> None:
        with self.assertRaisesRegex(Exception, "invalid compaction mode"):
            run_letta_compact(
                letta_bin="letta",
                agent_id="agent",
                conversation_id="default",
                cwd=".",
                mode="bad",
            )

    def test_websocket_round_trip(self) -> None:
        left, right = socket.socketpair()
        try:
            sender = _WebSocket(left)
            receiver = _WebSocket(right)
            sender.send_json({"type": "ping", "value": "秋"})
            self.assertEqual(
                receiver.recv_json(1.0), {"type": "ping", "value": "秋"}
            )
            sender.close()
        finally:
            left.close()
            right.close()

    def test_websocket_large_round_trip(self) -> None:
        left, right = socket.socketpair()
        try:
            sender = _WebSocket(left)
            receiver = _WebSocket(right)
            payload = {"type": "large", "value": "x" * 70000}
            sender.send_json(payload)
            self.assertEqual(json.loads(json.dumps(payload)), receiver.recv_json(1.0))
            sender.close()
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
