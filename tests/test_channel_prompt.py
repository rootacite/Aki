from __future__ import annotations

import unittest

from engine.channel_prompt import (
    SPEAKER_SENTENCE_TEMPLATE,
    add_speaker_to_wake_reminder,
    existing_speaker,
    infer_speaker_kind,
    migrate_channel_prompt,
    parse_channel_prompt,
    parse_sender,
    render_username_template,
    roster_kind,
    strip_channel_context,
)
def channel_prompt(
    *,
    sender: str = "Acite Lee (legacy_48487683f202ce845f7df0e56c94b11d)",
    roster: str = (
        '<channel-context>\n<members online="2">\n'
        '<member id="legacy_48487683f202ce845f7df0e56c94b11d" '
        'name="Acite Lee" kind="user" self="false">\n'
        "<description>owner</description>\n</member>\n"
        '<member id="legacy_8ddfb8585d44e615b2fe8b37846b4ef6" '
        'name="Aki" kind="agent" self="true"/>\n'
        "</members>\n</channel-context>\n"
    ),
    wake: str = (
        "wake-reminder: 用^排除不需要被提醒的成员（格式：^成员名）。"
        "@Master仅在你只对Master说话，无需打扰其他人时使用"
        "（该标记会强制只唤醒用户）。\n"
    ),
    include_time: bool = True,
    body: str = "hello",
) -> str:
    lines = ["[akid MQTT channel message]"]
    if include_time:
        lines.append("message-time: 2026-10-26 00:00")
    if wake:
        lines.append(wake.rstrip("\n"))
    lines.extend([f"sender: {sender}", "seq: 9", roster.rstrip("\n"), "message:", body])
    return "\n".join(lines) + "\n"


class ChannelPromptTextTests(unittest.TestCase):
    def test_strip_channel_context_preserves_everything_else(self) -> None:
        original = channel_prompt()
        updated, changed = strip_channel_context(original)
        self.assertTrue(changed)
        self.assertNotIn("<channel-context>", updated)
        self.assertNotIn("<members", updated)
        self.assertIn("message-time: 2026-10-26 00:00", updated)
        self.assertIn("sender: Acite Lee", updated)
        self.assertIn("message:\nhello", updated)

    def test_strip_requires_a_complete_block(self) -> None:
        original = "x\n<channel-context>\n<members>\n"
        updated, changed = strip_channel_context(original)
        self.assertFalse(changed)
        self.assertEqual(updated, original)

    def test_username_placeholder_is_replaced(self) -> None:
        self.assertEqual(
            render_username_template(SPEAKER_SENTENCE_TEMPLATE, "哥哥"),
            "当前正在和你说话的是 哥哥。",
        )

    def test_existing_wake_reminder_gets_speaker_once(self) -> None:
        original = channel_prompt()
        updated, roster_changed, speaker_changed = migrate_channel_prompt(
            original,
            speaker_name="Master",
            speaker_kind="user",
            username="Master",
        )
        self.assertTrue(roster_changed)
        self.assertTrue(speaker_changed)
        self.assertIn(
            "wake-reminder: 当前正在和你说话的是 Master。用^排除",
            updated,
        )
        self.assertNotIn("用户", updated)
        self.assertNotIn("<channel-context>", updated)
        again, roster_changed, speaker_changed = migrate_channel_prompt(
            updated,
            speaker_name="Master",
            speaker_kind="user",
            username="Master",
        )
        self.assertFalse(roster_changed)
        self.assertFalse(speaker_changed)
        self.assertEqual(again, updated)

    def test_missing_wake_reminder_is_inserted_after_message_time(self) -> None:
        original = channel_prompt(wake="", include_time=True)
        updated, _roster, speaker_changed = migrate_channel_prompt(
            original, speaker_name="Master", speaker_kind="user"
        )
        self.assertTrue(speaker_changed)
        lines = updated.splitlines()
        time_index = lines.index("message-time: 2026-10-26 00:00")
        self.assertEqual(
            lines[time_index + 1],
            "wake-reminder: 当前正在和你说话的是 Master。",
        )
        self.assertEqual(updated.count("wake-reminder:"), 1)

    def test_sender_parser_handles_legacy_and_uuid_ids(self) -> None:
        self.assertEqual(
            parse_sender("Acite Lee (legacy_48487683f202ce845f7df0e56c94b11d)"),
            ("Acite Lee", "legacy_48487683f202ce845f7df0e56c94b11d"),
        )
        self.assertEqual(
            parse_sender("acitehost (4554f60b-5ab6-414e-a303-4111c6717d8e)"),
            ("acitehost", "4554f60b-5ab6-414e-a303-4111c6717d8e"),
        )
        self.assertEqual(parse_sender("plain name"), ("plain name", ""))

    def test_kind_priority_and_fallback(self) -> None:
        prompt = channel_prompt()
        info = parse_channel_prompt(prompt)
        assert info is not None
        self.assertEqual(roster_kind(prompt, info.sender_id, info.sender_name), "user")
        self.assertEqual(
            infer_speaker_kind(
                prompt,
                info.sender_name,
                info.sender_id,
                history_entries=[{"seq": 9, "text": "hello", "from_agent": True}],
                identity_lookup=lambda _id, _name: "agent",
            ),
            "user",
        )
        no_kind = prompt.replace(' kind="user"', "")
        self.assertEqual(
            infer_speaker_kind(
                no_kind,
                info.sender_name,
                info.sender_id,
                history_entries=[{"seq": 9, "text": "hello", "from_agent": False}],
                identity_lookup=lambda _id, _name: "agent",
            ),
            "user",
        )
        self.assertEqual(
            infer_speaker_kind(
                no_kind,
                info.sender_name,
                info.sender_id,
                identity_lookup=lambda _id, _name: "user",
            ),
            "user",
        )
        self.assertEqual(
            infer_speaker_kind(
                no_kind,
                "Unknown",
                "legacy_00000000000000000000000000000000",
            ),
            "",
        )

    def test_add_speaker_replaces_an_existing_prefix(self) -> None:
        text = (
            "[akid MQTT channel message]\n"
            "wake-reminder: 当前正在和你说话的是 Old（成员）。base\n"
            "sender: New (legacy_00000000000000000000000000000000)\n"
        )
        updated, changed = add_speaker_to_wake_reminder(
            text, speaker_name="New", speaker_kind="agent"
        )
        self.assertTrue(changed)
        self.assertIn(
            "wake-reminder: 当前正在和你说话的是 New。base", updated
        )
        self.assertEqual(updated.count("当前正在和你说话的是"), 1)

    def test_existing_speaker_parses_old_labels(self) -> None:
        text = (
            "[akid MQTT channel message]\n"
            "wake-reminder: 当前正在和你说话的是 Acite Lee（用户）。base\n"
        )
        self.assertEqual(existing_speaker(text), ("Acite Lee", "用户"))


if __name__ == "__main__":
    unittest.main()
