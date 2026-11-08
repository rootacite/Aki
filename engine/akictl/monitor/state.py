"""State models for the monitor TUI."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass
class ApiCallRecord:
    request_id: str
    profile: str
    seq: int = 0
    timestamp: str = ""
    path: str = ""
    requested_model: str = ""
    upstream_model: str = ""
    upstream_url: str = ""
    status: int = 0
    duration_ms: int = 0
    body_bytes: int = 0
    body_sha256: str = ""
    tool_names: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    chunks_count: int = 0
    total_chunk_bytes: int = 0
    first_chunk_at: float = 0.0
    created_at: float = field(default_factory=time.monotonic)
    usage: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    response_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    completed: bool = False
    error: str = ""

    @property
    def ttft_ms(self) -> int:
        if self.first_chunk_at > 0.0:
            return int((self.first_chunk_at - self.created_at) * 1000)
        return self.duration_ms

    @property
    def prompt_tokens(self) -> int:
        return int(
            self.usage.get("prompt_tokens")
            or self.timings.get("prompt_n")
            or 0
        )

    @property
    def completion_tokens(self) -> int:
        return int(
            self.usage.get("completion_tokens")
            or self.timings.get("predicted_n")
            or 0
        )

    @property
    def cached_tokens(self) -> int:
        details = self.usage.get("prompt_tokens_details")
        if isinstance(details, Mapping) and "cached_tokens" in details:
            return int(details["cached_tokens"] or 0)
        return int(self.timings.get("cache_n") or 0)

    @property
    def cache_rate(self) -> float:
        pt = self.prompt_tokens
        if pt > 0:
            return (self.cached_tokens / pt) * 100.0
        return 0.0


@dataclass
class AgentSnapshot:
    profile: str
    name: str = ""
    agent_id: str = ""
    status: str = "offline"  # "group", "solo", "offline"
    group_model: str = ""
    solo_model: str = ""
    reasoning_effort: str = "low"
    description: str = ""
    memfs_path: Path | None = None
    git_commits: list[dict[str, str]] = field(default_factory=list)
    live_budget_tokens: int = 65536
    solo_budget_tokens: int = 65536
    channel_messages: list[dict[str, Any]] = field(default_factory=list)
    solo_sessions: list[dict[str, Any]] = field(default_factory=list)
    group_context_messages: list[dict[str, Any]] = field(default_factory=list)
    solo_context_messages: list[dict[str, Any]] = field(default_factory=list)


class MonitorState:
    """Reactive state container for the TUI monitor."""

    def __init__(self, max_records: int = 200) -> None:
        self.max_records = max_records
        self.api_calls: deque[ApiCallRecord] = deque(maxlen=max_records)
        self.api_calls_by_id: dict[str, ApiCallRecord] = {}
        self.previous_call_by_profile: dict[str, ApiCallRecord] = {}
        self.agents: dict[str, AgentSnapshot] = {}

        # Navigation state
        self.active_page: int = 1  # 1 to 6
        self.active_focus: str = "list"  # "sidebar", "list", "detail"
        self.selected_call_id: str = ""
        self.selected_agent_profile: str = "aki"
        self.selected_detail_tab: int = 0  # 0: Calibration, 1: Fork Diff, 2: Cache, 3: Network, 4: Stream, 5: JSON
        self.selected_agent_tab: int = 0   # 0: Profile, 1: MemFS, 2: Channel, 3: Solo, 4: Context
        self.context_view_mode: str = "group"  # "group" or "solo"

        # Filters and switches
        self.filter_profile: str = "all"  # "all", "aki", "fuyu", "haru", "natsu"
        self.paused: bool = False
        self.nsfw_unlocked: bool = False

        # Modals / Dialogs
        self.active_modal: Any = None
        self.status_message: str = ""
        self.status_message_until: float = 0.0

        # Change listeners
        self._on_change: list[Callable[[], None]] = []

    def register_change_listener(self, callback: Callable[[], None]) -> None:
        self._on_change.append(callback)

    def notify_change(self) -> None:
        for cb in self._on_change:
            try:
                cb()
            except Exception:
                pass

    def set_status(self, message: str, duration: float = 3.0) -> None:
        self.status_message = message
        self.status_message_until = time.monotonic() + duration
        self.notify_change()

    def get_status(self) -> str:
        if time.monotonic() < self.status_message_until:
            return self.status_message
        return ""

    def add_or_update_call(self, record: ApiCallRecord) -> None:
        if self.paused and record.request_id not in self.api_calls_by_id:
            return
        if record.request_id in self.api_calls_by_id:
            existing = self.api_calls_by_id[record.request_id]
            # Update fields in-place
            existing.status = record.status or existing.status
            existing.duration_ms = record.duration_ms or existing.duration_ms
            existing.chunks_count = record.chunks_count or existing.chunks_count
            existing.total_chunk_bytes = record.total_chunk_bytes or existing.total_chunk_bytes
            existing.usage = record.usage or existing.usage
            existing.timings = record.timings or existing.timings
            existing.response_text = record.response_text or existing.response_text
            existing.response_tool_calls = record.response_tool_calls or existing.response_tool_calls
            existing.completed = record.completed or existing.completed
            existing.error = record.error or existing.error
        else:
            self.api_calls.append(record)
            self.api_calls_by_id[record.request_id] = record
            if not self.selected_call_id:
                self.selected_call_id = record.request_id

        if record.completed:
            self.previous_call_by_profile[record.profile] = record
        self.notify_change()

    def filtered_calls(self) -> list[ApiCallRecord]:
        calls = list(self.api_calls)
        if self.filter_profile and self.filter_profile != "all":
            calls = [c for c in calls if c.profile == self.filter_profile]
        return calls

    def get_selected_call(self) -> ApiCallRecord | None:
        if self.selected_call_id and self.selected_call_id in self.api_calls_by_id:
            return self.api_calls_by_id[self.selected_call_id]
        calls = self.filtered_calls()
        if calls:
            return calls[-1]
        return None

    def get_previous_call(self, record: ApiCallRecord) -> ApiCallRecord | None:
        calls = self.filtered_calls()
        idx = -1
        for i, c in enumerate(calls):
            if c.request_id == record.request_id:
                idx = i
                break
        if idx > 0:
            return calls[idx - 1]
        # Fallback to last recorded completed call for this profile
        prev = self.previous_call_by_profile.get(record.profile)
        if prev and prev.request_id != record.request_id:
            return prev
        return None
