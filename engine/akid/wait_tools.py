"""wait_other, wait_user, the wait guard, and the execution lock."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from engine.execution_policy import (
    LOCK_ACQUIRE_TOOL,
    LOCK_BUSY_ERROR,
    LOCK_NOT_AUTHORIZED_ERROR,
)
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    normalize_member_kind,
)
from .constants import (
    CHANNEL_MEMBER_TIMEOUT,
    LOGGER,
    WAIT_GUARD_ORIGIN,
    WAIT_OTHER_MAX_REPLY_CHARS,
    WAIT_OTHER_MAX_TOTAL_CHARS,
    WAIT_OTHER_POLL_INTERVAL,
    WAIT_OTHER_START_GRACE,
    WAIT_USER_MAX_REPLY_CHARS,
    WAIT_USER_MAX_TIMEOUT_S,
    WAIT_USER_MAX_TOTAL_CHARS,
    WAIT_USER_MIN_TIMEOUT_S,
)
from .history import _format_channel_message_time
from .types import (
    Turn,
    WaitOtherResult,
)

from .access import pkg

class WaitToolsMixin:

    def _wait_targets_for_turn(self, turn: Turn) -> list[tuple[str, str, str]]:
        if turn.wait_guard_targets:
            return list(turn.wait_guard_targets)
        if turn.origin != "mqtt" or self._solo_session is not None:
            return []
        targets: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for label in self.config.channel_wait_for:
            resolved = self._resolve_label_member_id(label) or self._history_member_id(
                label, label
            )
            if not resolved:
                targets.append(("", label, label))
                continue
            if resolved == self._channel_self_key() or resolved in seen:
                continue
            member = self._channel_members.get(resolved)
            if member is not None and normalize_member_kind(member.get("kind")) != MEMBER_KIND_AGENT:
                continue
            if turn.channel_participants and resolved not in turn.channel_participants:
                continue
            seen.add(resolved)
            display = self._member_display_name(resolved, label) or label
            targets.append((resolved, display, label))
        return targets


    def _unfinished_wait_targets(
        self, turn: Turn
    ) -> list[tuple[str, str, str]]:
        unfinished: list[tuple[str, str, str]] = []
        for member_id, name, label in self._wait_targets_for_turn(turn):
            if not member_id:
                continue
            state = self._agent_channel_state(member_id)
            if state.active or state.queue_depth > 0:
                unfinished.append((member_id, name, label))
        return unfinished


    @staticmethod
    def _wait_guard_notice(targets: list[tuple[str, str, str]]) -> str:
        names = ", ".join(
            dict.fromkeys(name or label for _member_id, name, label in targets)
        )
        return (
            "[akid wait guard]\n"
            "这是系统提示，不是用户消息：你刚才没有等待应该等待的目标，"
            f"这些目标是:{names}。请先调用 `wait_other` 等待这些目标完成，"
            "再给出最终回复。不要解释或提及这条系统提示。"
        )


    def _enqueue_wait_guard_turn(
        self, turn: Turn, targets: list[tuple[str, str, str]]
    ) -> None:
        guard_turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.channel_conn,
            message=self._wait_guard_notice(targets),
            include_reasoning=False,
            client_message_id=turn.client_message_id,
            origin=WAIT_GUARD_ORIGIN,
            origin_sender_id=turn.origin_sender_id,
            origin_is_agent=False,
            lock_authorized=False,
            lock_authorization="wait-guard",
            nighttalk=turn.nighttalk,
            channel_participants=turn.channel_participants,
            channel_sender=turn.channel_sender,
            wait_guard_targets=tuple(targets),
            wait_guard_attempt=turn.wait_guard_attempt + 1,
        )
        if len(self.queue) >= self.config.queue_max:
            LOGGER.warning(
                "wait guard queue exceeds max; enqueueing correction turn=%s",
                guard_turn.turn_id,
            )
        guard_turn.state = "queued"
        self.queue.append(guard_turn)
        self._schedule_status_refresh()
        LOGGER.info(
            "wait guard blocked final turn=%s correction=%s targets=%s attempt=%s",
            turn.turn_id,
            guard_turn.turn_id,
            ",".join(item[1] or item[2] for item in targets),
            guard_turn.wait_guard_attempt,
        )


    @staticmethod
    def _wait_result_from_state(
        member_id: str,
        name: str,
        state: AgentChannelState,
        *,
        fallback_status: str = "completed",
    ) -> WaitOtherResult:
        status = fallback_status
        if state.last_completion_phase == "offline":
            status = "offline"
        elif state.last_completion_phase == "no_reply":
            status = "no_reply"
        elif state.last_completion_phase == "ended" and state.last_final_text:
            status = "completed"
        return WaitOtherResult(
            member_id=member_id,
            name=name,
            status=status,
            text=state.last_final_text,
            turn_id=state.last_final_turn_id,
        )


    async def _wait_for_agent(
        self, member_id: str, name: str, turn: Turn
    ) -> WaitOtherResult:
        if not member_id:
            return WaitOtherResult(member_id="", name=name, status="unknown")
        state = self._agent_channel_state(member_id)
        was_discovered = member_id in self._channel_members
        started_at = asyncio.get_running_loop().time()
        while member_id not in self._channel_members and not state.active:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if asyncio.get_running_loop().time() - started_at >= pkg().WAIT_OTHER_START_GRACE:
                return WaitOtherResult(member_id=member_id, name=name, status="offline")
            await asyncio.sleep(pkg().WAIT_OTHER_POLL_INTERVAL)
            state = self._agent_channel_state(member_id)
            was_discovered = was_discovered or member_id in self._channel_members
        was_discovered = was_discovered or member_id in self._channel_members

        baseline = state.last_completion_seq

        while not state.active and state.queue_depth <= 0:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if member_id not in self._channel_members:
                return WaitOtherResult(member_id=member_id, name=name, status="offline")
            state = self._agent_channel_state(member_id)
            if state.last_completion_seq > baseline:
                return self._wait_result_from_state(member_id, name, state)
            if asyncio.get_running_loop().time() - started_at >= pkg().WAIT_OTHER_START_GRACE:
                return self._wait_result_from_state(
                    member_id, name, state, fallback_status="idle"
                )
            await asyncio.sleep(pkg().WAIT_OTHER_POLL_INTERVAL)

        while True:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                return WaitOtherResult(member_id=member_id, name=name, status="cancelled")
            if member_id not in self._channel_members:
                if was_discovered:
                    return WaitOtherResult(
                        member_id=member_id, name=name, status="offline"
                    )
                last_event = state.last_turn_event_sent_at
                stale_after_ms = int(CHANNEL_MEMBER_TIMEOUT * 1000)
                if (
                    not state.active
                    or last_event <= 0
                    or int(time.time() * 1000) - last_event > stale_after_ms
                ):
                    return WaitOtherResult(
                        member_id=member_id, name=name, status="offline"
                    )
            state = self._agent_channel_state(member_id)
            was_discovered = was_discovered or member_id in self._channel_members
            if state.last_completion_seq > baseline:
                return self._wait_result_from_state(member_id, name, state)
            if not state.active and state.queue_depth <= 0:
                return self._wait_result_from_state(member_id, name, state)
            await asyncio.sleep(pkg().WAIT_OTHER_POLL_INTERVAL)


    @staticmethod
    def _format_wait_other_results(results: list[WaitOtherResult]) -> str:
        lines = ["WAIT_OTHER_RESULT"]
        total = 0
        for result in results:
            lines.append(f"[{result.name}] status={result.status}")
            if result.turn_id:
                lines.append(f"turn_id={result.turn_id}")
            if result.text:
                remaining = max(0, WAIT_OTHER_MAX_TOTAL_CHARS - total)
                limit = min(WAIT_OTHER_MAX_REPLY_CHARS, remaining)
                text = result.text[:limit]
                if len(text) < len(result.text):
                    text += "\n...[truncated by akid]"
                lines.append(text)
                total += len(text)
            lines.append("---")
        lines.append(
            "Use these earlier replies to continue the discussion; do not repeat them verbatim."
        )
        return "\n".join(lines)


    @staticmethod
    def _pending_wait_user_messages(turn: Turn) -> list[dict[str, Any]]:
        sender_id = turn.origin_sender_id
        messages: list[dict[str, Any]] = []
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)):
                continue
            if str(item.get("sender_id") or "") != sender_id:
                continue
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq <= 0 or seq not in turn.wait_user_eligible_seqs:
                continue
            if seq > 0 and seq in turn.waited_user_seqs:
                continue
            messages.append(item)
        messages.sort(
            key=lambda item: (
                int(item.get("sent_at") or 0),
                int(item.get("seq") or 0),
            )
        )
        return messages


    @staticmethod
    def _consume_wait_user_messages(
        turn: Turn, messages: list[dict[str, Any]]
    ) -> None:
        from engine.akid.server import GatewayServer
        for item in messages:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                turn.waited_user_seqs.add(seq)
                GatewayServer._remove_turn_overlay_file(turn, seq)
            item["waited_by_user"] = True


    def _format_wait_user_results(
        self, turn: Turn, messages: list[dict[str, Any]]
    ) -> str:
        lines = [f"WAIT_USER_RESULT status=messages count={len(messages)}"]
        total = 0
        for item in messages:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            sent_at = int(item.get("sent_at") or 0)
            sender = str(item.get("sender") or turn.channel_sender or "user")
            lines.append(
                f"[seq={seq} at={_format_channel_message_time(sent_at)} "
                f"sender={sender}]"
            )
            text = str(item.get("text") or "").strip()
            remaining = max(0, WAIT_USER_MAX_TOTAL_CHARS - total)
            limit = min(WAIT_USER_MAX_REPLY_CHARS, remaining)
            if text:
                rendered = text[:limit]
                if len(rendered) < len(text):
                    rendered += "\n...[truncated by akid]"
                lines.append(rendered)
                total += len(rendered)
            attachments = item.get("attachments") or ()
            if attachments:
                payload = [
                    {
                        "name": attachment.name,
                        "path": attachment.path,
                        **({"missing": True} if attachment.missing else {}),
                    }
                    for attachment in attachments
                ]
                lines.append(
                    "attachments="
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
            lines.append("---")
        lines.append(
            "Treat these messages as the user's continuation. Do not mention "
            "the wait or this tool in your reply."
        )
        return "\n".join(lines)


    async def _handle_wait_user_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if turn.origin != "mqtt" or turn.origin_is_agent:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_UNAVAILABLE: only human-triggered channel turns may "
                "wait for the user. Do not retry.",
            )
            return
        if not turn.origin_sender_id:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_UNAVAILABLE: the current turn has no human sender. "
                "Do not retry.",
            )
            return
        raw_timeout = tool_input.get("timeout_s")
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int):
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_INVALID_TIMEOUT: timeout_s must be an integer from "
                f"{WAIT_USER_MIN_TIMEOUT_S} to {WAIT_USER_MAX_TIMEOUT_S}.",
            )
            return
        timeout_s = int(raw_timeout)
        if not WAIT_USER_MIN_TIMEOUT_S <= timeout_s <= WAIT_USER_MAX_TIMEOUT_S:
            await self._external_tool_failure(
                turn,
                tool_call_id,
                "WAIT_USER_INVALID_TIMEOUT: timeout_s must be an integer from "
                f"{WAIT_USER_MIN_TIMEOUT_S} to {WAIT_USER_MAX_TIMEOUT_S}.",
            )
            return

        if not turn.wait_user_used:
            turn.wait_user_used = True
        turn.wait_user_call_active = True
        try:
            await self._set_wait_user_active(True)
            await self._wait_for_user_messages(turn, tool_call_id, timeout_s)
        finally:
            turn.wait_user_call_active = False
            with contextlib.suppress(Exception):
                await self._set_wait_user_active(False)


    async def _wait_for_user_messages(
        self, turn: Turn, tool_call_id: str, timeout_s: int
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if turn.end_sent or turn.cancelled or self._shutting_down:
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    "WAIT_USER_CANCELLED: the turn ended before another user "
                    "message arrived. Do not mention this tool or the wait.",
                )
                return
            pending = self._pending_wait_user_messages(turn)
            if pending:
                self._consume_wait_user_messages(turn, pending)
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    self._format_wait_user_results(turn, pending),
                )
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    f"WAIT_USER_TIMEOUT: no new message from "
                    f"{turn.channel_sender or 'the user'} arrived within "
                    f"{timeout_s}s. Continue with the current request. Do not "
                    "mention the wait or this tool.",
                )
                return
            turn.user_message_event.clear()
            pending = self._pending_wait_user_messages(turn)
            if pending:
                self._consume_wait_user_messages(turn, pending)
                await self._external_tool_success(
                    turn,
                    tool_call_id,
                    self._format_wait_user_results(turn, pending),
                )
                return
            try:
                await asyncio.wait_for(
                    turn.user_message_event.wait(), timeout=remaining
                )
            except asyncio.TimeoutError:
                continue


    async def _handle_wait_other_tool(
        self, turn: Turn, tool_call_id: str
    ) -> None:
        targets = self._wait_targets_for_turn(turn)
        if not targets:
            await self._send_external_result(
                tool_call_id,
                "WAIT_OTHER_SKIPPED: no earlier agents are configured for this turn.",
                False,
            )
            return
        labels = tuple(name or label for _member_id, name, label in targets)
        await self._set_wait_other_active(True, labels)
        turn.waited_agent_ids.update(
            member_id for member_id, _name, _label in targets if member_id
        )
        try:
            results = await asyncio.gather(
                *(
                    self._wait_for_agent(member_id, name, turn)
                    for member_id, name, _label in targets
                )
            )
            await self._send_external_result(
                tool_call_id,
                self._format_wait_other_results(list(results)),
                False,
            )
        finally:
            await self._set_wait_other_active(False)


    async def _handle_execution_lock_tool(
        self, turn: Turn, tool_call_id: str, tool_name: str
    ) -> None:
        if tool_name == LOCK_ACQUIRE_TOOL:
            if not turn.lock_authorized:
                await self._send_external_result(
                    tool_call_id, LOCK_NOT_AUTHORIZED_ERROR, True
                )
                return
            if self.execution_lock.held_by(
                profile=self.config.profile, turn_id=turn.turn_id
            ):
                turn.lock_held = True
                await self._send_external_result(
                    tool_call_id,
                    "EXECUTION_LOCK_ACQUIRED: this turn already holds the lock.",
                    False,
                )
                return
            try:
                acquired, holder = self.execution_lock.acquire(
                    profile=self.config.profile,
                    agent_id=self.config.agent_id,
                    turn_id=turn.turn_id,
                )
            except OSError as exc:
                self.execution_lock.last_error = str(exc)
                await self._send_external_result(
                    tool_call_id,
                    f"EXECUTION_LOCK_UNAVAILABLE: {exc}. Do not retry; end this turn.",
                    True,
                )
                return
            if acquired:
                turn.lock_held = True
                await self._send_external_result(
                    tool_call_id,
                    "EXECUTION_LOCK_ACQUIRED: device-wide non-read-only execution "
                    "is now exclusive to this turn.",
                    False,
                )
                return
            held_by = str(holder.get("profile") or "another profile")
            held_turn = str(holder.get("turn_id") or "unknown turn")
            await self._send_external_result(
                tool_call_id,
                f"{LOCK_BUSY_ERROR} holder={held_by} turn={held_turn}",
                True,
            )
            return

        released = self.execution_lock.release(
            profile=self.config.profile, turn_id=turn.turn_id
        )
        turn.lock_held = False
        await self._send_external_result(
            tool_call_id,
            "EXECUTION_LOCK_RELEASED"
            if released
            else "EXECUTION_LOCK_NOT_HELD: this turn did not hold the lock.",
            False,
        )


    async def _external_tool_failure(
        self, turn: Turn, tool_call_id: str, text: str
    ) -> None:
        await self._emit_tool_result_once(
            turn,
            tool_call_id,
            {"tool_call_id": tool_call_id, "status": "error", "tool_return": text},
        )
        await self._send_external_result(tool_call_id, text, True)


    async def _external_tool_success(
        self, turn: Turn, tool_call_id: str, text: str
    ) -> None:
        await self._emit_tool_result_once(
            turn,
            tool_call_id,
            {"tool_call_id": tool_call_id, "status": "success", "tool_return": text},
        )
        await self._send_external_result(tool_call_id, text, False)
