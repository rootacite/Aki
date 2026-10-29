from __future__ import annotations

import unittest

from engine.akid.turns import render_format_guard
from engine.live_context import ChannelMember, render_turn_control


class PromptLayoutTests(unittest.TestCase):
    def test_format_guard_is_enabled_for_all_profiles(self) -> None:
        for user_name in ("Master", "主人", "哥哥"):
            block = render_format_guard(user_name)
            self.assertIn('<format-guard version="1">', block)
            self.assertIn(user_name, block)

    def test_turn_control_contains_control_fields_but_no_message_copy(self) -> None:
        member = ChannelMember(id="aki", name="Aki", is_self=True)
        block = render_turn_control(
            self_member=member,
            members=(member,),
            message="UNIQUE_MESSAGE_BODY",
            wait_labels=("Fuyu",),
        )
        self.assertIn('<turn-control version="1"', block)
        self.assertIn("<guidance>", block)
        self.assertIn("<initiation-budget", block)
        self.assertIn('<wait-user allowed="true"', block)
        self.assertIn('<wait-other targets="Fuyu">', block)
        self.assertNotIn("UNIQUE_MESSAGE_BODY", block)
        self.assertNotIn("<new-message", block)


if __name__ == "__main__":
    unittest.main()
