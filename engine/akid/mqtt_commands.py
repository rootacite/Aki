"""Channel commands, cancellation, and outbound turn events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from engine.protocol import (
    FrameError,
    make_event,
)
from .constants import (
    HIDDEN_TOOL_NAMES,
    LOGGER,
)
from .history import (
    _format_channel_message_time,
    _parse_tool_arguments,
)
from .paths import now_iso
from .types import Turn

class MqttCommandsMixin:

    async def _handle_channel_command(self, text: str, sender_id: str = "") -> bool:
        tokens = text.split()
        if not tokens:
            return False
        head = tokens[0].lower()
        decision: bool | None = None
        approval_id: str | None = None
        if head in ("akid-approve", "akid-allow"):
            decision = True
            approval_id = tokens[1] if len(tokens) > 1 else None
        elif head in ("akid-deny", "akid-reject"):
            decision = False
            approval_id = tokens[1] if len(tokens) > 1 else None
        elif head == "akid" and len(tokens) > 1:
            verb = tokens[1].lower()
            if verb in ("approve", "allow"):
                decision = True
            elif verb in ("deny", "reject"):
                decision = False
            if decision is not None:
                approval_id = tokens[2] if len(tokens) > 2 else None
        if decision is None:
            return False

        approval: Approval | None = None
        if approval_id:
            approval = self.approvals.get(approval_id)
        else:
            approval = next(iter(self.approvals.values()), None)
        if approval is None:
            LOGGER.info(
                "channel approval command ignored: no matching local approval id=%s",
                approval_id or "(oldest)",
            )
            return True
        if approval.turn.origin != "mqtt":
            self._schedule_channel_notice(
                "akid: that approval belongs to a local client; use aki.approval.respond"
            )
            return True
        if not self._approval_may_be_resolved_by(approval, sender_id):
            LOGGER.info(
                "channel approval rejected id=%s sender_id=%s origin_sender_id=%s "
                "origin_is_agent=%s",
                approval.approval_id,
                sender_id or "unknown",
                approval.turn.origin_sender_id or "unknown",
                approval.turn.origin_is_agent,
            )
            self._schedule_channel_notice(
                "akid: only the user may resolve an approval triggered by another agent"
                if approval.turn.origin_is_agent
                else "akid: only the message sender may resolve this approval"
            )
            return True
        if approval.future.done():
            self._schedule_channel_notice("akid: that approval was already resolved")
            return True

        reason = "approved via MQTT channel" if decision else "denied via MQTT channel"
        approval.future.set_result((decision, reason))
        verb = "approved" if decision else "denied"
        LOGGER.info(
            "channel approval decision id=%s decision=%s tool=%s",
            approval.approval_id,
            verb,
            approval.tool_name,
        )
        return True


    def _wait_labels_for_participants(
        self, participants: frozenset[str]
    ) -> list[str]:
        # Aki is the first responder in the configured order and never waits
        # for another member; keep the tool and turn prompt out of her profile.
        if self.config.profile == "aki":
            return []
        labels: list[str] = []
        for label in self.config.channel_wait_for:
            resolved = self._resolve_label_member_id(label) or self._history_member_id(
                label, label
            )
            if resolved and participants and resolved not in participants:
                continue
            labels.append(label)
        return labels


    def _channel_prompt(
        self,
        sender: str,
        sender_id: str,
        text: str,
        *,
        from_agent: bool = False,
        seq: int | None = None,
        sent_at: int = 0,
        attachments: tuple[ChannelImageAttachment, ...] = (),
    ) -> str:
        member_id = self._history_member_id(sender, sender_id) or sender_id
        display_sender = self._member_display_name(member_id, sender) or sender
        identity = (
            f"{display_sender} ({member_id})" if member_id else display_sender
        )
        lines = [
            "[akid MQTT channel message]",
            f"message-time: {_format_channel_message_time(sent_at)}",
        ]
        if seq is not None:
            lines.append(f"seq: {seq}")
        lines.append(f"sender: {identity}")
        if member_id:
            lines.append(f"sender_id: {member_id}")
        lines.append(f"source: {'agent' if from_agent else 'user'}")
        lines.append("phase: final")
        if attachments:
            payload = [
                {
                    "name": attachment.name,
                    "path": attachment.path,
                    **(
                        {"missing": True}
                        if attachment.missing or not attachment.path
                        else {}
                    ),
                }
                for attachment in sorted(attachments, key=lambda item: item.index)
            ]
            lines.append(
                "attachments: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            lines.append("multimodal: true")
        lines.extend(["", text])
        return "\n".join(lines)


    def _find_turn_by_client_message_id(self, value: str) -> Turn | None:
        for turn in (
            ([self.active] if self.active is not None else [])
            + list(self.queue)
        ):
            if turn.client_message_id == value:
                return turn
        return None


    async def _handle_channel_cancel(
        self,
        sender: str,
        sender_id: str,
        member_id: str,
        metadata: Mapping[str, str],
    ) -> None:
        turn_id = str(metadata.get("turnId") or "").strip()
        client_message_id = str(metadata.get("clientMessageId") or "").strip()
        if not turn_id and not client_message_id:
            self._schedule_channel_notice("akid: cancel needs turnId or clientMessageId")
            return
        turn = self._find_turn(turn_id) if turn_id else None
        if turn is None and client_message_id:
            turn = self._find_turn_by_client_message_id(client_message_id)
        if turn is None:
            self._schedule_channel_notice("akid: cancel target not found")
            return
        initiator = self._history_member_id(sender, sender_id)
        if (
            turn.origin_is_agent
            or not turn.origin_sender_id
            or initiator != turn.origin_sender_id
            or (member_id and member_id != turn.origin_sender_id)
        ):
            self._schedule_channel_notice(
                "akid: only the human who started the turn may cancel it"
            )
            return
        if turn in self.queue:
            self.queue.remove(turn)
            self._schedule_status_refresh()
            turn.state = "cancelled"
            turn.cancelled = True
            turn.end_sent = True
            turn.done_event.set()
            state = self.channel_conn._turn_state(turn.turn_id)
            state.client_message_id = turn.client_message_id or ""
            await self._emit(
                turn,
                "turn_end",
                {"reason": "cancelled", "cancelled": True},
            )
        else:
            await self._cancel_turn(turn)
        LOGGER.info(
            "channel cancel accepted turn=%s client_message_id=%s sender_id=%s",
            turn.turn_id,
            turn.client_message_id or "",
            initiator,
        )


    def _enqueue_channel_turn(
        self,
        sender: str,
        sender_id: str,
        text: str,
        *,
        origin_is_agent: bool = False,
        channel_message_seq: int | None = None,
        participants: frozenset[str] = frozenset(),
        attachments: tuple[ChannelImageAttachment, ...] = (),
        sent_at: int = 0,
        lock_authorized: bool = False,
        lock_authorization: str = "channel-unknown",
        client_message_id: str | None = None,
    ) -> None:
        if len(self.queue) >= self.config.queue_max:
            self._schedule_channel_notice("akid: queue is full; channel message dropped")
            return
        prompt = self._channel_prompt(
            sender,
            sender_id,
            text,
            from_agent=origin_is_agent,
            seq=channel_message_seq,
            sent_at=sent_at,
            attachments=attachments,
        )
        turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.channel_conn,
            message=prompt,
            include_reasoning=False,
            client_message_id=client_message_id,
            origin="mqtt",
            origin_sender_id=sender_id,
            origin_is_agent=origin_is_agent,
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            channel_message_seq=channel_message_seq,
            channel_participants=participants,
            channel_sender=sender,
            channel_text=text,
            channel_attachments=tuple(attachments),
        )
        if (
            self.active is None
            and not self.queue
            and not self._compacting
            and not self._external_tools_pending
            and bool(getattr(self.backend, "ready", False))
        ):
            self.active = turn
            turn.state = "running"
            self._spawn_turn(turn)
            return
        turn.state = "queued"
        self.queue.append(turn)
        self._schedule_status_refresh()


    def _schedule_channel_notice(self, text: str) -> None:
        self._schedule_channel_publish(text, final=False)


    def _schedule_channel_publish(self, text: str, *, final: bool) -> None:
        async def publish() -> None:
            channel = self.channel
            if channel is None:
                return
            try:
                if final:
                    await channel.publish_text(text)
                else:
                    await channel.publish_notice(text)
            except Exception as exc:
                LOGGER.warning("channel publish failed: %s", exc)

        task = asyncio.create_task(publish(), name="channel-publish")
        self._channel_tasks.add(task)
        task.add_done_callback(self._channel_tasks.discard)


    async def _emit(self, turn: Turn, event_type: str, data: Mapping[str, Any] | None = None) -> None:
        turn.seq += 1
        message = make_event(turn.turn_id, turn.seq, event_type, data, now_iso())
        if turn.origin == "mqtt" and event_type in (
            "approval_request",
            "approval_resolved",
        ):
            await self._broadcast_local_event(message)
        if turn.conn.closed:
            return
        try:
            await turn.conn.send(message)
        except FrameError:
            turn.seq += 1
            fallback = make_event(
                turn.turn_id,
                turn.seq,
                "error",
                {
                    "code": "EVENT_TOO_LARGE",
                    "message": f"{event_type} event exceeded the 1 MiB frame limit",
                },
                now_iso(),
            )
            with contextlib.suppress(Exception):
                await turn.conn.send(fallback)
        except (ConnectionResetError, BrokenPipeError):
            turn.conn.closed = True


    async def _broadcast_local_event(self, message: Mapping[str, Any]) -> None:
        """Deliver a channel-turn approval event to every local user client."""
        for conn in list(self.connection_set):
            if conn.closed or not conn.handshaken:
                continue
            try:
                await conn.send(message)
            except FrameError:
                LOGGER.warning(
                    "local approval event exceeded frame limit type=%s",
                    (message.get("params") or {}).get("type"),
                )
            except (ConnectionResetError, BrokenPipeError):
                conn.closed = True


    async def _emit_tool_call_once(
        self, turn: Turn, tool_call_id: str, name: str, arguments: Any
    ) -> None:
        if tool_call_id:
            turn.tool_names[tool_call_id] = name
        if name in HIDDEN_TOOL_NAMES:
            await self._emit(
                turn,
                "assistant_segment",
                {
                    "reason": "hidden_tool",
                    "tool_call_id": tool_call_id,
                    "name": name,
                },
            )
            return
        if tool_call_id in turn.tool_calls_seen:
            return
        turn.tool_calls_seen.add(tool_call_id)
        self.operation_journal.start(tool_call_id, name, arguments)
        await self._emit(
            turn,
            "tool_call",
            {
                "tool_call_id": tool_call_id,
                "name": name,
                "arguments": _parse_tool_arguments(arguments),
            },
        )


    async def _emit_tool_result_once(
        self, turn: Turn, tool_call_id: str, data: Mapping[str, Any]
    ) -> None:
        key = str(tool_call_id or "")
        if key:
            if key in turn.tool_results_seen:
                return
            turn.tool_results_seen.add(key)
        status = str(data.get("status") or "completed")
        self.operation_journal.finish(
            key,
            status=status,
            error=(
                data.get("tool_return") or data.get("stderr") or ""
                if status.lower() in ("error", "failed", "failure")
                else ""
            ),
        )
        if turn.tool_names.get(key) in HIDDEN_TOOL_NAMES:
            return
        await self._emit(turn, "tool_result", data)
