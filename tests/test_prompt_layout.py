from __future__ import annotations

import unittest
from pathlib import Path

from engine.live_context import ChannelMember, render_turn_control

ROOT = Path(__file__).resolve().parent.parent


class PromptLayoutTests(unittest.TestCase):
    def test_format_guard_lives_in_all_base_prompts(self) -> None:
        guards: list[str] = []
        for profile in ("aki", "fuyu", "haru", "natsu"):
            text = (ROOT / "prompts" / f"{profile}-system.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("# Format Guard", text)
            guard = text.split("# Format Guard", 1)[1].split("{CORE_MEMORY}", 1)[0]
            guards.append(guard.strip())
            self.assertIn("SHORT SENTENCES BY DEFAULT (短句为主)", guard)
            self.assertIn("Do not target a fixed reply length", guard)
            self.assertIn("BUBBLE SPLITTING", guard)
            self.assertIn("ZERO MARKDOWN BLOAT", guard)
            self.assertIn("HIDDEN SPAN (`~[text]`)", guard)
            self.assertIn("`'\\n\\n'` (two newlines)", guard)
            for forbidden in (
                "real-time IM chatroom",
                "IM BUBBLE SPLITTING",
                "CONVERSATIONAL BREVITY (CRITICAL)",
                "2 or 3",
            ):
                self.assertNotIn(forbidden, guard)
        self.assertEqual(len(set(guards)), 1)

    def test_turn_control_contains_control_fields_but_no_message_copy(self) -> None:
        member = ChannelMember(id="aki", name="Aki", is_self=True)
        block = render_turn_control(
            self_member=member,
            members=(member,),
            message="UNIQUE_MESSAGE_BODY",
            wait_labels=("Fuyu",),
        )
        self.assertTrue(block.startswith("# turn-control"))
        self.assertIn("## Turn Action & Permissions", block)
        self.assertIn("## Initiation Budget", block)
        self.assertIn("remaining=3/3", block)
        self.assertIn('<wait-user allowed="true"', block)
        self.assertIn('<wait-other targets="Fuyu">', block)
        self.assertNotIn("UNIQUE_MESSAGE_BODY", block)
        self.assertNotIn("<new-message", block)


if __name__ == "__main__":
    unittest.main()
