"""Night-talk schedule, prompt, and tool gate."""

from __future__ import annotations

import io
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from engine.akictl.cli import main
from engine.nighttalk import (
    NIGHT_TALK_INTERVAL_SECONDS,
    NIGHT_TALK_PROMPT,
    NightTalkState,
    advance_nighttalk,
    load_nighttalk_state,
    manual_nighttalk_block,
    metadata_is_nighttalk,
    night_cycle_key,
    nighttalk_tool_decision,
    note_nighttalk_delivery,
    render_nighttalk_prompt,
    save_nighttalk_state,
)
from engine.tokifactor import TextEnvelope, decode_envelope, encode_envelope


TZ = timezone(timedelta(hours=8))


def at(hour: int, minute: int, *, day: int = 30, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


class NightCycleTests(unittest.TestCase):
    def test_default_window_uses_the_local_date(self) -> None:
        start, end = time(0, 30), time(5, 30)
        self.assertEqual(night_cycle_key(at(0, 30), start, end), "2026-10-30")
        self.assertEqual(night_cycle_key(at(2, 0), start, end), "2026-10-30")
        self.assertEqual(night_cycle_key(at(5, 29), start, end), "2026-10-30")
        self.assertIsNone(night_cycle_key(at(0, 29), start, end))
        self.assertIsNone(night_cycle_key(at(5, 30), start, end))
        self.assertIsNone(night_cycle_key(at(12, 0), start, end))

    def test_wrapping_window_keeps_the_start_date(self) -> None:
        start, end = time(23, 0), time(5, 30)
        self.assertEqual(night_cycle_key(at(23, 0), start, end), "2026-10-30")
        self.assertEqual(
            night_cycle_key(at(1, 0, day=31), start, end), "2026-10-30"
        )
        self.assertIsNone(night_cycle_key(at(12, 0), start, end))
        self.assertIsNone(night_cycle_key(at(5, 30, day=31), start, end))


class NightTalkScheduleTests(unittest.TestCase):
    def test_arms_thirty_minutes_after_becoming_eligible(self) -> None:
        step = advance_nighttalk(
            NightTalkState(),
            now=1_000.0,
            eligible=True,
            cycle="2026-10-30",
            roll=0.0,
        )
        self.assertEqual(step.action, "arm")
        self.assertEqual(step.state.fires, 0)
        self.assertEqual(step.state.cycle, "2026-10-30")
        self.assertEqual(step.state.next_roll_at, 1_000.0 + NIGHT_TALK_INTERVAL_SECONDS)

    def test_waits_then_misses_then_fires_once(self) -> None:
        armed = advance_nighttalk(
            NightTalkState(),
            now=1_000.0,
            eligible=True,
            cycle="2026-10-30",
            roll=0.0,
        ).state
        waiting = advance_nighttalk(
            armed,
            now=armed.next_roll_at - 1,
            eligible=True,
            cycle="2026-10-30",
            roll=0.0,
        )
        self.assertEqual(waiting.action, "wait")
        missed = advance_nighttalk(
            waiting.state,
            now=waiting.state.next_roll_at,
            eligible=True,
            cycle="2026-10-30",
            roll=0.5,
        )
        self.assertEqual(missed.action, "miss")
        self.assertEqual(missed.state.fires, 0)
        fired = advance_nighttalk(
            missed.state,
            now=missed.state.next_roll_at,
            eligible=True,
            cycle="2026-10-30",
            roll=0.49,
        )
        self.assertEqual(fired.action, "fire")
        self.assertEqual(fired.state.fires, 0)
        counted = note_nighttalk_delivery(
            fired.state, cycle="2026-10-30", target="Fuyu", fired_at="t"
        )
        self.assertEqual(counted.fires, 1)
        self.assertEqual(counted.last_target, "Fuyu")

    def test_inactivity_break_restarts_the_timer(self) -> None:
        armed = advance_nighttalk(
            NightTalkState(cycle="2026-10-30", fires=1, next_roll_at=5_000.0),
            now=4_000.0,
            eligible=False,
            cycle="2026-10-30",
            roll=0.0,
        )
        self.assertEqual(armed.action, "idle")
        self.assertIsNone(armed.state.next_roll_at)
        self.assertEqual(armed.state.fires, 1)
        again = advance_nighttalk(
            armed.state,
            now=4_100.0,
            eligible=True,
            cycle="2026-10-30",
            roll=0.0,
        )
        self.assertEqual(again.action, "arm")
        self.assertEqual(again.state.next_roll_at, 4_100.0 + NIGHT_TALK_INTERVAL_SECONDS)

    def test_overdue_timer_rolls_once(self) -> None:
        now = 10_000.0
        step = advance_nighttalk(
            NightTalkState(
                cycle="2026-10-30",
                fires=0,
                next_roll_at=now - 4 * NIGHT_TALK_INTERVAL_SECONDS,
            ),
            now=now,
            eligible=True,
            cycle="2026-10-30",
            roll=0.1,
        )
        self.assertEqual(step.action, "fire")
        self.assertEqual(step.state.next_roll_at, now + NIGHT_TALK_INTERVAL_SECONDS)
        self.assertEqual(step.state.fires, 0)

    def test_fourth_success_is_not_rolled(self) -> None:
        step = advance_nighttalk(
            NightTalkState(cycle="2026-10-30", fires=3, next_roll_at=1.0),
            now=100.0,
            eligible=True,
            cycle="2026-10-30",
            roll=0.0,
        )
        self.assertEqual(step.action, "capped")
        self.assertEqual(step.state.fires, 3)
        self.assertIsNone(step.state.next_roll_at)

    def test_new_cycle_resets_the_count(self) -> None:
        step = advance_nighttalk(
            NightTalkState(cycle="2026-10-30", fires=3),
            now=100.0,
            eligible=True,
            cycle="2026-10-31",
            roll=0.0,
        )
        self.assertEqual(step.action, "arm")
        self.assertEqual(step.state.fires, 0)
        self.assertEqual(step.state.cycle, "2026-10-31")

    def test_manual_trigger_counts_only_inside_the_cycle(self) -> None:
        full = NightTalkState(cycle="2026-10-30", fires=3)
        self.assertEqual(
            manual_nighttalk_block(full, "2026-10-30"),
            "night talk already fired 3 times tonight",
        )
        self.assertEqual(manual_nighttalk_block(full, None), "")
        self.assertEqual(manual_nighttalk_block(full, "2026-10-31"), "")
        outside = note_nighttalk_delivery(
            full, cycle=None, target="Aki", fired_at="day"
        )
        self.assertEqual(outside.fires, 3)
        self.assertEqual(outside.last_target, "Aki")
        inside = note_nighttalk_delivery(
            NightTalkState(cycle="2026-10-29", fires=3),
            cycle="2026-10-30",
            target="Haru",
            fired_at="night",
        )
        self.assertEqual(inside.fires, 1)
        self.assertEqual(inside.cycle, "2026-10-30")

    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nighttalk" / "aki.json"
            state = NightTalkState(
                cycle="2026-10-30",
                fires=2,
                next_roll_at=1234.5,
                last_target="Fuyu",
                last_fire_at="2026-10-30T02:00:00+08:00",
            )
            save_nighttalk_state(path, state)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_nighttalk_state(path), state)
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_nighttalk_state(path), NightTalkState())


class NightTalkPromptTests(unittest.TestCase):
    def test_prompt_replaces_both_username_placeholders(self) -> None:
        rendered = render_nighttalk_prompt("哥哥")
        self.assertEqual(rendered.count("哥哥"), 2)
        self.assertNotIn("${{USERNAME}}", rendered)
        self.assertTrue(rendered.startswith("现在是安静的夜晚，哥哥非活跃"))
        self.assertIn(NIGHT_TALK_PROMPT.split("${{USERNAME}}")[1][:8], rendered)

    def test_metadata_flag_comes_only_from_agent_finals(self) -> None:
        self.assertTrue(metadata_is_nighttalk({"nighttalk": "1"}))
        self.assertTrue(metadata_is_nighttalk({"nighttalk": "true"}))
        self.assertFalse(metadata_is_nighttalk({}))
        self.assertFalse(metadata_is_nighttalk({"nighttalk": "0"}))
        self.assertFalse(metadata_is_nighttalk(None))

    def test_nighttalk_metadata_survives_the_channel_envelope(self) -> None:
        envelope = TextEnvelope(
            sender="Aki",
            text="今晚很安静",
            sender_id="aki",
            metadata={
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "final",
                "wake": "all",
                "nighttalk": "1",
            },
        )
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.metadata.get("nighttalk"), "1")


class NightTalkToolTests(unittest.TestCase):
    def test_lock_and_non_readonly_tools_are_denied(self) -> None:
        self.assertIn(
            "NIGHTTALK_LOCK_DENIED",
            nighttalk_tool_decision("acquire_execution_lock") or "",
        )
        for name in (
            "generate_image",
            "Bash",
            "Write",
            "tokifactor_send_file",
            "tokifactor_send_image",
            "aki_ledger_add",
            "aki_schedule_create",
        ):
            decision = nighttalk_tool_decision(name)
            self.assertIsNotNone(decision, name)
            assert decision is not None
            self.assertIn("NIGHTTALK_READONLY", decision)
        for name in (
            "Read",
            "Grep",
            "search_chat_history",
            "desktop_take_screenshot",
            "camera_take_photo",
            "wait_other",
            "aki_schedule_list",
            "aki_ledger_list",
        ):
            self.assertIsNone(nighttalk_tool_decision(name), name)


class NightTalkCommandTests(unittest.TestCase):
    def test_other_profile_is_rejected_before_connecting(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["fuyu", "nighttalk"])
        self.assertEqual(code, 2)
        self.assertIn("aki profile", stderr.getvalue())

    def test_help_lists_nighttalk(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["help"])
        self.assertEqual(code, 0)
        self.assertIn("akictl nighttalk", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
