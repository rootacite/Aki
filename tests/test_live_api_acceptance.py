from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from engine.live_acceptance import analyze_events, load_event_lines
from engine.live_context import (
    ChannelMember,
    ChannelTranscriptEntry,
    HistoryEntry,
    LiveContextSnapshot,
    ProcessInfo,
    render_channel_transcript,
    render_live_context,
    render_turn_control,
    render_user_format_guard,
)


def overlay(seq: int) -> str:
    member = ChannelMember(id="aki", name="Aki", is_self=True)
    transcript = render_channel_transcript(
        [
            ChannelTranscriptEntry(
                seq=seq,
                sender="Master",
                sender_id="master",
                text="earlier",
            )
        ]
    )
    live = render_live_context(
        LiveContextSnapshot(
            captured_at=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
            processes=[ProcessInfo(1, "python", 1.0, 1.0)],
            history=[HistoryEntry("echo hi")],
        ),
        memory_dir="/tmp/memfs/agent/memory",
        mcp_servers=("tavily",),
        mcp_tools=[{"name": "mcp__tavily__search"}],
    )
    control = render_turn_control(
        self_member=member,
        members=(member,),
        message="hello",
    )
    return "\n\n".join(
        [
            transcript,
            live,
            control,
            render_user_format_guard("Master"),
            f"[akid MQTT channel message]\nseq: {seq}\n"
            "sender: Master\nsource: user\nphase: final\n\nhello",
        ]
    )


def request_event(seq: int, *, request_id: str | None = None) -> dict:
    return {
        "event": "request",
        "request_id": request_id or f"req-{seq}",
        "payload": {
            "messages": [
                {"role": "system", "content": "# Format Guard"},
                {"role": "user", "content": overlay(seq)},
            ]
        },
    }


def terminal(request_id: str) -> dict:
    return {"event": "response_end", "request_id": request_id}


class LiveAcceptanceAnalyzerTests(unittest.TestCase):
    def test_accepts_five_well_formed_turns(self) -> None:
        events = []
        for seq in range(1, 6):
            events.extend([request_event(seq), terminal(f"req-{seq}")])
        report = analyze_events(events, expected_turns=5)
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["received_turns"], 5)
        self.assertEqual(report["user_seqs"], [1, 2, 3, 4, 5])

    def test_detects_duplicate_and_missing_blocks(self) -> None:
        events = [request_event(1), terminal("req-1")]
        duplicate_prompt = overlay(1).replace(
            "\n\n[akid MQTT channel message]",
            "\n\n# format-guard\n\nagain\n\n[akid MQTT channel message]",
        )
        events[0]["payload"]["messages"][1]["content"] = duplicate_prompt
        duplicate = analyze_events(events, expected_turns=1)
        self.assertEqual(duplicate["status"], "FAIL")
        self.assertTrue(
            any("more than once" in error for error in duplicate["errors"])
        )

        missing_prompt = overlay(2).replace("# format-guard", "")
        missing_events = [
            {
                "event": "request",
                "request_id": "req-2",
                "payload": {
                    "messages": [
                        {"role": "user", "content": missing_prompt},
                    ]
                },
            },
            terminal("req-2"),
        ]
        missing = analyze_events(missing_events, expected_turns=1)
        self.assertEqual(missing["status"], "FAIL")
        self.assertTrue(
            any("format-guard" in error for error in missing["errors"])
        )

    def test_detects_malformed_json_and_pending_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            path.write_text(
                json.dumps(request_event(1)) + "\n{not-json\n",
                encoding="utf-8",
            )
            events, errors = load_event_lines(path)
        self.assertEqual(len(events), 1)
        self.assertTrue(errors)
        pending = analyze_events(events, expected_turns=1, parse_errors=errors)
        self.assertEqual(pending["status"], "FAIL")
        self.assertTrue(any("malformed JSON" in error for error in pending["errors"]))

        incomplete = analyze_events([request_event(1)], expected_turns=1)
        self.assertEqual(incomplete["status"], "INCOMPLETE")
        self.assertTrue(any("no response_end" in error for error in incomplete["errors"]))


if __name__ == "__main__":
    unittest.main()
