"""Device-wide advisory lock for tools with non-read-only effects."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from engine.aki_config import atomic_write

LOCK_VERSION = 1


def default_execution_lock_path() -> Path:
    override = os.environ.get("AKI_EXECUTION_LOCK_PATH")
    if override:
        return Path(override).expanduser()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return runtime / "aki" / "execution.lock"


def default_execution_lock_status_path(lock_path: Path | None = None) -> Path:
    override = os.environ.get("AKI_EXECUTION_LOCK_STATUS")
    if override:
        return Path(override).expanduser()
    path = lock_path or default_execution_lock_path()
    return path.with_name(f"{path.name}.json")


class ExecutionLock:
    """A crash-safe cross-process lock held by the akid process."""

    def __init__(
        self,
        path: Path | None = None,
        status_path: Path | None = None,
    ) -> None:
        self.path = (path or default_execution_lock_path()).expanduser()
        self.status_path = (
            status_path or default_execution_lock_status_path(self.path)
        ).expanduser()
        self._fd: int | None = None
        self._holder: dict[str, Any] | None = None
        self.last_error: str | None = None

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def _read_status(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_stale(self, holder: dict[str, Any] | None) -> None:
        if not holder or self._pid_alive(holder.get("pid")):
            return
        try:
            self.status_path.unlink()
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        holder = self._read_status()
        self._cleanup_stale(holder)
        if holder is not None and not self.status_path.exists():
            holder = None
        return {
            "path": str(self.path),
            "status_path": str(self.status_path),
            "held": holder is not None,
            "holder": holder,
            "last_error": self.last_error,
        }

    def acquire(
        self,
        *,
        profile: str,
        agent_id: str,
        turn_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        self._ensure_parent()
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._read_status()
            self._cleanup_stale(holder)
            os.close(fd)
            if holder is not None and not self.status_path.exists():
                holder = None
            return False, holder or {}
        except Exception:
            os.close(fd)
            raise

        holder = {
            "version": LOCK_VERSION,
            "profile": profile,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "pid": os.getpid(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            atomic_write(
                self.status_path,
                json.dumps(holder, ensure_ascii=False, sort_keys=True) + "\n",
            )
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._fd = fd
        self._holder = holder
        self.last_error = None
        return True, dict(holder)

    def release(self, *, profile: str | None = None, turn_id: str | None = None) -> bool:
        holder = self._holder
        if holder is None:
            return False
        if profile is not None and holder.get("profile") != profile:
            return False
        if turn_id is not None and holder.get("turn_id") != turn_id:
            return False
        try:
            self.status_path.unlink()
        except OSError:
            pass
        fd = self._fd
        self._fd = None
        self._holder = None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return True

    def held_by(self, *, profile: str, turn_id: str) -> bool:
        holder = self._holder
        return bool(
            holder
            and holder.get("profile") == profile
            and holder.get("turn_id") == turn_id
        )

    def close(self) -> None:
        self.release()
