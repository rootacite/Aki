from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from engine.presence import ActivitySample, LogindActivityProvider, PresenceMonitor


def local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


def active(epoch: float) -> ActivitySample:
    return ActivitySample(True, valid=True, sampled_at=epoch)


def idle(epoch: float) -> ActivitySample:
    return ActivitySample(False, valid=True, idle_hint=True, sampled_at=epoch)


class PresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.monitor = PresenceMonitor(
            Path(self.tempdir.name) / "presence.json",
            work_warn_seconds=7200,
            work_repeat_seconds=3600,
            return_idle_seconds=2700,
            return_debounce_seconds=10,
            night_start="00:30",
            night_end="05:30",
            night_history_days=30,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _advance_active(self, start: float, seconds: int, step: int = 60):
        triggers = []
        current = start
        remaining = seconds
        while remaining > 0:
            delta = min(step, remaining)
            current += delta
            remaining -= delta
            triggers.extend(self.monitor.process_sample(active(current), now=current))
        return current, triggers

    def test_work_trigger_at_two_hours_then_every_hour(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        current, triggers = self._advance_active(start, 7200)
        self.assertEqual([item.kind for item in triggers], ["work"])
        self.assertEqual(triggers[0].facts["active_seconds"], 7200)
        current, triggers = self._advance_active(current, 3600)
        self.assertEqual([item.kind for item in triggers], ["work"])
        self.assertEqual(triggers[0].facts["active_seconds"], 10800)

    def test_idle_resets_work_counter(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        current, _ = self._advance_active(start, 3600)
        self.monitor.process_sample(idle(current + 10), now=current + 10)
        state = self.monitor.state
        self.assertEqual(state["active_seconds"], 0.0)
        self.assertEqual(state["next_work_threshold"], 7200.0)

    def test_long_idle_period_does_not_accumulate_work_time(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        current = start
        triggers = []
        for _ in range(8 * 60):
            current += 60
            triggers.extend(self.monitor.process_sample(idle(current), now=current))
        self.assertFalse([item for item in triggers if item.kind == "work"])
        self.assertEqual(self.monitor.state["active_seconds"], 0.0)

    def test_late_night_fires_once_per_cycle_and_counts_prior_nights(self) -> None:
        first = local_epoch(2026, 10, 24, 1, 0)
        triggers = self.monitor.process_sample(active(first), now=first)
        late = [item for item in triggers if item.kind == "late_night"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].facts["night_work_count"], 0)
        again = self.monitor.process_sample(active(first + 600), now=first + 600)
        self.assertFalse([item for item in again if item.kind == "late_night"])

        second = local_epoch(2026, 10, 25, 1, 0)
        triggers = self.monitor.process_sample(active(second), now=second)
        late = [item for item in triggers if item.kind == "late_night"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].facts["night_work_count"], 1)

    def test_daytime_does_not_fire_late_night(self) -> None:
        noon = local_epoch(2026, 10, 24, 12, 0)
        triggers = self.monitor.process_sample(active(noon), now=noon)
        self.assertFalse([item for item in triggers if item.kind == "late_night"])

    def test_return_requires_debounce(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        idle_start = start + 60
        self.monitor.process_sample(idle(idle_start), now=idle_start)
        return_at = idle_start + 2710
        first = self.monitor.process_sample(active(return_at), now=return_at)
        self.assertFalse([item for item in first if item.kind == "return"])
        second = self.monitor.process_sample(active(return_at + 10), now=return_at + 10)
        returns = [item for item in second if item.kind == "return"]
        self.assertEqual(len(returns), 1)
        self.assertGreaterEqual(returns[0].facts["idle_seconds"], 2700)

    def test_short_idle_does_not_trigger_return(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        idle_start = start + 60
        self.monitor.process_sample(idle(idle_start), now=idle_start)
        return_at = idle_start + 600
        self.monitor.process_sample(active(return_at), now=return_at)
        triggers = self.monitor.process_sample(active(return_at + 10), now=return_at + 10)
        self.assertFalse([item for item in triggers if item.kind == "return"])

    def test_startup_idle_suppresses_first_return(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(idle(start), now=start)
        return_at = start + 2710
        self.monitor.process_sample(active(return_at), now=return_at)
        triggers = self.monitor.process_sample(active(return_at + 10), now=return_at + 10)
        self.assertFalse([item for item in triggers if item.kind == "return"])

        idle_again = return_at + 20
        self.monitor.process_sample(idle(idle_again), now=idle_again)
        second_return = idle_again + 2710
        self.monitor.process_sample(active(second_return), now=second_return)
        triggers = self.monitor.process_sample(active(second_return + 10), now=second_return + 10)
        self.assertTrue([item for item in triggers if item.kind == "return"])

    def test_invalid_sample_does_not_change_state(self) -> None:
        start = local_epoch(2026, 10, 24, 8, 0)
        self.monitor.process_sample(active(start), now=start)
        before = dict(self.monitor.state)
        self.monitor.process_sample(
            ActivitySample(False, valid=False, error="logind unavailable"),
            now=start + 60,
        )
        after = self.monitor.state
        self.assertEqual(before["active_seconds"], after["active_seconds"])
        self.assertIn("logind unavailable", self.monitor.last_error or "")


class LogindProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_chosen_session_is_queried_after_selection(self) -> None:
        provider = LogindActivityProvider()

        async def show_session(session_id: str):
            if session_id == "2":
                return {
                    "Id": "2",
                    "Active": "yes",
                    "State": "active",
                    "IdleHint": "no",
                    "IdleSinceHint": "0",
                    "LockedHint": "no",
                }
            return {}

        async def choose_session():
            provider._session_id = "2"
            return "2"

        with mock.patch.object(provider, "_show_session", side_effect=show_session), mock.patch.object(
            provider, "_choose_session", side_effect=choose_session
        ):
            sample = await provider.sample()
        self.assertTrue(sample.valid)
        self.assertTrue(sample.active)
        self.assertEqual(sample.session_id, "2")

    async def _real_input_probe_sample(
        self,
        *,
        last_activity_at: float | None,
        idle_hint: str = "no",
        locked_hint: str = "no",
    ):
        now = time.time()
        provider = LogindActivityProvider(
            input_probe=FakeInputProbe(last_activity_at=last_activity_at),
            input_idle_seconds=300,
        )

        async def show_session(session_id: str):
            if session_id == "2":
                return {
                    "Id": "2",
                    "Active": "yes",
                    "State": "active",
                    "IdleHint": idle_hint,
                    "IdleSinceHint": "0",
                    "LockedHint": locked_hint,
                }
            return {}

        async def choose_session():
            provider._session_id = "2"
            return "2"

        with mock.patch.object(provider, "_show_session", side_effect=show_session), mock.patch.object(
            provider, "_choose_session", side_effect=choose_session
        ):
            return await provider.sample()

    async def test_recent_input_overrides_stuck_logind_idle_hint(self) -> None:
        now = time.time()
        sample = await self._real_input_probe_sample(last_activity_at=now - 30)
        self.assertTrue(sample.valid)
        self.assertTrue(sample.active)

    async def test_stale_input_marks_session_idle(self) -> None:
        now = time.time()
        last = now - 600
        sample = await self._real_input_probe_sample(last_activity_at=last)
        self.assertTrue(sample.valid)
        self.assertFalse(sample.active)
        self.assertAlmostEqual(sample.idle_since or 0, last, delta=1)

    async def test_locked_session_is_idle_even_with_recent_input(self) -> None:
        sample = await self._real_input_probe_sample(
            last_activity_at=time.time() - 30,
            locked_hint="yes",
        )
        self.assertFalse(sample.active)

    async def test_unavailable_input_probe_uses_logind_idle_hint(self) -> None:
        provider = LogindActivityProvider(
            input_probe=FakeInputProbe(
                available=False,
                ever_available=False,
                last_activity_at=None,
            )
        )

        async def show_session(session_id: str):
            return {
                "Id": "2",
                "Active": "yes",
                "State": "active",
                "IdleHint": "yes",
                "IdleSinceHint": "0",
                "LockedHint": "no",
            }

        with mock.patch.object(provider, "_show_session", side_effect=show_session):
            provider._session_id = "2"
            sample = await provider.sample()
        self.assertTrue(sample.valid)
        self.assertFalse(sample.active)


class FakeInputProbe:
    def __init__(
        self,
        *,
        available: bool = True,
        ever_available: bool = True,
        last_activity_at: float | None = None,
    ) -> None:
        self.available = available
        self.ever_available = ever_available
        self.last_activity_at = last_activity_at

    def poll(self, now: float | None = None) -> float | None:
        return self.last_activity_at

    def status(self):
        return {"available": self.available}

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
