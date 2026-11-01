"""Delivery of akid chat messages to aki-layer notification bubbles."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
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


_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_FENCE_RE = re.compile(r"(?m)^\s*(```|~~~).*$")


def conceal_hidden_spans(content: str) -> str:
    """Drop complete ``~[text]`` spans before a desktop bubble is drawn.

    The first ``]`` ends a span. An unclosed ``~[`` stays visible. Spans that
    overlap inline code or a fenced code block stay literal. This matches the
    Tokifactor display filter; the published message is left unchanged.
    """
    text = str(content or "")
    if "~[" not in text:
        return text
    protected = _protected_indexes(text)
    spans = _hidden_span_ranges(text, protected)
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    joined = pieces[0]
    for piece in pieces[1:]:
        joined = _join_around_removal(joined, piece)
    return joined


def _protected_indexes(content: str) -> list[bool]:
    protected = [False] * len(content)
    for match in _INLINE_CODE_RE.finditer(content):
        for index in range(match.start(), match.end()):
            protected[index] = True
    fence_start: int | None = None
    for match in _FENCE_RE.finditer(content):
        if fence_start is None:
            fence_start = match.start()
            continue
        for index in range(fence_start, match.end()):
            protected[index] = True
        fence_start = None
    if fence_start is not None:
        for index in range(fence_start, len(content)):
            protected[index] = True
    return protected


def _hidden_span_ranges(content: str, protected: list[bool]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(content)
    while index < length:
        if protected[index]:
            index += 1
            continue
        if content.startswith("~[", index):
            close = content.find("]", index + 2)
            if close < 0:
                break
            if any(protected[cursor] for cursor in range(index, close + 1)):
                index += 2
                continue
            spans.append((index, close + 1))
            index = close + 1
            continue
        index += 1
    return spans


def _is_horizontal_space(char: str) -> bool:
    return char in " \t"


def _is_line_break(char: str) -> bool:
    return char in "\r\n"


def _trim_end_horizontal(text: str) -> str:
    end = len(text)
    while end > 0 and _is_horizontal_space(text[end - 1]):
        end -= 1
    return text[:end]


def _trim_start_horizontal(text: str) -> str:
    start = 0
    while start < len(text) and _is_horizontal_space(text[start]):
        start += 1
    return text[start:]


def _join_around_removal(left: str, right: str) -> str:
    left_trimmed = _trim_end_horizontal(left)
    right_trimmed = _trim_start_horizontal(right)
    removed_left = len(left_trimmed) != len(left)
    removed_right = len(right_trimmed) != len(right)
    if not removed_left and not removed_right:
        return left + right
    need_space = (
        bool(left_trimmed)
        and bool(right_trimmed)
        and not _is_line_break(left_trimmed[-1])
        and not _is_line_break(right_trimmed[0])
    )
    if need_space:
        return left_trimmed + " " + right_trimmed
    return left_trimmed + right_trimmed


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
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=queue_max)
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
        for paragraph in split_paragraphs(conceal_hidden_spans(text)):
            preview = preview_text(paragraph, self.preview_chars)
            if not preview:
                continue
            if not self._enqueue({"character": character, "text": preview}):
                break
            queued = True
        return queued

    def notify_presence(self, presence: Mapping[str, object]) -> bool:
        """Publish one presence snapshot for the aki-layer status card."""
        return self._enqueue(
            {"kind": "presence", "presence": dict(presence)}
        )

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

    def _enqueue(self, payload: dict[str, object]) -> bool:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            self._warn("aki-layer notify queue is full")
            return False
        return True

    async def _send(self, payload: Mapping[str, object]) -> None:
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
