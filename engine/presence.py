"""Logind-backed user activity, work-duration, late-night, and return triggers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from engine.aki_config import atomic_write

PRESENCE_VERSION = 1
DEFAULT_POLL_SECONDS = 15
DEFAULT_IDLE_RESET_SECONDS = 300
DEFAULT_WORK_WARN_SECONDS = 7200
DEFAULT_WORK_REPEAT_SECONDS = 3600
DEFAULT_NIGHT_START = "00:30"
DEFAULT_NIGHT_END = "05:30"
DEFAULT_NIGHT_HISTORY_DAYS = 30
DEFAULT_RETURN_IDLE_SECONDS = 2700
DEFAULT_RETURN_DEBOUNCE_SECONDS = 10
MAX_SAMPLE_GAP_SECONDS = 60


@dataclass
class ActivitySample:
    active: bool
    valid: bool = True
    session_id: str = ""
    idle_hint: bool | None = None
    locked: bool = False
    session_active: bool = True
    idle_since: float | None = None
    sampled_at: float = field(default_factory=time.time)
    error: str | None = None


@dataclass(frozen=True)
class PresenceTrigger:
    kind: str
    facts: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "facts": dict(self.facts)}


def parse_clock(value: str, default: str) -> dt_time:
    text = str(value or default).strip() or default
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return dt_time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid clock value: {text}") from exc


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def now_iso(epoch: float | None = None) -> str:
    value = datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class ActivityProvider:
    async def sample(self) -> ActivitySample:
        raise NotImplementedError


class LogindActivityProvider(ActivityProvider):
    def __init__(
        self,
        *,
        loginctl_bin: str = "loginctl",
        session_id: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.loginctl_bin = loginctl_bin
        self.configured_session_id = str(session_id or "").strip()
        self.logger = logger or logging.getLogger("akid.presence")
        self._session_id = ""

    async def sample(self) -> ActivitySample:
        try:
            values = await self._show_session(self._session_id)
            if not values or values.get("Active", "").lower() != "yes":
                chosen = await self._choose_session()
                if chosen:
                    self._session_id = chosen
                    values = await self._show_session(chosen)
            if not values:
                return ActivitySample(
                    active=False,
                    valid=False,
                    error="loginctl returned no usable session",
                )
            if values.get("State", "").lower() in ("closing", "lingering"):
                return ActivitySample(
                    active=False,
                    valid=False,
                    session_id=self._session_id,
                    error=f"loginctl session is {values.get('State')}",
                )
            idle_value = values.get("IdleHint")
            if idle_value is None:
                return ActivitySample(
                    active=False,
                    valid=False,
                    session_id=self._session_id,
                    error="loginctl session has no IdleHint",
                )
            locked = values.get("LockedHint", "no").lower() == "yes"
            session_active = values.get("Active", "yes").lower() == "yes"
            idle_hint = idle_value.lower() == "yes"
            idle_since = _usec_to_epoch(values.get("IdleSinceHint"))
            active = session_active and not locked and not idle_hint
            return ActivitySample(
                active=active,
                valid=True,
                session_id=self._session_id,
                idle_hint=idle_hint,
                locked=locked,
                session_active=session_active,
                idle_since=idle_since,
                sampled_at=time.time(),
            )
        except Exception as exc:
            return ActivitySample(
                active=False,
                valid=False,
                session_id=self._session_id,
                error=str(exc),
                sampled_at=time.time(),
            )

    async def _choose_session(self) -> str:
        if self.configured_session_id:
            self._session_id = self.configured_session_id
            return self._session_id
        candidates: list[str] = []
        xdg = os.environ.get("XDG_SESSION_ID", "").strip()
        if xdg:
            candidates.append(xdg)
        try:
            _code, stdout, _stderr = await _run(
                [self.loginctl_bin, "list-sessions", "--no-legend"]
            )
        except Exception:
            stdout = ""
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0].lower() == "session":
                continue
            if parts[3] == "seat0":
                candidates.append(parts[0])
        seen: set[str] = set()
        fallback = ""
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            fallback = fallback or candidate
            values = await self._show_session(candidate)
            if not values:
                continue
            if values.get("Active", "").lower() != "yes":
                continue
            if values.get("State", "").lower() in ("closing", "lingering"):
                continue
            self._session_id = candidate
            return candidate
        self._session_id = fallback
        return fallback

    async def _show_session(self, session_id: str) -> dict[str, str]:
        target = normalized_session_id(session_id)
        if not target:
            return {}
        _code, stdout, _stderr = await _run(
            [
                self.loginctl_bin,
                "show-session",
                target,
                "-p",
                "Id",
                "-p",
                "Active",
                "-p",
                "LockedHint",
                "-p",
                "IdleHint",
                "-p",
                "IdleSinceHint",
                "-p",
                "State",
            ]
        )
        values: dict[str, str] = {}
        for line in stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
        if not target and values.get("Id"):
            self._session_id = values["Id"]
        return values


def normalized_session_id(value: str) -> str:
    text = str(value or "").strip()
    return text


def _usec_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return raw / 1_000_000.0


async def _run(command: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    return (
        int(proc.returncode or 0),
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


class PresenceMonitor:
    """Maintain active time and emit wellbeing/return triggers."""

    def __init__(
        self,
        state_path: Path,
        *,
        provider: ActivityProvider | None = None,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        idle_reset_seconds: int = DEFAULT_IDLE_RESET_SECONDS,
        work_warn_seconds: int = DEFAULT_WORK_WARN_SECONDS,
        work_repeat_seconds: int = DEFAULT_WORK_REPEAT_SECONDS,
        night_start: str = DEFAULT_NIGHT_START,
        night_end: str = DEFAULT_NIGHT_END,
        night_history_days: int = DEFAULT_NIGHT_HISTORY_DAYS,
        return_idle_seconds: int = DEFAULT_RETURN_IDLE_SECONDS,
        return_debounce_seconds: int = DEFAULT_RETURN_DEBOUNCE_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.state_path = Path(state_path).expanduser()
        self.provider = provider or LogindActivityProvider()
        self.poll_seconds = max(1, int(poll_seconds))
        self.idle_reset_seconds = max(1, int(idle_reset_seconds))
        self.work_warn_seconds = max(60, int(work_warn_seconds))
        self.work_repeat_seconds = max(60, int(work_repeat_seconds))
        self.night_start = parse_clock(night_start, DEFAULT_NIGHT_START)
        self.night_end = parse_clock(night_end, DEFAULT_NIGHT_END)
        self.night_history_days = max(1, int(night_history_days))
        self.return_idle_seconds = max(60, int(return_idle_seconds))
        self.return_debounce_seconds = max(0, int(return_debounce_seconds))
        self.logger = logger or logging.getLogger("akid.presence")
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        # A fresh monitor must never turn a pre-existing idle period into a
        # return notification. The first active sample clears this flag when
        # the monitor starts while the user is already active.
        self._state["return_suppressed"] = True
        self.last_error: str | None = None
        self.last_sample: ActivitySample | None = None
        self.last_triggers: list[PresenceTrigger] = []

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def _load_state(self) -> dict[str, Any]:
        default = {
            "version": PRESENCE_VERSION,
            "boot_id": boot_id(),
            "session_id": "",
            "initialized": False,
            "last_sample_at": None,
            "last_active_at": None,
            "active_seconds": 0.0,
            "next_work_threshold": float(self.work_warn_seconds),
            "late_night_dates": [],
            "last_trigger": None,
            "last_return_at": None,
            "last_idle_started_at": None,
            "return_candidate_since": None,
            "return_suppressed": True,
            "last_saved_at": time.time(),
        }
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(payload, dict):
            return default
        state = {**default, **payload}
        if state.get("boot_id") != default["boot_id"]:
            state.update(
                {
                    "boot_id": default["boot_id"],
                    "initialized": False,
                    "last_sample_at": None,
                    "last_active_at": None,
                    "active_seconds": 0.0,
                    "next_work_threshold": float(self.work_warn_seconds),
                    "last_idle_started_at": None,
                    "return_candidate_since": None,
                    "return_suppressed": True,
                }
            )
        state["late_night_dates"] = [
            str(item)
            for item in state.get("late_night_dates", [])
            if isinstance(item, str) and item
        ]
        return state

    def _save_state(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - float(self._state.get("last_saved_at") or 0) < 60:
            return
        self._state["last_saved_at"] = now
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(
                self.state_path,
                json.dumps(self._state, ensure_ascii=False, sort_keys=True) + "\n",
            )
            with contextlib.suppress(OSError):
                self.state_path.chmod(0o600)
        except OSError as exc:
            self.last_error = f"cannot persist presence state: {exc}"

    def process_sample(
        self,
        sample: ActivitySample,
        *,
        now: float | None = None,
    ) -> list[PresenceTrigger]:
        """Update state for one sample and return newly due trigger facts."""
        self.last_sample = sample
        self.last_triggers = []
        if not sample.valid:
            self.last_error = sample.error or "invalid activity sample"
            return []

        clock = float(now if now is not None else sample.sampled_at)
        previous = self._state
        last_sample_at = previous.get("last_sample_at")
        gap = clock - float(last_sample_at) if last_sample_at is not None else None
        triggers: list[PresenceTrigger] = []
        initialized_before = bool(previous.get("initialized"))
        if sample.active:
            if gap is not None and gap <= MAX_SAMPLE_GAP_SECONDS:
                previous["active_seconds"] = float(previous.get("active_seconds") or 0) + gap
            elif gap is not None:
                previous["active_seconds"] = 0.0
                previous["next_work_threshold"] = float(self.work_warn_seconds)
            previous["last_active_at"] = now_iso(clock)

            idle_started = previous.get("last_idle_started_at")
            if idle_started:
                idle_seconds = max(0.0, clock - float(idle_started))
                candidate = previous.get("return_candidate_since")
                if candidate is None:
                    previous["return_candidate_since"] = clock
                elif initialized_before and idle_seconds >= self.return_idle_seconds:
                    if clock - float(candidate) >= self.return_debounce_seconds:
                        if not bool(previous.get("return_suppressed")):
                            triggers.append(
                                PresenceTrigger(
                                    "return",
                                    {
                                        "idle_seconds": int(idle_seconds),
                                        "idle_started_at": now_iso(float(idle_started)),
                                        "returned_at": now_iso(clock),
                                    },
                                )
                            )
                            previous["last_return_at"] = now_iso(clock)
                        previous["return_suppressed"] = False
                        previous["last_idle_started_at"] = None
                        previous["return_candidate_since"] = None
            if not initialized_before:
                previous["initialized"] = True
                if not idle_started:
                    previous["return_suppressed"] = False

            active_seconds = float(previous.get("active_seconds") or 0)
            next_threshold = float(
                previous.get("next_work_threshold") or self.work_warn_seconds
            )
            if active_seconds >= next_threshold:
                triggers.append(
                    PresenceTrigger(
                        "work",
                        {
                            "active_seconds": int(active_seconds),
                            "active_hours": round(active_seconds / 3600.0, 2),
                            "at": now_iso(clock),
                        },
                    )
                )
                while next_threshold <= active_seconds:
                    next_threshold += self.work_repeat_seconds
                previous["next_work_threshold"] = next_threshold

            cycle = self._night_cycle(clock)
            dates = list(previous.get("late_night_dates") or [])
            if cycle and cycle not in dates:
                prior = [item for item in dates if item != cycle]
                cutoff = (
                    datetime.fromtimestamp(clock).astimezone().date()
                    - timedelta(days=self.night_history_days)
                )
                prior = [item for item in prior if _date_key(item) >= cutoff]
                triggers.append(
                    PresenceTrigger(
                        "late_night",
                        {
                            "current_time": datetime.fromtimestamp(clock)
                            .astimezone()
                            .isoformat(timespec="seconds"),
                            "cycle_date": cycle,
                            "night_work_count": len(set(prior)),
                            "history_days": self.night_history_days,
                        },
                    )
                )
                dates.append(cycle)
                dates = [
                    item
                    for item in dates
                    if _date_key(item)
                    >= datetime.fromtimestamp(clock).astimezone().date()
                    - timedelta(days=self.night_history_days)
                ]
                previous["late_night_dates"] = sorted(set(dates))
        else:
            if initialized_before and not previous.get("last_idle_started_at"):
                previous["last_idle_started_at"] = float(sample.idle_since or clock)
            elif previous.get("last_idle_started_at") is None:
                previous["last_idle_started_at"] = float(sample.idle_since or clock)
            previous["active_seconds"] = 0.0
            previous["next_work_threshold"] = float(self.work_warn_seconds)
            previous["return_candidate_since"] = None
            if not initialized_before:
                previous["initialized"] = True

        previous["last_sample_at"] = clock
        previous["session_id"] = sample.session_id
        previous["last_active"] = bool(sample.active)
        previous["last_idle_hint"] = sample.idle_hint
        previous["last_locked"] = sample.locked
        if triggers:
            previous["last_trigger"] = triggers[-1].to_dict()
        self.last_triggers = triggers
        self.last_error = None
        self._save_state(force=bool(triggers))
        return triggers

    def _night_cycle(self, clock: float) -> str | None:
        local = datetime.fromtimestamp(clock).astimezone()
        current = local.time()
        if self.night_start <= self.night_end:
            inside = self.night_start <= current < self.night_end
        else:
            inside = current >= self.night_start or current < self.night_end
        return local.date().isoformat() if inside else None

    async def run(
        self,
        on_triggers: Callable[[list[PresenceTrigger]], Awaitable[None]],
    ) -> None:
        while not self._stop.is_set():
            try:
                sample = await self.provider.sample()
                triggers = self.process_sample(sample)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"presence sample failed: {exc}"
                self.logger.exception("presence sample failed")
                triggers = []
            if triggers:
                try:
                    await on_triggers(triggers)
                except Exception:
                    self.logger.exception("presence trigger delivery failed")
            elif self.last_error:
                self.logger.debug("presence sample unavailable: %s", self.last_error)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def start(
        self,
        on_triggers: Callable[[list[PresenceTrigger]], Awaitable[None]],
    ) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self.run(on_triggers), name="akid-presence"
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            self._save_state(force=True)

    def status(self) -> dict[str, Any]:
        sample = self.last_sample
        return {
            "enabled": self._task is not None,
            "state_path": str(self.state_path),
            "poll_seconds": self.poll_seconds,
            "session_id": self._state.get("session_id"),
            "active_seconds": int(float(self._state.get("active_seconds") or 0)),
            "next_work_threshold": int(
                float(
                    self._state.get("next_work_threshold")
                    or self.work_warn_seconds
                )
            ),
            "idle_hint": sample.idle_hint if sample is not None else None,
            "locked": sample.locked if sample is not None else None,
            "valid": sample.valid if sample is not None else None,
            "last_sample_at": self._state.get("last_sample_at"),
            "last_trigger": self._state.get("last_trigger"),
            "late_night_dates": list(self._state.get("late_night_dates") or []),
            "last_error": self.last_error,
        }


def _date_key(value: str) -> Any:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return datetime.min.date()


def render_trigger_facts(triggers: Iterable[PresenceTrigger]) -> str:
    lines = []
    for trigger in triggers:
        facts = " ".join(
            f'{key}="{value}"' for key, value in sorted(trigger.facts.items())
        )
        lines.append(f'<trigger type="{trigger.kind}" {facts}/>')
    return "\n".join(lines)
