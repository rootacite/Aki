"""Handle one backend message, including tool approval."""

from __future__ import annotations

import asyncio
from engine.letta_process import (
    BackendProtocolError,
    BackendUnavailable,
)
from engine.execution_policy import (
    LOCK_RELEASE_TOOL,
    LOCK_REQUIRED_ERROR,
    requires_execution_lock,
)
from engine.nighttalk import nighttalk_tool_decision
from engine.tool_ownership import (
    authorize_exclusive_tool,
    ownership_denied_message,
)
from .constants import (
    EXTERNAL_TOOL_NAMES,
    LOGGER,
    MCP_TOOL_PREFIX,
)
from .history import _content_text
from .paths import tool_is_safe
from .types import Approval

class BackendTurnMixin:

    async def _on_backend_message(self, message: dict[str, Any]) -> None:
        if message.get("type") == "stream_event" and isinstance(message.get("event"), dict):
            message = message["event"]
        message_type = message.get("type")

        if message_type == "queue_item_enqueued":
            self._track_backend_queue_item(message)
            return
        if message_type == "queue_batch_dequeued":
            self._track_backend_queue_batch(message)
            return
        if message_type == "queue_cleared":
            self._reset_backend_queue_state()
            return

        if message_type == "control_request":
            request = message.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                task = asyncio.create_task(self._handle_tool_request(message))
                self._approval_tasks.add(task)
                task.add_done_callback(self._approval_tasks.discard)
            elif request.get("subtype") == "execute_external_tool":
                task = asyncio.create_task(self._handle_external_tool(message))
                self._external_tool_tasks.add(task)
                task.add_done_callback(self._external_tool_tasks.discard)
            return

        turn = self.active
        if message_type == "result":
            batch = self._backend_current_batch
            self._backend_current_batch = None
            if batch is not None and not bool(batch.get("has_user")):
                LOGGER.info(
                    "ignoring backend result for internal batch id=%s items=%s",
                    batch.get("batch_id"),
                    ",".join(
                        str(item.get("kind") or item.get("source") or "?")
                        for item in batch.get("items", [])
                    ),
                )
                return
            if turn is None or turn.end_sent:
                return
            turn.final_result = dict(message)
            reason = "cancelled" if message.get("stop_reason") == "cancelled" else "completed"
            if message.get("subtype") == "error" and reason != "cancelled":
                reason = "error"
            # Never await end-of-turn bridge cleanup from the backend read
            # loop: end_aki_turn needs that same loop to receive its response.
            task = asyncio.create_task(
                self._finish_turn(turn, reason),
                name=f"finish-turn:{turn.turn_id}",
            )
            self._turn_tasks.add(task)
            task.add_done_callback(self._turn_tasks.discard)
            return

        if turn is None or turn.end_sent:
            return
        if self._backend_batch_is_internal():
            # Task notifications and cron prompts run as backend-internal
            # turns. Their stream output must not be attributed to the user
            # turn waiting behind them in the queue.
            return
        inner_type = message.get("message_type") or message_type

        if inner_type == "assistant_message":
            turn.pending_tool_results = 0
            text = _content_text(message.get("content"))
            if text:
                turn.assistant_parts.append(text)
                await self._emit(turn, "assistant_delta", {"text": text})
            return
        if inner_type == "reasoning_message":
            turn.pending_tool_results = 0
            reasoning = message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                turn.reasoning_parts.append(reasoning)
                if turn.include_reasoning:
                    await self._emit(
                        turn, "assistant_delta", {"text": reasoning, "reasoning": True}
                    )
            return
        if inner_type in ("tool_call_message", "approval_request_message"):
            turn.pending_tool_results = 0
            call = message.get("tool_call") or {}
            if isinstance(call, dict):
                tool_call_id = str(call.get("tool_call_id") or call.get("id") or "")
                name = str(call.get("name") or "unknown")
                await self._emit_tool_call_once(
                    turn, tool_call_id, name, call.get("arguments")
                )
            return
        if inner_type == "tool_return_message":
            turn.tool_result_events += 1
            turn.pending_tool_results += 1
            await self._emit_tool_result_once(
                turn,
                str(message.get("tool_call_id") or ""),
                {
                    "tool_call_id": message.get("tool_call_id"),
                    "status": message.get("status"),
                    "tool_return": message.get("tool_return"),
                    "stdout": message.get("stdout"),
                    "stderr": message.get("stderr"),
                },
            )
            return
        if inner_type == "usage_statistics":
            usage = {
                key: message.get(key)
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "cached_input_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "context_tokens",
                )
                if message.get(key) is not None
            }
            self.last_usage = usage
            await self._emit(turn, "usage", usage)
            self._schedule_status_refresh()
            return
        if inner_type == "event_message" and message.get("event_type") == "compaction":
            await self._emit(
                turn, "compaction", {"trigger": (message.get("event_data") or {}).get("trigger")}
            )
            return
        if inner_type == "summary_message":
            await self._emit(
                turn,
                "compaction",
                {
                    "summary": message.get("summary"),
                    "stats": message.get("compaction_stats"),
                },
            )
            return
        if message_type == "error" or inner_type == "error_message":
            await self._emit(
                turn,
                "error",
                {
                    "message": message.get("message") or message.get("error"),
                    "stop_reason": message.get("stop_reason"),
                },
            )
            return


    async def _handle_tool_request(self, message: dict[str, Any]) -> None:
        turn = self.active
        request_id = str(message.get("request_id") or "")
        request = message.get("request") or {}
        if turn is None or turn.end_sent or not request_id:
            if request_id:
                await self._safe_deny(request_id, "no active turn")
            return
        tool_name = str(request.get("tool_name") or "unknown")
        tool_call_id = str(request.get("tool_call_id") or request_id)
        tool_input = request.get("input")

        if self._backend_batch_is_internal():
            if tool_is_safe(tool_name) or (
                tool_name == "Bash"
                and self.config.sandbox_enabled
                and self._sandbox_blocked_reason is None
            ):
                await self._safe_allow(request_id)
            else:
                await self._safe_deny(request_id, "internal backend turn")
            return

        if tool_name == LOCK_RELEASE_TOOL:
            await self._safe_deny(
                request_id,
                "EXECUTION_LOCK_RELEASE_UNAVAILABLE: manual lock release is not "
                "available. The gateway releases the lock automatically when the "
                "turn ends; do not retry.",
            )
            return

        if turn.nighttalk:
            decision = nighttalk_tool_decision(tool_name)
            if decision:
                await self._emit_tool_call_once(
                    turn, tool_call_id, tool_name, tool_input
                )
                await self._safe_deny(request_id, decision)
                return

        allowed, _owner = authorize_exclusive_tool(self.config.profile, tool_name)
        if not allowed:
            await self._safe_deny(request_id, ownership_denied_message(tool_name))
            return

        await self._emit_tool_call_once(turn, tool_call_id, tool_name, tool_input)

        if (
            requires_execution_lock(tool_name)
            and not turn.lock_held
            and tool_name not in EXTERNAL_TOOL_NAMES
            and not tool_name.startswith(MCP_TOOL_PREFIX)
        ):
            await self._safe_deny(request_id, LOCK_REQUIRED_ERROR)
            return

        if (
            tool_is_safe(tool_name)
            or tool_name in EXTERNAL_TOOL_NAMES
            or (
                tool_name == "Bash"
                and self.config.sandbox_enabled
                and self._sandbox_blocked_reason is None
            )
        ):
            await self._safe_allow(request_id)
            return
        allow, reason = await self._request_approval(
            turn,
            approval_id=request_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_input=tool_input,
            diffs=request.get("diffs"),
        )
        try:
            await self.backend.approve_tool(request_id, allow=allow, message=reason)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deliver approval decision tool=%s", tool_name)


    async def _request_approval(
        self,
        turn: Turn,
        *,
        approval_id: str,
        tool_name: str,
        tool_call_id: str,
        tool_input: Any,
        diffs: Any = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        if turn.end_sent:
            return False, "turn ended"
        if turn.conn.closed:
            return False, "initiating client disconnected"
        future: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        approval = Approval(
            approval_id=approval_id,
            turn=turn,
            future=future,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        self.approvals[approval_id] = approval
        self._schedule_status_refresh()
        data: dict[str, Any] = {
            "approval_id": approval_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "input": tool_input,
            "diffs": diffs,
            "timeout_s": self.config.approval_timeout,
        }
        if turn.origin == "mqtt" and not turn.origin_is_agent:
            data["requester_id"] = turn.origin_sender_id
        if extra:
            data.update(extra)
        await self._emit(turn, "approval_request", data)
        self.operation_journal.approval_started()
        LOGGER.info(
            "approval requested id=%s tool=%s turn=%s origin=%s from_agent=%s",
            approval_id,
            tool_name,
            turn.turn_id,
            turn.origin,
            turn.origin_is_agent,
        )
        timed_out = False
        try:
            allow, reason = await asyncio.wait_for(
                future, timeout=self.config.approval_timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            allow, reason = False, f"approval timed out after {self.config.approval_timeout:g}s"
            LOGGER.info("approval timeout turn=%s tool=%s", turn.turn_id, tool_name)
        except asyncio.CancelledError:
            allow, reason = False, "turn cancelled"
        finally:
            self.operation_journal.approval_finished()
            self.approvals.pop(approval_id, None)
            self._schedule_status_refresh()
        decision = "timeout" if timed_out else ("allow" if allow else "deny")
        await self._emit(
            turn,
            "approval_resolved",
            {
                "approval_id": approval_id,
                "decision": decision,
                "message": reason,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            },
        )
        return allow, reason


    async def _safe_allow(self, request_id: str) -> None:
        try:
            await self.backend.approve_tool(request_id, allow=True)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to auto-allow tool")


    async def _safe_deny(self, request_id: str, reason: str) -> None:
        try:
            await self.backend.approve_tool(request_id, allow=False, message=reason)
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deny tool")
