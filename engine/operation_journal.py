"""Small in-memory tool/approval journal for return-trigger context."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

MAX_JOURNAL_ENTRIES = 64
MAX_ARGUMENT_CHARS = 256


def summarize(value: Any, limit: int = MAX_ARGUMENT_CHARS) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass
class OperationEntry:
    call_id: str
    tool: str
    arguments: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error": self.error,
        }


class OperationJournal:
    def __init__(self, max_entries: int = MAX_JOURNAL_ENTRIES) -> None:
        self._entries: deque[OperationEntry] = deque(maxlen=max(1, int(max_entries)))
        self._by_call: dict[str, OperationEntry] = {}
        self.pending_approvals = 0

    def start(self, call_id: str, tool: str, arguments: Any = None) -> OperationEntry:
        key = str(call_id or f"local-{time.monotonic_ns()}")
        entry = OperationEntry(
            call_id=key,
            tool=str(tool or "unknown"),
            arguments=summarize(arguments),
        )
        self._entries.append(entry)
        self._by_call[key] = entry
        if len(self._by_call) > self._entries.maxlen:
            self._by_call = {
                item.call_id: item for item in self._entries if item.call_id
            }
        return entry

    def finish(
        self,
        call_id: str,
        *,
        status: str = "completed",
        error: Any = "",
    ) -> OperationEntry | None:
        entry = self._by_call.get(str(call_id or ""))
        if entry is None:
            return None
        entry.finished_at = time.time()
        entry.status = str(status or "completed")
        entry.error = summarize(error, 160)
        return entry

    def approval_started(self) -> None:
        self.pending_approvals += 1

    def approval_finished(self) -> None:
        self.pending_approvals = max(0, self.pending_approvals - 1)

    def outstanding(self) -> list[dict[str, Any]]:
        return [
            entry.to_dict()
            for entry in self._entries
            if entry.status == "running"
        ]

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in list(self._entries)[-max(1, limit) :]]

    def snapshot(self) -> dict[str, Any]:
        return {
            "entries": self.recent(10),
            "outstanding": self.outstanding(),
            "pending_approvals": self.pending_approvals,
        }
