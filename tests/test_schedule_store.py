from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.schedule_store import ScheduleError, ScheduleStore

UTC = timezone.utc


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = ScheduleStore(self.root / "schedules.json")
        self.now = datetime(2026, 10, 24, 8, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_list_update_delete(self) -> None:
        record = self.store.create(
            {
                "title": "喝水",
                "starts_at": "2026-10-24T17:00:00",
                "timezone": "Asia/Shanghai",
                "repeat": "none",
                "notes": "一杯",
            },
            now=self.now,
        )
        self.assertEqual(record.next_fire_at, "2026-10-24T09:00:00Z")
        listed = self.store.list()
        self.assertEqual([item.id for item in listed], [record.id])

        updated = self.store.update(
            {"id": record.id, "title": "喝水休息", "repeat": "daily"},
            now=self.now,
        )
        self.assertEqual(updated.title, "喝水休息")
        self.assertEqual(updated.repeat, "daily")
        self.assertEqual(updated.next_fire_at, "2026-10-24T09:00:00Z")

        cancelled = self.store.delete(record.id, now=self.now)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.list(status="all")[0].status, "cancelled")

    def test_weekly_recurrence_uses_start_weekday(self) -> None:
        # 2026-10-24 is Saturday.
        record = self.store.create(
            {
                "title": "周会",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
                "repeat": "weekly",
            },
            now=self.now,
        )
        self.assertEqual(record.next_fire_at, "2026-10-24T09:00:00Z")
        completed = self.store.complete_due(
            [record], now=datetime(2026, 10, 24, 9, 1, tzinfo=UTC)
        )
        self.assertEqual(completed[0].next_fire_at, "2026-10-31T09:00:00Z")

    def test_daily_recurrence_advances_after_fire(self) -> None:
        record = self.store.create(
            {
                "title": "散步",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
                "repeat": "daily",
            },
            now=self.now,
        )
        due = self.store.due(now=datetime(2026, 10, 24, 9, 1, tzinfo=UTC))
        self.assertEqual([item.id for item in due], [record.id])
        completed = self.store.complete_due(
            due, now=datetime(2026, 10, 24, 9, 1, tzinfo=UTC)
        )
        self.assertEqual(completed[0].status, "scheduled")
        self.assertEqual(completed[0].next_fire_at, "2026-10-25T09:00:00Z")

    def test_past_start_rejected_but_ten_minute_grace_allowed(self) -> None:
        with self.assertRaises(ScheduleError):
            self.store.create(
                {
                    "title": "late",
                    "starts_at": "2026-10-24T07:00:00Z",
                    "timezone": "UTC",
                },
                now=self.now,
            )
        record = self.store.create(
            {
                "title": "grace",
                "starts_at": "2026-10-24T07:55:00Z",
                "timezone": "UTC",
            },
            now=self.now,
        )
        self.assertEqual(record.next_fire_at, "2026-10-24T07:55:00Z")

    def test_repeat_until_stops_recurrence(self) -> None:
        record = self.store.create(
            {
                "title": "short",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
                "repeat": "daily",
                "repeat_until": "2026-10-25T09:00:00Z",
            },
            now=self.now,
        )
        completed = self.store.complete_due(
            [record], now=datetime(2026, 10, 25, 9, 1, tzinfo=UTC)
        )
        self.assertEqual(completed[0].status, "fired")
        self.assertIsNone(completed[0].next_fire_at)

    def test_missed_marks_or_advances(self) -> None:
        one_shot = self.store.create(
            {
                "title": "one",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
            },
            now=self.now,
        )
        recurring = self.store.create(
            {
                "title": "repeat",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
                "repeat": "daily",
            },
            now=self.now,
        )
        later = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
        self.store.complete_due([one_shot, recurring], now=later, missed=True)
        all_records = {item.title: item for item in self.store.list(status="all")}
        self.assertEqual(all_records["one"].status, "missed")
        self.assertEqual(all_records["repeat"].status, "scheduled")
        self.assertEqual(all_records["repeat"].next_fire_at, "2026-10-25T09:00:00Z")

    def test_snapshot_reports_next(self) -> None:
        record = self.store.create(
            {
                "title": "snap",
                "starts_at": "2026-10-24T09:00:00Z",
                "timezone": "UTC",
            },
            now=self.now,
        )
        snapshot = self.store.snapshot(now=self.now)
        self.assertEqual(snapshot["active"], 1)
        self.assertEqual(snapshot["next_title"], "snap")
        self.assertEqual(snapshot["next_fire_at"], record.next_fire_at)


if __name__ == "__main__":
    unittest.main()
