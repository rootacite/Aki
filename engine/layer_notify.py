"""Delivery of akid chat messages to aki-layer notification bubbles."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Mapping

DEFAULT_PREVIEW_CHARS = 80
DEFAULT_QUEUE_MAX = 64
DEFAULT_SEND_TIMEOUT = 1.0
DEFAULT_WARN_INTERVAL = 60.0

CHARACTER_ALIASES = {
    "aki": "Aki",
    "秋": "Aki",
    "小秋": "Aki",
    "fuyu": "Fuyu",
    "冬": "Fuyu",
    "haru": "Haru",
    "春": "Haru",
    "natsu": "Natsu",
    "夏": "Natsu",
}


def preview_text(text: str, limit: int = DEFAULT_PREVIEW_CHARS) -> str:
    """Collapse whitespace and keep a notification-sized preview."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    if limit == 1:
        return "…"
    return collapsed[: limit - 1].rstrip() + "…"


def split_paragraphs(content: str) -> list[str]:
    """Split an agent reply at blank lines, keeping fenced code intact."""
    if not str(content or "").strip():
        return [str(content or "")]
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    paragraphs: list[str] = []
    current: list[str] = []
    pending_blank = False
    fence: tuple[str, int] | None = None

    def flush() -> None:
        nonlocal pending_blank
        if any(entry.strip() for entry in current):
            paragraphs.append("\n".join(current))
        current.clear()
        pending_blank = False

    def opening_fence(line: str) -> tuple[str, int] | None:
        trimmed = line.lstrip()
        if not trimmed or trimmed[0] not in "`~":
            return None
        character = trimmed[0]
        length = len(trimmed) - len(trimmed.lstrip(character))
        return (character, length) if length >= 3 else None

    def closing_fence(line: str, active: tuple[str, int]) -> bool:
        trimmed = line.lstrip()
        character, length = active
        run = len(trimmed) - len(trimmed.lstrip(character))
        return run >= length and not trimmed[run:].strip()

    for line in lines:
        if fence is not None:
            current.append(line)
            if closing_fence(line, fence):
                fence = None
                flush()
            continue

        opening = opening_fence(line)
        if opening is not None:
            if any(entry.strip() for entry in current):
                flush()
            current.append(line)
            fence = opening
            continue

        if not line.strip():
            pending_blank = True
            continue
        if pending_blank:
            flush()
        current.append(line)

    flush()
    return paragraphs if len(paragraphs) > 1 else [content]


def character_for_names(*names: object) -> str | None:
    """Resolve known member names/aliases to a bubble theme character."""
    for name in names:
        key = str(name or "").strip().casefold()
        if key in CHARACTER_ALIASES:
            return CHARACTER_ALIASES[key]
    return None


def character_for_profile(profile: str, fallback: str = "") -> str:
    return character_for_names(profile, fallback) or "Aki"


class LayerNotifier:
    """Queue final messages and write them to the aki-layer control socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
        queue_max: int = DEFAULT_QUEUE_MAX,
        send_timeout: float = DEFAULT_SEND_TIMEOUT,
        warn_interval: float = DEFAULT_WARN_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.preview_chars = preview_chars
        self.send_timeout = send_timeout
        self.warn_interval = warn_interval
        self.logger = logger or logging.getLogger("akid.layer")
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_max)
        self._task: asyncio.Task[None] | None = None
        self._sent = 0
        self._failed = 0
        self._dropped = 0
        self._last_error: str | None = None
        self._last_sent_at: float | None = None
        self._last_warning_at = 0.0

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="aki-layer-notifier")

    def notify(self, character: str, text: str) -> bool:
        queued = False
        for paragraph in split_paragraphs(text):
            preview = preview_text(paragraph, self.preview_chars)
            if not preview:
                continue
            try:
                self._queue.put_nowait({"character": character, "text": preview})
            except asyncio.QueueFull:
                self._dropped += 1
                self._warn("aki-layer notify queue is full")
                break
            queued = True
        return queued

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def status(self) -> dict[str, object]:
        return {
            "state": "running" if self._task is not None else "stopped",
            "socket": str(self.socket_path),
            "queued": self._queue.qsize(),
            "sent": self._sent,
            "failed": self._failed,
            "dropped": self._dropped,
            "last_error": self._last_error,
            "last_sent_at": self._last_sent_at,
        }

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._send(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed += 1
                self._last_error = str(exc)
                self._warn(f"aki-layer notify failed: {exc}")
            else:
                self._sent += 1
                self._last_error = None
                self._last_sent_at = time.time()
            finally:
                self._queue.task_done()

    async def _send(self, payload: Mapping[str, str]) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path)),
            timeout=self.send_timeout,
        )
        try:
            line = json.dumps(
                dict(payload), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            writer.write(line + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=self.send_timeout)
            with contextlib.suppress(asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(reader.readline(), timeout=self.send_timeout)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _warn(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning_at < self.warn_interval:
            return
        self._last_warning_at = now
        self.logger.warning(message)
