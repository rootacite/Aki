from __future__ import annotations

import unittest

from engine.live_context import (
    ChannelMember,
    ChannelTranscriptEntry,
    render_channel_transcript,
    render_live_context,
    render_runtime_context,
    render_solo_memories,
    render_solo_mode,
    render_solo_transcript,
    render_turn_control,
    render_user_format_guard,
)
from engine.live_context import _FORMAT_GUARD_RE
from engine.live_context.types import LiveContextSnapshot
from datetime import datetime, timezone


class UserOverlayMarkdownTests(unittest.TestCase):
    def test_block_headings_and_data_boundaries(self) -> None:
        mode = render_solo_mode(
            member_name="Aki",
            user_name="Master",
            session_id="solo-1",
            scene="Private room.",
        )
        self.assertTrue(mode.startswith("# solo-mode\n\n## Notice\n"))
        self.assertIn("\n\n## Scene\n", mode)
        self.assertIn("\n\n## Rules\n- ", mode)
        self.assertNotIn("<solo-mode", mode)

        entries = [
            ChannelTranscriptEntry(
                seq=1,
                sender="Master",
                sender_id="master",
                text="hello",
            )
        ]
        channel = render_channel_transcript(entries)
        solo = render_solo_transcript(entries)
        for name, block in (
            ("channel-transcript", channel),
            ("solo-transcript", solo),
        ):
            self.assertTrue(block.startswith(f"# {name}\n\n## Note\n"))
            self.assertIn("\n\n## Content\n<messages>\n", block)
            self.assertTrue(block.endswith("</messages>"))

        class Session:
            started_at = "2026-10-30T00:00:00Z"
            ended_at = "2026-10-30T00:01:00Z"
            summary = "summary"
            archived = False
            entries = []

        memories = render_solo_memories([Session()])
        self.assertTrue(memories.startswith("# solo-memories\n\n## Note\n"))
        self.assertIn("\n\n## Content\n<solo ", memories)
        self.assertIn("</solo>", memories)
        self.assertNotIn("budget_tokens=", memories)

    def test_live_and_runtime_context_include_memfs_and_mcp(self) -> None:
        live = render_live_context(
            LiveContextSnapshot(
                captured_at=datetime(2026, 10, 30, tzinfo=timezone.utc)
            ),
            memory_dir="/tmp/memory",
            mcp_servers=("tavily",),
            mcp_tools=[{"name": "mcp__tavily__search"}],
        )
        runtime = render_runtime_context(
            memory_dir="/tmp/memory",
            mcp_servers=("tavily",),
            mcp_tools=[{"name": "mcp__tavily__search"}],
        )
        for block in (live, runtime):
            self.assertIn("## MemFS", block)
            self.assertIn("- path: `/tmp/memory`", block)
            self.assertIn("## MCP", block)
            self.assertIn("- servers: tavily", block)
            self.assertIn("mcp__tavily__search", block)
            self.assertNotIn("<live-context", block)
            self.assertNotIn("</live-context>", block)
        self.assertIn("Desktop live context is disabled.", runtime)

    def test_turn_control_and_format_guard_have_no_framework_attributes(self) -> None:
        member = ChannelMember(id="aki", name="Aki", is_self=True)
        control = render_turn_control(
            self_member=member,
            members=(member,),
            message="hello",
            wait_labels=("Fuyu",),
        )
        self.assertTrue(control.startswith("# turn-control\n## Turn Action & Permissions\n"))
        self.assertIn("\n\n## Initiation Budget\n- status: ", control)
        self.assertIn("remaining=3/3", control)
        self.assertIn("\n\n## Wait User\n<wait-user ", control)
        self.assertIn("</wait-user>", control)
        self.assertIn("\n\n## Wait Other (MANDATORY BEFORE FINAL REPLY)\n<wait-other ", control)
        self.assertIn("</wait-other>", control)
        for legacy in (
            "budget_tokens=",
            "shown=",
            "total=",
            "dropped=",
            "frozen=",
            "from_agent=",
            "solo=",
            "<turn-control",
        ):
            self.assertNotIn(legacy, control)

        guard = render_user_format_guard("Master")
        self.assertTrue(guard.startswith("# format-guard (STRICT ENFORCEMENT)\n\n"))
        self.assertIn("CONVERSATIONAL BREVITY (CRITICAL)", guard)
        self.assertIn("IM BUBBLE SPLITTING", guard)
        self.assertIn("ZERO MARKDOWN BLOAT", guard)
        self.assertIn("WAKE TAG CHECK", guard)
        self.assertIn("`'\\n\\n'` (two newlines)", guard)
        self.assertIn("Master", guard)
        match = _FORMAT_GUARD_RE.search(guard)
        self.assertIsNotNone(match)
        self.assertEqual(_FORMAT_GUARD_RE.sub("", guard).strip(), "")


if __name__ == "__main__":
    unittest.main()
