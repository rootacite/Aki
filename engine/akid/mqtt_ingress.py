"""Accept channel text and images and open a turn for them."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from engine.nighttalk import metadata_is_nighttalk
from engine.live_context import (
    CHANNEL_WAKE_ALL,
    CHANNEL_WAKE_MENTIONS,
    CHANNEL_WAKE_USER,
    ChannelImageAttachment,
    match_channel_mentions,
)
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    normalize_member_kind,
)
from engine.tokifactor import FileEnvelope, MAX_ATTACHMENT_NAME_CHARS
from .constants import (
    CHANNEL_IMAGE_GROUP_GRACE_S,
    CHANNEL_IMAGE_GROUP_TIMEOUT_S,
    LOGGER,
    MAX_CHANNEL_ATTACHMENTS,
    MAX_CHANNEL_TEXT_CHARS,
)

class MqttIngressMixin:

    # --------------------------------------------------------------- mqtt rx
    def _claim_agent_initiation(self, turn_id: str) -> bool:
        if self._agent_initiation_remaining <= 0:
            LOGGER.info(
                "agent initiation budget exhausted; suppressing turn output turn=%s",
                turn_id,
            )
            return False
        self._agent_initiation_remaining -= 1
        LOGGER.info(
            "agent initiation budget consumed remaining=%s turn=%s",
            self._agent_initiation_remaining,
            turn_id,
        )
        return True


    def _channel_sender_is_agent(
        self,
        sender: str,
        sender_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        key = self._member_key(metadata or {}, sender, sender_id)
        member = self._channel_members.get(key) if key else None
        return (
            member is not None
            and normalize_member_kind(member.get("kind")) == MEMBER_KIND_AGENT
        )


    def _channel_member_kind(self, member_id: str) -> str:
        """Return ``user``, ``agent``, or an empty string when unknown."""
        if not member_id:
            return ""
        if member_id == self._channel_self_key():
            return MEMBER_KIND_AGENT
        if self._identity_registry is not None:
            identity = self._identity_registry.get(member_id)
            if identity is not None:
                return normalize_member_kind(identity.kind)
        member = self._channel_members.get(member_id)
        if member is not None:
            return normalize_member_kind(member.get("kind"))
        return ""


    def _approval_may_be_resolved_by(self, approval: "Approval", sender_id: str) -> bool:
        """Return whether a channel sender is allowed to resolve an approval.

        Approvals triggered by another agent belong to the human user, not to
        the agent whose message happened to wake the executor.  A local client
        connection is handled separately by ``_approval_respond``.
        """
        kind = self._channel_member_kind(sender_id)
        if approval.turn.origin_is_agent:
            return kind == MEMBER_KIND_USER
        if kind == MEMBER_KIND_AGENT:
            return False
        return bool(approval.turn.origin_sender_id) and (
            sender_id == approval.turn.origin_sender_id
        )


    def _channel_agent_views(self) -> list[ChannelMember]:
        return [
            member
            for member in self._channel_member_views()
            if normalize_member_kind(member.kind) == MEMBER_KIND_AGENT
        ]


    def _channel_message_mentions(self, text: str) -> set[str]:
        if self._solo_session is not None:
            return set()
        return match_channel_mentions(text, self._channel_member_views())


    def _channel_lock_authorization(
        self, text: str, *, from_agent: bool
    ) -> tuple[bool, str]:
        if self._solo_session is not None:
            return True, "solo-mode"
        if from_agent:
            return False, "agent-message"
        mentions = self._channel_message_mentions(text)
        if self._channel_self_key() in mentions:
            return True, "user-mentioned"
        if mentions:
            return False, "mentioned-other-members"
        if len(self._channel_agent_views()) <= 1:
            return True, "single-online-member"
        return False, "user-must-name-executor"


    def _channel_busy(self) -> bool:
        return self.active is not None or bool(self.queue)


    def _channel_participants_for_event(
        self,
        *,
        from_agent: bool,
        mentions: set[str],
        wake: str,
        wake_ids: set[str],
    ) -> frozenset[str]:
        if self._solo_session is not None:
            return frozenset({self._channel_self_key()})
        if from_agent:
            if wake == CHANNEL_WAKE_MENTIONS:
                return frozenset(wake_ids)
            return frozenset(member.id for member in self._channel_agent_views() if member.id)
        if mentions:
            return frozenset(mentions)
        return frozenset(member.id for member in self._channel_agent_views() if member.id)


    def _channel_text_action(self, metadata: Mapping[str, str]) -> str:
        """Return user/final/progress actions, or an empty string to ignore."""
        schema = str(metadata.get("schema") or "").strip()
        if not schema:
            return "user"
        if schema != "akid.v1":
            return "user"
        kind = str(metadata.get("kind") or "").strip()
        phase = str(metadata.get("phase") or "").strip()
        if kind == "turn" and phase == "final":
            return "agent-final"
        if kind == "turn" and phase == "progress":
            return "agent-progress"
        if kind in ("message", "text"):
            return "user"
        return ""


    @staticmethod
    def _channel_attachment_count(metadata: Mapping[str, str]) -> int:
        try:
            return max(0, min(int(metadata.get("attachmentCount") or 0), MAX_CHANNEL_ATTACHMENTS))
        except (TypeError, ValueError):
            return 0


    def _channel_attachment_group(self, message_id: str) -> dict[str, Any]:
        group = self._channel_pending_images.get(message_id)
        if group is None:
            group = {
                "message_id": message_id,
                "attachments": {},
                "count": 1,
                "text_event": None,
                "timer": None,
                "created": time.monotonic(),
            }
            self._channel_pending_images[message_id] = group
        return group


    def _cancel_attachment_timer(self, group: dict[str, Any]) -> None:
        timer = group.get("timer")
        if isinstance(timer, asyncio.Task) and timer is not asyncio.current_task():
            timer.cancel()
        group["timer"] = None


    def _schedule_attachment_finalize(
        self, message_id: str, delay: float
    ) -> None:
        group = self._channel_pending_images.get(message_id)
        if group is None:
            return
        self._cancel_attachment_timer(group)

        async def finalize() -> None:
            await asyncio.sleep(delay)
            await self._finalize_attachment_group(message_id)

        timer = asyncio.create_task(finalize(), name=f"channel-images:{message_id[:24]}")
        group["timer"] = timer
        self._channel_attachment_tasks.add(timer)
        timer.add_done_callback(self._channel_attachment_tasks.discard)


    async def _finalize_attachment_group(self, message_id: str) -> None:
        group = self._channel_pending_images.pop(message_id, None)
        if group is None:
            return
        self._cancel_attachment_timer(group)
        attachments = tuple(
            group["attachments"][index]
            for index in sorted(group["attachments"])
            if isinstance(group["attachments"].get(index), ChannelImageAttachment)
        )
        event = group.get("text_event")
        if isinstance(event, dict):
            await self._on_channel_text(
                str(event["sender"]),
                str(event["sender_id"]),
                str(event["text"]),
                event.get("metadata") or {},
                str(event.get("message_id") or ""),
                int(event.get("sent_at") or 0),
                bool(event.get("replayed")),
                _attachments=attachments,
            )
            return
        if not attachments:
            return
        sender = str(group.get("sender") or "Unknown")
        sender_id = str(group.get("sender_id") or "")
        member_id = self._history_member_id(sender, sender_id) or sender_id
        display_sender = self._member_display_name(member_id, sender) or sender
        from_agent = self._channel_sender_is_agent(sender, sender_id, {})
        self._record_channel_message(
            sender=display_sender,
            sender_id=member_id or sender_id,
            text="",
            from_agent=from_agent,
            sent_at=int(group.get("sent_at") or 0),
            phase="final",
            arrived_while_busy=self._channel_busy(),
            attachments=attachments,
        )
        await self._refresh_channel_transcript()
        LOGGER.info(
            "channel image-only message recorded message_id=%s images=%s",
            message_id,
            len(attachments),
        )


    async def _on_channel_image(self, image: ImageEnvelope, replayed: bool = False) -> None:
        message_id = image.message_id or image.transfer_id
        if not message_id:
            return
        group = self._channel_attachment_group(message_id)
        group["count"] = max(int(group.get("count") or 1), image.attachment_count)
        group["sender"] = image.sender
        group["sender_id"] = image.sender_id
        group["sent_at"] = image.sent_at
        index = max(0, min(image.attachment_index, MAX_CHANNEL_ATTACHMENTS - 1))
        name = (image.name or f"图片{index + 1}")[:MAX_ATTACHMENT_NAME_CHARS]
        attachment = self._store_channel_image(image, name=name, index=index + 1)
        if attachment is None:
            return
        group["attachments"][index] = attachment
        group["expect_text"] = bool(group.get("expect_text")) or image.expect_text
        if len(group["attachments"]) >= int(group["count"]):
            if group.get("text_event") is not None:
                self._schedule_attachment_finalize(message_id, 0)
                return
            self._schedule_attachment_finalize(
                message_id,
                CHANNEL_IMAGE_GROUP_TIMEOUT_S
                if group.get("expect_text")
                else CHANNEL_IMAGE_GROUP_GRACE_S,
            )
        else:
            self._schedule_attachment_finalize(message_id, CHANNEL_IMAGE_GROUP_TIMEOUT_S)


    async def _on_channel_file(self, file: FileEnvelope, replayed: bool = False) -> None:
        transfer_id = file.transfer_id or ""
        if transfer_id and any(
            entry.message_id == transfer_id for entry in self._channel_history
        ):
            with contextlib.suppress(OSError):
                Path(file.staged_path).unlink()
            return
        stored = await asyncio.to_thread(self._store_channel_file, file)
        if stored is None:
            with contextlib.suppress(OSError):
                Path(file.staged_path).unlink()
            return
        record_path = await asyncio.to_thread(
            self._channel_file_record_path, file, stored
        )
        sender = file.sender or "Unknown"
        sender_id = file.sender_id or ""
        member_id = self._history_member_id(sender, sender_id) or sender_id
        display_sender = self._member_display_name(member_id, sender) or sender
        from_agent = self._channel_sender_is_agent(sender, sender_id, {})
        busy = self._channel_busy()
        entry = self._record_channel_message(
            sender=display_sender,
            sender_id=member_id or sender_id,
            text=record_path,
            from_agent=from_agent,
            message_id=transfer_id,
            sent_at=file.sent_at,
            phase="final",
            arrived_while_busy=busy,
        )
        LOGGER.info(
            "channel file message recorded without turn seq=%s transfer_id=%s "
            "sender_id=%s path=%s",
            entry.seq,
            transfer_id or "unknown",
            member_id or sender_id or "unknown",
            record_path,
        )
        if busy:
            await self._refresh_channel_transcript()


    def _take_complete_attachment_group(
        self, message_id: str, metadata: Mapping[str, str]
    ) -> tuple[ChannelImageAttachment, ...] | None:
        group = self._channel_pending_images.get(message_id)
        expected = self._channel_attachment_count(metadata)
        if group is None:
            return () if expected == 0 else None
        attachments = group.get("attachments") or {}
        required = max(expected, int(group.get("count") or 1))
        if len(attachments) < required:
            return None
        self._cancel_attachment_timer(group)
        self._channel_pending_images.pop(message_id, None)
        return tuple(
            attachments[index]
            for index in sorted(attachments)
            if isinstance(attachments.get(index), ChannelImageAttachment)
        )


    async def _on_channel_text(
        self,
        sender: str,
        sender_id: str,
        text: str,
        metadata: Mapping[str, str] | None = None,
        message_id: str = "",
        sent_at: int = 0,
        replayed: bool = False,
        *,
        _attachments: tuple[ChannelImageAttachment, ...] | None = None,
    ) -> None:
        text = text.strip()
        if len(text) > MAX_CHANNEL_TEXT_CHARS:
            text = text[:MAX_CHANNEL_TEXT_CHARS] + "\n...[truncated by akid]"
        event_metadata = dict(metadata or {})
        client_message_id = (
            str(event_metadata.get("clientMessageId") or "").strip() or None
        )
        member_id = self._member_key(event_metadata, sender, sender_id)
        if (
            event_metadata.get("schema") == "akid.v1"
            and event_metadata.get("kind") == "cancel"
        ):
            await self._handle_channel_cancel(
                sender, sender_id, member_id, event_metadata
            )
            return
        attachments = _attachments
        expected_attachments = self._channel_attachment_count(event_metadata)
        if expected_attachments and attachments is None:
            complete = self._take_complete_attachment_group(message_id, event_metadata)
            if complete is None:
                group = self._channel_attachment_group(message_id)
                group["text_event"] = {
                    "sender": sender,
                    "sender_id": sender_id,
                    "text": text,
                    "metadata": event_metadata,
                    "message_id": message_id,
                    "sent_at": sent_at,
                    "replayed": replayed,
                }
                self._schedule_attachment_finalize(
                    message_id, CHANNEL_IMAGE_GROUP_TIMEOUT_S
                )
                return
            attachments = complete
        if attachments is None:
            attachments = ()
        if not replayed:
            self._observe_channel_turn_event(
                member_id or sender_id,
                event_metadata,
                text,
                sent_at=sent_at,
            )
        if not text:
            return
        display_sender = self._member_display_name(member_id, sender) or sender
        action = self._channel_text_action(event_metadata)
        if not action:
            if (
                event_metadata.get("schema") == "akid.v1"
                and event_metadata.get("kind") == "wait_user"
                and not replayed
            ):
                self._observe_channel_waiting_user(
                    member_id or sender_id, event_metadata
                )
            return
        busy = self._channel_busy()
        if action == "agent-progress":
            self._record_channel_message(
                sender=display_sender,
                sender_id=member_id or sender_id,
                text=text,
                from_agent=True,
                message_id=message_id,
                sent_at=sent_at,
                phase="progress",
                arrived_while_busy=busy,
                attachments=attachments,
            )
            if replayed:
                LOGGER.info(
                    "channel backlog progress recorded without turn seq=%s",
                    self._channel_history_seq,
                )
                if busy:
                    await self._refresh_channel_transcript()
                return
            if busy:
                await self._refresh_channel_transcript()
            return
        from_agent = action == "agent-final" or self._channel_sender_is_agent(
            sender, sender_id, event_metadata
        )
        nighttalk = from_agent and metadata_is_nighttalk(event_metadata)
        if not from_agent and not replayed:
            self._agent_initiation_remaining = max(
                0, int(self.config.agent_initiation_limit)
            )
        if await self._handle_channel_command(text, member_id or sender_id):
            return
        entry = self._record_channel_message(
            sender=display_sender,
            sender_id=member_id or sender_id,
            text=text,
            from_agent=from_agent,
            message_id=message_id,
            sent_at=sent_at,
            phase="final",
            arrived_while_busy=busy,
            attachments=attachments,
        )
        if replayed:
            LOGGER.info(
                "channel backlog message recorded without turn seq=%s sender_id=%s",
                entry.seq,
                member_id or sender_id or "unknown",
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        if from_agent and (member_id or sender_id) != self._channel_self_key():
            character = self._layer_character_for_member(
                member_id or sender_id,
                display_sender,
            )
            self._notify_layer(character, text)
        LOGGER.info(
            "channel message accepted sender_id=%s from_agent=%s seq=%s "
            "initiation_remaining=%s",
            member_id or sender_id or "unknown",
            from_agent,
            entry.seq,
            self._agent_initiation_remaining,
        )
        mentions = self._channel_message_mentions(text)
        wake = str(event_metadata.get("wake") or CHANNEL_WAKE_ALL).strip().lower()
        if wake not in (CHANNEL_WAKE_USER, CHANNEL_WAKE_MENTIONS, CHANNEL_WAKE_ALL):
            wake = CHANNEL_WAKE_ALL
        wake_ids = {
            self._history_member_id(item.strip(), item.strip()) or item.strip()
            for item in str(event_metadata.get("wakeIds") or "").split(",")
            if item.strip()
        }
        participants = self._channel_participants_for_event(
            from_agent=from_agent,
            mentions=mentions,
            wake=wake,
            wake_ids=wake_ids,
        )
        self_key = self._channel_self_key()
        directly_targeted = self_key in wake_ids or self_key in mentions
        should_wake = True
        if from_agent:
            if wake == CHANNEL_WAKE_USER:
                should_wake = False
            elif wake == CHANNEL_WAKE_MENTIONS:
                should_wake = directly_targeted
        if (
            not from_agent
            and mentions
            and self_key not in mentions
        ):
            LOGGER.info(
                "channel user message addressed to other agents; "
                "transcript-only seq=%s mentions=%s",
                entry.seq,
                ",".join(sorted(mentions)),
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        if from_agent and not should_wake:
            LOGGER.info(
                "channel agent reply recorded without wake seq=%s wake=%s targets=%s",
                entry.seq,
                wake,
                ",".join(sorted(wake_ids)),
            )
            if busy:
                await self._refresh_channel_transcript()
            return
        # A turn that has already sent its end event must not swallow a message
        # that arrived at the boundary. Route it through the normal queue path
        # instead of appending it to a turn whose busy list is about to drain.
        active = (
            self.active
            if self.active is not None and not self.active.end_sent
            else None
        )
        if from_agent and active is not None:
            if directly_targeted:
                active.busy_messages.append(
                    {
                        "sender": sender,
                        "sender_id": member_id or sender_id,
                        "text": text,
                        "seq": entry.seq,
                        "sent_at": sent_at,
                        "origin_is_agent": True,
                        "tool_results_at_arrival": active.tool_result_events,
                        "pending_tool_results_at_arrival": active.pending_tool_results,
                        "participants": participants,
                        "attachments": attachments,
                        "client_message_id": client_message_id,
                        "nighttalk": nighttalk,
                    }
                )
            await self._refresh_channel_transcript()
            return
        if not from_agent and active is not None:
            item = {
                "sender": sender,
                "sender_id": member_id or sender_id,
                "text": text,
                "seq": entry.seq,
                "sent_at": sent_at,
                "origin_is_agent": False,
                "tool_results_at_arrival": active.tool_result_events,
                "pending_tool_results_at_arrival": active.pending_tool_results,
                "participants": participants,
                "attachments": attachments,
                "client_message_id": client_message_id,
                "nighttalk": False,
            }
            active.busy_messages.append(item)
            if active.wait_user_call_active:
                active.wait_user_eligible_seqs.add(entry.seq)
            else:
                path = await self._write_turn_overlay_file(active, item)
                if path is not None:
                    active.overlay_files[entry.seq] = path
            active.user_message_event.set()
            await self._refresh_channel_transcript()
            return
        if not from_agent and not mentions:
            waiting_ids = self._channel_waiting_user_ids()
            if waiting_ids and self_key not in waiting_ids:
                LOGGER.info(
                    "channel user message routed only to wait_user members; "
                    "transcript-only seq=%s waiting=%s",
                    entry.seq,
                    ",".join(sorted(waiting_ids)),
                )
                if busy:
                    await self._refresh_channel_transcript()
                return
        if busy:
            await self._refresh_channel_transcript()
        lock_authorized, lock_authorization = self._channel_lock_authorization(
            text, from_agent=from_agent
        )
        self._enqueue_channel_turn(
            display_sender,
            member_id or sender_id,
            text,
            origin_is_agent=from_agent,
            channel_message_seq=entry.seq,
            participants=participants,
            attachments=attachments,
            sent_at=sent_at,
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            client_message_id=client_message_id,
            nighttalk=nighttalk,
        )
