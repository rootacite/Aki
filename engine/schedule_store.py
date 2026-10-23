"""Aki-only schedule persistence, recurrence, and due-run selection."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engine.aki_config import atomic_write

SCHEDULE_VERSION = 1
MAX_SCHEDULES = 256
MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 2048
DEFAULT_TIMEZONE = "Asia/Shanghai"
RECURRENCES = {"none", "daily", "weekly"}
STATUSES = {"scheduled", "fired", "cancelled", "missed"}
UTC = timezone.utc


class ScheduleError(ValueError):
    """Invalid schedule input or state."""


def now_utc() -> datetime:
    return datetime.now(UTC)


def system_timezone_name() -> str:
    configured = os.environ.get("TZ", "").strip()
    if configured:
        with contextlib.suppress(Exception):
            ZoneInfo(configured)
            return configured
    with contextlib.suppress(OSError):
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if text:
            ZoneInfo(text)
            return text
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        marker = "/zoneinfo/"
        if marker in target:
            candidate = target.split(marker, 1)[1]
            with contextlib.suppress(Exception):
                ZoneInfo(candidate)
                return candidate
    return DEFAULT_TIMEZONE


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: object, *, timezone_name: str = "") -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("datetime must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleError(f"invalid ISO 8601 datetime: {text}") from exc
    if parsed.tzinfo is None:
        zone = resolve_timezone(timezone_name or system_timezone_name())
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def resolve_timezone(value: object) -> ZoneInfo:
    name = str(value or "").strip() or system_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError(f"unknown timezone: {name}") from exc


def _clean_text(value: object, *, label: str, limit: int, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise ScheduleError(f"{label} must not be empty")
    if len(text) > limit:
        raise ScheduleError(f"{label} must be at most {limit} characters")
    return text


@dataclass
class ScheduleRecord:
    id: str
    title: str
    starts_at: str
    timezone: str
    repeat: str = "none"
    repeat_until: str | None = None
    notes: str = ""
    status: str = "scheduled"
    created_at: str = ""
    updated_at: str = ""
    last_fired_at: str | None = None
    last_missed_at: str | None = None
    next_fire_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "starts_at": self.starts_at,
            "timezone": self.timezone,
            "repeat": self.repeat,
            "repeat_until": self.repeat_until,
            "notes": self.notes,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_fired_at": self.last_fired_at,
            "last_missed_at": self.last_missed_at,
            "next_fire_at": self.next_fire_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScheduleRecord":
        record = cls(
            id=str(value.get("id") or "").strip(),
            title=str(value.get("title") or ""),
            starts_at=str(value.get("starts_at") or ""),
            timezone=str(value.get("timezone") or system_timezone_name()),
            repeat=str(value.get("repeat") or "none"),
            repeat_until=(
                str(value.get("repeat_until"))
                if value.get("repeat_until")
                else None
            ),
            notes=str(value.get("notes") or ""),
            status=str(value.get("status") or "scheduled"),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            last_fired_at=(
                str(value.get("last_fired_at"))
                if value.get("last_fired_at")
                else None
            ),
            last_missed_at=(
                str(value.get("last_missed_at"))
                if value.get("last_missed_at")
                else None
            ),
            next_fire_at=(
                str(value.get("next_fire_at"))
                if value.get("next_fire_at")
                else None
            ),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if not self.id:
            raise ScheduleError("schedule id must not be empty")
        self.title = _clean_text(
            self.title, label="title", limit=MAX_TITLE_CHARS, allow_empty=False
        )
        self.notes = _clean_text(self.notes, label="notes", limit=MAX_NOTES_CHARS)
        if self.repeat not in RECURRENCES:
            raise ScheduleError(f"invalid repeat: {self.repeat}")
        if self.status not in STATUSES:
            raise ScheduleError(f"invalid status: {self.status}")
        self.timezone = str(resolve_timezone(self.timezone))
        parse_datetime(self.starts_at, timezone_name=self.timezone)
        if self.repeat_until:
            parse_datetime(self.repeat_until, timezone_name=self.timezone)
        if self.repeat == "none" and self.repeat_until:
            raise ScheduleError("repeat_until is only valid for recurring schedules")


def _advance_local(
    record: ScheduleRecord,
    *,
    after: datetime,
) -> datetime | None:
    zone = resolve_timezone(record.timezone)
    start_local = parse_datetime(record.starts_at, timezone_name=record.timezone).astimezone(zone)
    reference = after.astimezone(zone)
    until = (
        parse_datetime(record.repeat_until, timezone_name=record.timezone)
        if record.repeat_until
        else None
    )
    wall = start_local.timetz().replace(tzinfo=None)
    if record.repeat == "none":
        return start_local.astimezone(UTC)

    if record.repeat == "daily":
        candidate_date = reference.date()
    else:
        days = (start_local.weekday() - reference.weekday()) % 7
        candidate_date = reference.date() + timedelta(days=days)

    for _ in range(3660):
        candidate = datetime.combine(candidate_date, wall, tzinfo=zone)
        if candidate <= reference:
            candidate_date += timedelta(days=1 if record.repeat == "daily" else 7)
            continue
        if candidate < start_local:
            candidate_date += timedelta(days=1 if record.repeat == "daily" else 7)
            continue
        candidate_utc = candidate.astimezone(UTC)
        if until is not None and candidate_utc > until:
            return None
        return candidate_utc
    return None


def next_fire_for(record: ScheduleRecord, *, after: datetime) -> str | None:
    if record.status != "scheduled":
        return None
    candidate = _advance_local(record, after=after)
    return iso_utc(candidate) if candidate is not None else None


def _new_record(
    *,
    title: object,
    starts_at: object,
    timezone_name: object,
    repeat: object,
    repeat_until: object,
    notes: object,
    now: datetime,
) -> ScheduleRecord:
    zone_name = str(timezone_name or "").strip() or system_timezone_name()
    resolve_timezone(zone_name)
    start = parse_datetime(starts_at, timezone_name=zone_name)
    if start < now - timedelta(seconds=600):
        raise ScheduleError("starts_at is more than 10 minutes in the past")
    repeat_value = str(repeat or "none").strip().lower()
    if repeat_value not in RECURRENCES:
        raise ScheduleError(f"invalid repeat: {repeat_value}")
    until_iso = None
    if repeat_until:
        until = parse_datetime(repeat_until, timezone_name=zone_name)
        if until < start:
            raise ScheduleError("repeat_until must not be before starts_at")
        until_iso = iso_utc(until)
    stamp = iso_utc(now)
    record = ScheduleRecord(
        id=f"sched-{uuid.uuid4().hex}",
        title=_clean_text(
            title, label="title", limit=MAX_TITLE_CHARS, allow_empty=False
        ),
        starts_at=iso_utc(start),
        timezone=zone_name,
        repeat=repeat_value,
        repeat_until=until_iso,
        notes=_clean_text(notes, label="notes", limit=MAX_NOTES_CHARS),
        status="scheduled",
        created_at=stamp,
        updated_at=stamp,
    )
    record.next_fire_at = next_fire_for(record, after=now)
    if record.next_fire_at is None:
        raise ScheduleError("schedule has no future occurrence")
    return record


class ScheduleStore:
    """Versioned, locked schedule file for one agent profile."""

    def __init__(
        self,
        path: Path,
        *,
        max_records: int = MAX_SCHEDULES,
        missed_grace_seconds: int = 600,
    ) -> None:
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_records = max(1, int(max_records))
        self.missed_grace_seconds = max(0, int(missed_grace_seconds))
        self.last_error: str | None = None

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[ScheduleRecord]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = f"cannot read schedules: {exc}"
            return []
        raw_records = payload.get("schedules") if isinstance(payload, dict) else None
        if not isinstance(raw_records, list):
            self.last_error = "schedule file has no schedules list"
            return []
        records: list[ScheduleRecord] = []
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            try:
                records.append(ScheduleRecord.from_dict(item))
            except (ScheduleError, ZoneInfoNotFoundError) as exc:
                self.last_error = f"ignored invalid schedule: {exc}"
        return records

    def _write_unlocked(self, records: list[ScheduleRecord]) -> None:
        payload = {
            "version": SCHEDULE_VERSION,
            "schedules": [record.to_dict() for record in records],
        }
        atomic_write(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _load(self) -> list[ScheduleRecord]:
        with self._locked():
            return self._read_unlocked()

    def _mutate(self, callback):
        with self._locked():
            records = self._read_unlocked()
            result = callback(records)
            self._write_unlocked(records)
            self.last_error = None
            return result

    def create(self, params: Mapping[str, Any], *, now: datetime | None = None) -> ScheduleRecord:
        clock = now or now_utc()

        def mutate(records: list[ScheduleRecord]) -> ScheduleRecord:
            if len(records) >= self.max_records:
                # Keep the file bounded. Terminal audit records are pruned
                # oldest-first; scheduled entries are never silently dropped.
                terminal = sorted(
                    (record for record in records if record.status != "scheduled"),
                    key=lambda record: record.created_at,
                )
                while terminal and len(records) >= self.max_records:
                    victim = terminal.pop(0)
                    records.remove(victim)
                if len(records) >= self.max_records:
                    raise ScheduleError(
                        f"schedule limit reached ({self.max_records})"
                    )
            record = _new_record(
                title=params.get("title"),
                starts_at=params.get("starts_at"),
                timezone_name=params.get("timezone"),
                repeat=params.get("repeat"),
                repeat_until=params.get("repeat_until"),
                notes=params.get("notes"),
                now=clock,
            )
            records.append(record)
            return record

        return self._mutate(mutate)

    def update(self, params: Mapping[str, Any], *, now: datetime | None = None) -> ScheduleRecord:
        clock = now or now_utc()
        schedule_id = str(params.get("id") or "").strip()
        if not schedule_id:
            raise ScheduleError("id must not be empty")

        def mutate(records: list[ScheduleRecord]) -> ScheduleRecord:
            record = next((item for item in records if item.id == schedule_id), None)
            if record is None:
                raise ScheduleError(f"schedule not found: {schedule_id}")
            if "title" in params:
                record.title = str(params.get("title") or "")
            if "notes" in params:
                record.notes = str(params.get("notes") or "")
            if "timezone" in params:
                record.timezone = str(resolve_timezone(params.get("timezone")))
            if "starts_at" in params:
                record.starts_at = iso_utc(
                    parse_datetime(params.get("starts_at"), timezone_name=record.timezone)
                )
            if "repeat" in params:
                record.repeat = str(params.get("repeat") or "none").strip().lower()
            if "repeat_until" in params:
                value = params.get("repeat_until")
                record.repeat_until = (
                    iso_utc(parse_datetime(value, timezone_name=record.timezone))
                    if value
                    else None
                )
            record.validate()
            start = parse_datetime(record.starts_at, timezone_name=record.timezone)
            if record.repeat == "none" and start < clock - timedelta(seconds=600):
                raise ScheduleError("starts_at is more than 10 minutes in the past")
            record.status = "scheduled"
            record.last_fired_at = None
            record.last_missed_at = None
            record.updated_at = iso_utc(clock)
            record.next_fire_at = next_fire_for(record, after=clock)
            if record.next_fire_at is None:
                raise ScheduleError("schedule has no future occurrence")
            return record

        return self._mutate(mutate)

    def delete(self, schedule_id: object, *, now: datetime | None = None) -> ScheduleRecord:
        clock = now or now_utc()
        key = str(schedule_id or "").strip()
        if not key:
            raise ScheduleError("id must not be empty")

        def mutate(records: list[ScheduleRecord]) -> ScheduleRecord:
            record = next((item for item in records if item.id == key), None)
            if record is None:
                raise ScheduleError(f"schedule not found: {key}")
            record.status = "cancelled"
            record.next_fire_at = None
            record.updated_at = iso_utc(clock)
            return record

        return self._mutate(mutate)

    def list(
        self,
        *,
        status: str = "active",
        start: object = None,
        end: object = None,
    ) -> list[ScheduleRecord]:
        records = self._load()
        filter_value = str(status or "active").strip().lower()
        if filter_value == "active":
            records = [record for record in records if record.status == "scheduled"]
        elif filter_value != "all":
            records = [record for record in records if record.status == filter_value]
        lower = parse_datetime(start) if start else None
        upper = parse_datetime(end) if end else None
        if lower is not None or upper is not None:
            filtered = []
            for record in records:
                if not record.next_fire_at:
                    continue
                candidate = parse_datetime(record.next_fire_at)
                if lower is not None and candidate < lower:
                    continue
                if upper is not None and candidate > upper:
                    continue
                filtered.append(record)
            records = filtered
        records.sort(key=lambda record: (record.next_fire_at or "9999", record.created_at))
        return records

    def due(self, *, now: datetime | None = None) -> list[ScheduleRecord]:
        clock = now or now_utc()
        records = self._load()
        return [
            record
            for record in records
            if record.status == "scheduled"
            and record.next_fire_at
            and parse_datetime(record.next_fire_at) <= clock
        ]

    def complete_due(
        self,
        records: Iterable[ScheduleRecord],
        *,
        now: datetime | None = None,
        missed: bool = False,
    ) -> list[ScheduleRecord]:
        clock = now or now_utc()
        ids = {record.id for record in records}

        def mutate(all_records: list[ScheduleRecord]) -> list[ScheduleRecord]:
            completed: list[ScheduleRecord] = []
            for record in all_records:
                if record.id not in ids:
                    continue
                if missed:
                    record.last_missed_at = iso_utc(clock)
                else:
                    record.last_fired_at = iso_utc(clock)
                record.updated_at = iso_utc(clock)
                if record.repeat == "none":
                    record.status = "missed" if missed else "fired"
                    record.next_fire_at = None
                else:
                    record.status = "scheduled"
                    record.next_fire_at = next_fire_for(record, after=clock)
                    if record.next_fire_at is None:
                        record.status = "fired"
                completed.append(record)
            return completed

        return self._mutate(mutate)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        clock = now or now_utc()
        records = self._load()
        active = [record for record in records if record.status == "scheduled"]
        next_record = min(
            (
                record
                for record in active
                if record.next_fire_at
            ),
            key=lambda record: record.next_fire_at or "",
            default=None,
        )
        return {
            "path": str(self.path),
            "records": len(records),
            "active": len(active),
            "next_fire_at": next_record.next_fire_at if next_record else None,
            "next_title": next_record.title if next_record else None,
            "now": iso_utc(clock),
            "last_error": self.last_error,
        }
