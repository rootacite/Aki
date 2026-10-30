from __future__ import annotations

import unittest
from pathlib import Path

from engine.live_context import ChannelMember, render_turn_control

ROOT = Path(__file__).resolve().parent.parent


class PromptLayoutTests(unittest.TestCase):
    def test_format_guard_lives_in_all_base_prompts(self) -> None:
        for profile, user_name in (
            ("aki", "Master"),
            ("fuyu", "Master"),
            ("haru", "哥哥"),
        ):
            text = (ROOT / "prompts" / f"{profile}-system.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("# Format Guard", text)
            self.assertIn(
                "By default, do NOT use Markdown headings, bold tags, tables",
                text,
            )
            self.assertIn(
                "Use `'\\n\\n'` (two newlines) to separate paragraphs; each "
                "paragraph will be sent as a separate message in one batch.",
                text,
            )
            self.assertIn(user_name, text)
            self.assertIn(
                "Do NOT launch into a lengthy, detailed analysis", text
            )

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
