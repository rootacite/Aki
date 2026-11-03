from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from engine.akid.proactive import ProactiveMixin
from engine.live_context import ChannelTranscriptEntry


class _DummyProactive(ProactiveMixin):
    pass


class ProactiveTimeAnchorTests(unittest.TestCase):
    def test_anchor_has_current_time_last_message_and_history_rule(self) -> None:
        now = datetime(
            2026, 11, 3, 19, 41, 40, tzinfo=timezone(timedelta(hours=8))
        )
        sent_at = 1793704049371
        dummy = _DummyProactive()
        dummy._channel_history = [
            ChannelTranscriptEntry(
                seq=783,
                sender="Acite Lee",
                sender_id="legacy-user",
                text="@Aki test",
                sent_at=sent_at,
                timestamp="2026-11-03T11:07:29.371Z",
            )
        ]
        expected_last = (
            datetime.fromtimestamp(sent_at / 1000.0, tz=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        anchor = dummy._proactive_time_anchor(now=now)
        self.assertIn(
            f"current_local_time: {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            anchor,
        )
        self.assertIn(f"last_channel_message_at: {expected_last}", anchor)
        self.assertIn(
            'Never describe older transcript entries as "just now".', anchor
        )

    def test_anchor_without_history_uses_none(self) -> None:
        dummy = _DummyProactive()
        dummy._channel_history = []
        anchor = dummy._proactive_time_anchor(
            now=datetime(2026, 11, 3, 19, 41, 40, tzinfo=timezone.utc)
        )
        self.assertIn("last_channel_message_at: none", anchor)

    def test_proactive_message_puts_anchor_after_trigger(self) -> None:
        dummy = _DummyProactive()
        dummy._channel_history = []
        message = dummy._build_proactive_message(
            {
                "presence": {
                    "triggers": [
                        {
                            "type": "work",
                            "facts": {"active_seconds": 10804, "active_hours": 3.0},
                        }
                    ]
                }
            }
        )
        self.assertIn("<trigger type=\"presence\">", message)
        self.assertIn("<time-anchor>", message)
        self.assertLess(message.index("</trigger>"), message.index("<time-anchor>"))


if __name__ == "__main__":
    unittest.main()
