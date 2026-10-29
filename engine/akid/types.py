"""Turn, approval, and per-member channel state records."""

from __future__ import annotations

import asyncio
from dataclasses import (
    dataclass,
    field,
)



@dataclass
class ChannelTurnState:
    segment: int = 0
    text: str = ""
    last_text: str = ""
    finalized: bool = False
    no_reply: bool = False
    origin_is_agent: bool = False
    initiation_charged: bool = False
    suppressed: bool = False
    guard_blocked: bool = False
    tool_names: dict[str, str] = field(default_factory=dict)
    wake: str = ""
    wake_ids: str = ""
    client_message_id: str = ""



@dataclass
class Approval:
    approval_id: str
    turn: "Turn"
    future: asyncio.Future[tuple[bool, str | None]]
    tool_name: str
    tool_call_id: str



@dataclass
class Turn:
    turn_id: str
    conn: Connection
    message: str
    include_reasoning: bool
    client_message_id: str | None
    origin: str = "mqtt"
    origin_sender_id: str = ""
    origin_is_agent: bool = False
    lock_authorized: bool = True
    lock_authorization: str = "local-turn"
    lock_held: bool = False
    channel_message_seq: int | None = None
    channel_participants: frozenset[str] = frozenset()
    channel_sender: str = ""
    channel_text: str = ""
    channel_attachments: tuple[ChannelImageAttachment, ...] = ()
    conversation_snapshot: dict[str, Any] | None = None
    bridge_snapshot_id: str | None = None
    bridge_turn_started: bool = False
    busy_messages: list[dict[str, Any]] = field(default_factory=list)
    user_message_event: asyncio.Event = field(default_factory=asyncio.Event)
    wait_user_call_active: bool = False
    overlay_files: dict[int, Path] = field(default_factory=dict)
    overlay_consumed_seqs: set[int] = field(default_factory=set)
    wait_user_eligible_seqs: set[int] = field(default_factory=set)
    waited_user_seqs: set[int] = field(default_factory=set)
    wait_user_used: bool = False
    merged_user_messages: list[dict[str, Any]] = field(default_factory=list)
    user_role_entry_id: str = ""
    waited_agent_ids: set[str] = field(default_factory=set)
    wait_guard_targets: tuple[tuple[str, str, str], ...] = ()
    wait_guard_attempt: int = 0
    wait_guard_blocked: bool = False
    tool_result_events: int = 0
    pending_tool_results: int = 0
    seq: int = 0
    state: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False
    end_sent: bool = False
    assistant_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls_seen: set[str] = field(default_factory=set)
    tool_names: dict[str, str] = field(default_factory=dict)
    tool_results_seen: set[str] = field(default_factory=set)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    final_result: dict[str, Any] = field(default_factory=dict)
    trigger_kind: str = ""
    trigger_facts: dict[str, Any] = field(default_factory=dict)
    proactive_id: str = ""



@dataclass
class AgentChannelState:
    active: bool = False
    active_turn_id: str = ""
    queue_depth: int = 0
    last_final_text: str = ""
    last_final_turn_id: str = ""
    last_completion_seq: int = 0
    last_completion_phase: str = ""
    last_completion_sent_at: int = 0
    last_status_seq: int = 0
    last_status_sent_at: int = 0
    last_turn_event_sent_at: int = 0



@dataclass
class WaitOtherResult:
    member_id: str
    name: str
    status: str
    text: str = ""
    turn_id: str = ""
