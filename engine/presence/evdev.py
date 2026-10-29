"""Read user input activity from evdev devices."""

from __future__ import annotations

import array
import contextlib
import errno
import fcntl
import logging
import os
import select
import time
from pathlib import Path
from .types import (
    EV_KEY,
    EV_REL,
    IGNORED_KEYS,
    INPUT_RESCAN_SECONDS,
    KEY_MAX,
    now_iso,
)



def _eviocgbit(ev_type: int, length: int) -> int:
    # _IOC(_IOC_READ, 'E', 0x20 + ev_type, length) on Linux.
    return (2 << 30) | (length << 16) | (ord("E") << 8) | (0x20 + ev_type)



def _capability_bits(fd: int, ev_type: int, max_code: int) -> array.array | None:
    length = (max_code + 8) // 8
    buf = array.array("B", [0]) * length
    try:
        fcntl.ioctl(fd, _eviocgbit(ev_type, length), buf, True)
    except OSError:
        return None
    return buf



def _has_bit(bits: array.array, bit: int) -> bool:
    return bool(bits[bit // 8] & (1 << (bit % 8)))



def _is_user_input_device(fd: int) -> bool:
    """Reject audio jacks, LEDs and power/sleep buttons."""
    event_types = _capability_bits(fd, 0, 31)
    if event_types is None:
        return False
    if _has_bit(event_types, EV_REL):
        return True
    if not _has_bit(event_types, EV_KEY):
        return False
    keys = _capability_bits(fd, EV_KEY, KEY_MAX)
    if keys is None:
        return False
    return any(
        code not in IGNORED_KEYS and _has_bit(keys, code)
        for code in range(KEY_MAX + 1)
    )



class EvdevActivityProbe:
    """Compositor-independent keyboard/pointer activity.

    ``logind`` idle hints are optional and KDE Wayland can leave ``IdleHint``
    at ``no`` forever.  Input events are queued by evdev until someone reads
    them, so polling this probe every few seconds is enough to tell whether
    the user actually touched the keyboard or pointer.
    """

    def __init__(
        self,
        device_dir: str | Path = "/dev/input",
        *,
        rescan_seconds: int = INPUT_RESCAN_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.device_dir = Path(device_dir).expanduser()
        self.rescan_seconds = max(1, int(rescan_seconds))
        self.logger = logger or logging.getLogger("akid.presence")
        self._fds: dict[str, int] = {}
        self._last_activity_at: float | None = None
        self._last_scan_at = float("-inf")
        self._ever_opened = False
        self._closed = False
        self._unavailable_logged = False

    @property
    def available(self) -> bool:
        return bool(self._fds)

    @property
    def ever_available(self) -> bool:
        return self._ever_opened

    @property
    def last_activity_at(self) -> float | None:
        return self._last_activity_at

    def poll(self, now: float | None = None) -> float | None:
        """Drain queued input events and return the last input timestamp."""
        if self._closed:
            return self._last_activity_at
        clock = time.time() if now is None else float(now)
        self._scan(clock)
        if not self._fds:
            return self._last_activity_at
        try:
            readable, _, _ = select.select(list(self._fds.values()), [], [], 0)
        except (OSError, ValueError) as exc:
            self.logger.debug("presence input probe select failed: %s", exc)
            for key in list(self._fds):
                self._close_device(key)
            self._last_scan_at = float("-inf")
            return self._last_activity_at
        saw_input = False
        for fd in readable:
            if self._drain(fd):
                saw_input = True
        if saw_input:
            self._last_activity_at = clock
        elif self._last_activity_at is None:
            # Give the first sample an idle window instead of assuming that a
            # freshly started monitor has been idle forever.
            self._last_activity_at = clock
        return self._last_activity_at

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "ever_available": self._ever_opened,
            "devices": sorted(Path(path).name for path in self._fds),
            "last_activity_at": (
                now_iso(self._last_activity_at)
                if self._last_activity_at is not None
                else None
            ),
        }

    def close(self) -> None:
        for fd in self._fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds.clear()
        self._closed = True

    def _scan(self, now: float) -> None:
        if now - self._last_scan_at < self.rescan_seconds:
            return
        self._last_scan_at = now
        try:
            paths = sorted(self.device_dir.glob("event*"))
        except OSError:
            paths = []
        for path in paths:
            key = str(path)
            if key in self._fds:
                continue
            fd = self._open_device(key)
            if fd is not None:
                self._fds[key] = fd
                self._ever_opened = True
        for key in list(self._fds):
            if not Path(key).exists():
                self._close_device(key)
        if paths and not self._fds and not self._unavailable_logged:
            self.logger.warning(
                "presence input probe found no readable devices under %s; "
                "falling back to logind idle hints",
                self.device_dir,
            )
            self._unavailable_logged = True

    def _open_device(self, path: str) -> int | None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            self.logger.debug("presence input probe cannot open %s: %s", path, exc)
            return None
        try:
            if not _is_user_input_device(fd):
                os.close(fd)
                return None
        except OSError:
            os.close(fd)
            return None
        return fd

    def _drain(self, fd: int) -> bool:
        saw_input = False
        while True:
            try:
                data = os.read(fd, 24 * 64)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EINTR):
                    self._drop_fd(fd)
                break
            if not data:
                self._drop_fd(fd)
                break
            saw_input = True
            if len(data) < 24 * 64:
                break
        return saw_input

    def _drop_fd(self, fd: int) -> None:
        for key, value in list(self._fds.items()):
            if value == fd:
                self._close_device(key)
                break

    def _close_device(self, key: str) -> None:
        fd = self._fds.pop(key, None)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
