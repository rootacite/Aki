"""Night-talk schedule, prompt, and read-only gate.

The scheduler lives on the Aki profile because that process owns presence.
This module is pure: it does not read the clock, the roster, or the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Mapping

from engine.aki_config import atomic_write
from engine.channel_prompt import USERNAME_PLACEHOLDER, render_username_template
from engine.execution_policy import (
    LOCK_ACQUIRE_TOOL,
    is_read_only_tool,
)

NIGHT_TALK_INTERVAL_SECONDS = 30 * 60
NIGHT_TALK_MAX_FIRES = 3
NIGHT_TALK_PROBABILITY = 0.5
NIGHT_TALK_VERSION = 1

NIGHT_TALK_PROMPT = (
    "现在是安静的夜晚，${{USERNAME}}非活跃，你可以回顾一下今天，"
    "和你想与之讨论的其他成员聊一聊今天的事，或者和其他成员讨论一下你和"
    "${{USERNAME}}之间发生的事，如果没有什么想聊的，也可以保持沉默"
)

NIGHTTALK_LOCK_DENIED = (
    "NIGHTTALK_LOCK_DENIED: night talk cannot acquire the execution lock. "
    "Do not retry; end this turn or reply with [NO_REPLY]."
)
NIGHTTALK_READONLY = (
    "NIGHTTALK_READONLY: night talk may only call read-only tools. "
    "Do not retry; end this turn or reply with [NO_REPLY]."
)
NIGHTTALK_CAP_ERROR = "night talk already fired 3 times tonight"


@dataclass
class NightTalkState:
    version: int = NIGHT_TALK_VERSION
    cycle: str = ""
    fires: int = 0
    next_roll_at: float | None = None
    last_target: str = ""
    last_fire_at: str = ""


@dataclass(frozen=True)
class NightTalkStep:
    state: NightTalkState
    action: str


def night_cycle_key(
    local: datetime, start: dt_time, end: dt_time
) -> str | None:
    """Return the night id when ``local`` is inside the window.

    A window that does not cross midnight uses the local date. A window that
    does cross midnight uses the date on which that window started, so the
    3-fire cap does not reset at 00:00.
    """
    current = local.time()
    if start <= end:
        if start <= current < end:
            return local.date().isoformat()
        return None
    if current >= start:
        return local.date().isoformat()
    if current < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return None


def render_nighttalk_prompt(username: str) -> str:
    rendered = render_username_template(NIGHT_TALK_PROMPT, username)
    if USERNAME_PLACEHOLDER in rendered:
        raise ValueError("night talk prompt still contains a username placeholder")
    return rendered


def render_nighttalk_overlay() -> str:
    return (
        '<night-talk version="1">\n'
        "这是夜话。本轮只能调用只读工具，任何情况下都不能获取执行锁，"
        "免锁工具和专属写入也不能调用。\n"
        "若没有想说的，直接输出 [NO_REPLY]。\n"
        "</night-talk>"
    )


def metadata_is_nighttalk(metadata: Mapping[str, Any] | None) -> bool:
    if not metadata:
        return False
    value = str(metadata.get("nighttalk") or "").strip().casefold()
    return value in {"1", "true", "yes"}


def nighttalk_tool_decision(tool_name: str) -> str | None:
    """Return an error for a tool a night-talk turn must not call."""
    if tool_name == LOCK_ACQUIRE_TOOL:
        return NIGHTTALK_LOCK_DENIED
    if is_read_only_tool(tool_name):
        return None
    return NIGHTTALK_READONLY


def load_nighttalk_state(path: Path) -> NightTalkState:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NightTalkState()
    if not isinstance(payload, dict):
        return NightTalkState()
    fires = payload.get("fires")
    next_roll = payload.get("next_roll_at")
    cycle = payload.get("cycle")
    return NightTalkState(
        version=NIGHT_TALK_VERSION,
        cycle=cycle if isinstance(cycle, str) else "",
        fires=(
            fires
            if isinstance(fires, int) and not isinstance(fires, bool) and fires >= 0
            else 0
        ),
        next_roll_at=(
            float(next_roll)
            if isinstance(next_roll, (int, float)) and not isinstance(next_roll, bool)
            else None
        ),
        last_target=(
            payload.get("last_target")
            if isinstance(payload.get("last_target"), str)
            else ""
        ),
        last_fire_at=(
            payload.get("last_fire_at")
            if isinstance(payload.get("last_fire_at"), str)
            else ""
        ),
    )


def save_nighttalk_state(path: Path, state: NightTalkState) -> None:
    payload = {
        "version": NIGHT_TALK_VERSION,
        "cycle": state.cycle,
        "fires": state.fires,
        "next_roll_at": state.next_roll_at,
        "last_target": state.last_target,
        "last_fire_at": state.last_fire_at,
    }
    atomic_write(
        Path(path),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def fires_for_cycle(state: NightTalkState, cycle: str | None) -> int:
    if not cycle or state.cycle != cycle:
        return 0
    return state.fires


def manual_nighttalk_block(state: NightTalkState, cycle: str | None) -> str:
    if cycle and fires_for_cycle(state, cycle) >= NIGHT_TALK_MAX_FIRES:
        return NIGHTTALK_CAP_ERROR
    return ""


def note_nighttalk_delivery(
    state: NightTalkState,
    *,
    cycle: str | None,
    target: str,
    fired_at: str,
) -> NightTalkState:
    """Count a successful activation. Outside a night cycle, do not spend a fire."""
    updated = replace(state)
    if cycle:
        if updated.cycle != cycle:
            updated.cycle = cycle
            updated.fires = 0
        updated.fires += 1
    updated.last_target = target
    updated.last_fire_at = fired_at
    return updated


def advance_nighttalk(
    state: NightTalkState,
    *,
    now: float,
    eligible: bool,
    cycle: str | None,
    roll: float,
) -> NightTalkStep:
    """Advance one scheduler tick.

    ``roll`` is in ``[0, 1)``. A ``fire`` action does not increment ``fires``;
    the caller counts the night only after a member actually accepts the turn.
    A due timer rolls once, even if several intervals were missed.
    """
    updated = replace(state)
    if not eligible or not cycle:
        updated.next_roll_at = None
        return NightTalkStep(updated, "idle")
    if updated.cycle != cycle:
        updated.cycle = cycle
        updated.fires = 0
        updated.next_roll_at = None
    if updated.fires >= NIGHT_TALK_MAX_FIRES:
        updated.next_roll_at = None
        return NightTalkStep(updated, "capped")
    if updated.next_roll_at is None:
        updated.next_roll_at = now + NIGHT_TALK_INTERVAL_SECONDS
        return NightTalkStep(updated, "arm")
    if now < updated.next_roll_at:
        return NightTalkStep(updated, "wait")
    updated.next_roll_at = now + NIGHT_TALK_INTERVAL_SECONDS
    if roll < NIGHT_TALK_PROBABILITY:
        return NightTalkStep(updated, "fire")
    return NightTalkStep(updated, "miss")
