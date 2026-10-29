"""Sample the active session through loginctl."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from .types import (
    ActivityProvider,
    ActivitySample,
    DEFAULT_IDLE_RESET_SECONDS,
    normalized_session_id,
)



class LogindActivityProvider(ActivityProvider):
    def __init__(
        self,
        *,
        loginctl_bin: str = "loginctl",
        session_id: str = "",
        input_probe: EvdevActivityProbe | None = None,
        input_idle_seconds: int = DEFAULT_IDLE_RESET_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.loginctl_bin = loginctl_bin
        self.configured_session_id = str(session_id or "").strip()
        self.input_probe = input_probe
        self.input_idle_seconds = max(1, int(input_idle_seconds))
        self.logger = logger or logging.getLogger("akid.presence")
        self._session_id = ""

    async def sample(self) -> ActivitySample:
        try:
            clock = time.time()
            input_last: float | None = None
            probe_ok = self.input_probe is not None
            if self.input_probe is not None:
                try:
                    input_last = self.input_probe.poll(clock)
                except Exception:
                    self.logger.exception("presence input probe failed")
                    probe_ok = False
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
            input_active: bool | None = None
            if (
                probe_ok
                and self.input_probe is not None
                and self.input_probe.ever_available
            ):
                if not self.input_probe.available:
                    input_active = False
                elif input_last is None:
                    input_active = True
                else:
                    input_active = (
                        clock - float(input_last) <= self.input_idle_seconds
                    )
                if input_active:
                    # evdev observed real input; a stale logind IdleSinceHint
                    # must not leak into the next idle transition.
                    idle_since = None
                else:
                    idle_since = input_last or idle_since
            if input_active is None:
                active = session_active and not locked and not idle_hint
            else:
                active = session_active and not locked and input_active
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

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "input": (
                self.input_probe.status() if self.input_probe is not None else None
            ),
            "input_idle_seconds": self.input_idle_seconds,
        }

    def close(self) -> None:
        if self.input_probe is not None:
            self.input_probe.close()

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
