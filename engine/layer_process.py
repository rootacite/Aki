"""Supervision of the aki-layer Qt Quick layer-shell child process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections import deque
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_RESTART_BACKOFF = (1, 2, 5, 10, 30)


def default_layer_binary() -> Path:
    """Return the in-repo build of the aki-layer executable."""
    return Path(__file__).resolve().parent.parent / "aki-layer" / "build" / "aki-layer"


class LayerProcess:
    """Run and supervise the long-lived aki-layer GUI process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        backoff: Sequence[int] = DEFAULT_RESTART_BACKOFF,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else dict(os.environ)
        self.logger = logger or logging.getLogger("akid.layer")
        self.backoff = tuple(backoff)

        self._proc: asyncio.subprocess.Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._state = "stopped"
        self._restart_count = 0
        self._last_error: str | None = None
        self._started_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stopping = False
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name="aki-layer-supervisor"
        )

    def status(self) -> dict[str, object]:
        return {
            "state": self._state,
            "pid": self.pid,
            "command": list(self.command),
            "cwd": self.cwd,
            "restart_count": self._restart_count,
            "last_error": self._last_error,
            "started_at": self._started_at,
            "stderr_tail": list(self._stderr_tail),
        }

    async def _supervise(self) -> None:
        backoff_index = 0
        while not self._stopping:
            self._state = "starting" if backoff_index == 0 else "restarting"
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self._last_error = f"spawn failed: {exc}"
                self.logger.error("aki-layer spawn failed: %s", exc)
                if not await self._sleep_backoff(backoff_index):
                    break
                backoff_index = min(backoff_index + 1, len(self.backoff) - 1)
                continue

            self._state = "running"
            self._started_at = time.time()
            self._last_error = None
            self._stderr_tail.clear()
            self.logger.info("aki-layer running pid=%s", self._proc.pid)
            self._stderr_task = asyncio.create_task(
                self._read_stderr(self._proc), name="aki-layer-stderr"
            )
            returncode = await self._proc.wait()
            self._proc = None
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None

            if self._stopping:
                break

            self._restart_count += 1
            detail = self._stderr_tail[-1] if self._stderr_tail else None
            self._last_error = (
                f"aki-layer exited with code {returncode}"
                + (f": {detail}" if detail else "")
            )
            self.logger.warning(
                "aki-layer exited code=%s restart=%s",
                returncode,
                self._restart_count,
            )
            self._state = "restarting"
            if not await self._sleep_backoff(backoff_index):
                break
            backoff_index = min(backoff_index + 1, len(self.backoff) - 1)

        self._state = "stopped"
        self._proc = None

    async def _sleep_backoff(self, index: int) -> bool:
        delay = self.backoff[min(index, len(self.backoff) - 1)] if self.backoff else 0
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        return not self._stopping

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            self._stderr_tail.append(text[:400])
            self.logger.info("aki-layer: %s", text[:400])

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=3)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor_task
            self._supervisor_task = None
        self._proc = None
        self._state = "stopped"
