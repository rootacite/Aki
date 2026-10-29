"""Turn activity samples into work, night, and return triggers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import (
    datetime,
    timedelta,
)
from pathlib import Path
from engine.aki_config import atomic_write
from .logind import LogindActivityProvider
from .types import (
    DEFAULT_IDLE_RESET_SECONDS,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_HISTORY_DAYS,
    DEFAULT_NIGHT_START,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RETURN_DEBOUNCE_SECONDS,
    DEFAULT_RETURN_IDLE_SECONDS,
    DEFAULT_WORK_REPEAT_SECONDS,
    DEFAULT_WORK_WARN_SECONDS,
    MAX_SAMPLE_GAP_SECONDS,
    PRESENCE_VERSION,
    PresenceTrigger,
    _date_key,
    boot_id,
    now_iso,
    parse_clock,
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
        self.last_sample_valid: bool | None = None
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
            "return_resume_idle_seconds": None,
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
                    "return_resume_idle_seconds": None,
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
        self.last_sample_valid = sample.valid
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
                # Freeze the idle duration at the moment activity resumes.
                # A short pause must never become a "return" just because the
                # user keeps working afterwards.
                candidate = previous.get("return_candidate_since")
                if candidate is None:
                    resume_idle_seconds = max(0.0, clock - float(idle_started))
                    if resume_idle_seconds < self.return_idle_seconds:
                        previous["last_idle_started_at"] = None
                        previous["return_candidate_since"] = None
                        previous["return_resume_idle_seconds"] = None
                    else:
                        previous["return_candidate_since"] = clock
                        previous["return_resume_idle_seconds"] = resume_idle_seconds
                elif initialized_before:
                    resume_idle_seconds = float(
                        previous.get("return_resume_idle_seconds")
                        or max(0.0, clock - float(idle_started))
                    )
                    if (
                        resume_idle_seconds >= self.return_idle_seconds
                        and clock - float(candidate) >= self.return_debounce_seconds
                    ):
                        if not bool(previous.get("return_suppressed")):
                            triggers.append(
                                PresenceTrigger(
                                    "return",
                                    {
                                        "idle_seconds": int(resume_idle_seconds),
                                        "idle_started_at": now_iso(float(idle_started)),
                                        "returned_at": now_iso(clock),
                                    },
                                )
                            )
                            previous["last_return_at"] = now_iso(clock)
                        else:
                            self.logger.info(
                                "presence return suppressed after monitor startup "
                                "idle_seconds=%s",
                                int(resume_idle_seconds),
                            )
                        previous["return_suppressed"] = False
                        previous["last_idle_started_at"] = None
                        previous["return_candidate_since"] = None
                        previous["return_resume_idle_seconds"] = None
            elif not idle_started:
                # The monitor may have restarted while the user was active.
                # Clear the startup suppression on the first active sample so a
                # later idle -> return cycle still produces a trigger.
                previous["return_suppressed"] = False
            if not initialized_before:
                previous["initialized"] = True

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
            previous["return_resume_idle_seconds"] = None
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
        on_sample: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        while not self._stop.is_set():
            try:
                sample = await self.provider.sample()
                triggers = self.process_sample(sample)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"presence sample failed: {exc}"
                self.last_sample_valid = False
                self.logger.exception("presence sample failed")
                triggers = []
            if on_sample is not None:
                try:
                    await on_sample()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception("presence status delivery failed")
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
        on_sample: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self.run(on_triggers, on_sample), name="akid-presence"
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
            self.provider.close()
        with contextlib.suppress(Exception):
            self._save_state(force=True)

    def status(self) -> dict[str, Any]:
        sample = self.last_sample
        result = {
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
        provider_status = getattr(self.provider, "status", None)
        if callable(provider_status):
            with contextlib.suppress(Exception):
                result["provider"] = provider_status()
        return result

    def layer_status(self, *, now: float | None = None) -> dict[str, Any]:
        """Return the compact presence facts shown by the aki-layer overlay."""
        clock = time.time() if now is None else float(now)
        state = self._state
        sample = self.last_sample
        last_input_at = self._last_input_at()
        raw_active = state.get("last_active")
        active = raw_active if isinstance(raw_active, bool) else None
        active_seconds = (
            int(float(state.get("active_seconds") or 0)) if active else 0
        )
        sampled_at = state.get("last_sample_at")
        sampled_at_ms = (
            int(float(sampled_at) * 1000) if sampled_at is not None else None
        )
        idle_started = state.get("last_idle_started_at")
        idle_seconds = 0
        if active is False and idle_started is not None:
            idle_seconds = max(0, int(clock - float(idle_started)))
        away = bool(
            active is False and idle_seconds >= self.return_idle_seconds
        )
        last_input_at_ms = (
            int(last_input_at * 1000) if last_input_at is not None else None
        )
        no_input_seconds = (
            max(0, int(clock - last_input_at))
            if last_input_at is not None
            else None
        )
        return {
            "active": active,
            "activeSeconds": active_seconds,
            "activeSinceMs": (
                int((clock - active_seconds) * 1000) if active else None
            ),
            "idleSeconds": idle_seconds,
            "away": away,
            "returnIdleSeconds": self.return_idle_seconds,
            "pollSeconds": self.poll_seconds,
            "lastInputAtMs": last_input_at_ms,
            "noInputSeconds": no_input_seconds,
            "sampledAtMs": sampled_at_ms,
            "valid": self.last_sample_valid,
        }

    def _last_input_at(self) -> float | None:
        candidates = (self.provider, getattr(self.provider, "input_probe", None))
        for candidate in candidates:
            if candidate is None:
                continue
            value = getattr(candidate, "last_activity_at", None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None
