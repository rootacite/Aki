"""Finish, cancel, and roll back a turn."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from typing import Mapping
from engine.live_context import render_user_insertions
from .constants import (
    LOGGER,
    WAIT_GUARD_ORIGIN,
)
from .history import _content_text
from .no_reply import contains_no_reply_marker
from .paths import now_iso

class TurnFinishMixin:

    async def _finish_turn(self, turn: Turn, reason: str, extra: Mapping[str, Any] | None = None) -> None:
        if turn.end_sent:
            return
        turn.end_sent = True
        self._release_turn_lock(turn)
        turn.finished_at = time.time()
        turn.state = "finished"
        assistant_text = "".join(turn.assistant_parts)
        guard_targets: list[tuple[str, str, str]] = []
        if (
            reason == "completed"
            and assistant_text
            and not contains_no_reply_marker(assistant_text)
        ):
            guard_targets = self._unfinished_wait_targets(turn)
        if guard_targets:
            turn.wait_guard_blocked = True
            self.channel_conn.discard_turn_output(turn.turn_id)
            self._enqueue_wait_guard_turn(turn, guard_targets)
        if assistant_text and not turn.wait_guard_blocked:
            await self._emit(
                turn,
                "assistant_message",
                {
                    "content": assistant_text,
                    "text": assistant_text,
                },
            )
            if reason == "completed":
                if turn.origin in ("proactive", "solo_notice"):
                    await self._publish_proactive_text(turn, assistant_text)
                elif turn.origin not in ("mqtt", WAIT_GUARD_ORIGIN):
                    self._notify_layer(self._layer_own_character(), assistant_text)
        data: dict[str, Any] = {
            "reason": reason,
            "cancelled": turn.cancelled,
            "stop_reason": turn.final_result.get("stop_reason"),
            "subtype": turn.final_result.get("subtype"),
            "result": turn.final_result.get("result"),
            "duration_ms": round(((turn.finished_at or time.time()) - (turn.started_at or time.time())) * 1000),
        }
        if turn.wait_guard_blocked:
            data["wait_guard"] = True
            data["wait_targets"] = [
                name or label for _member_id, name, label in guard_targets
            ]
        if extra:
            data.update(extra)
        await self._emit(turn, "turn_end", data)
        LOGGER.info(
            "turn ended id=%s reason=%s duration_ms=%s",
            turn.turn_id,
            reason,
            data["duration_ms"],
        )
        if turn.bridge_turn_started:
            with contextlib.suppress(Exception):
                await self.backend.end_aki_turn(turn.turn_id)
            turn.bridge_turn_started = False
        task = asyncio.create_task(
            self._complete_finished_turn(turn),
            name=f"channel-finalize:{turn.turn_id}",
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)


    async def _complete_finished_turn(self, turn: Turn) -> None:
        """Complete rollback, overlay persistence and turn handoff in order."""
        try:
            if turn.bridge_snapshot_id is not None:
                await self._restore_channel_turn_bridge(turn)
            elif turn.conversation_snapshot is not None:
                await self._restore_channel_turn(turn)
        finally:
            try:
                await self._finalize_turn_overlays(turn)
            finally:
                self._queue_unhandled_busy_messages(turn)
                await self._finalize_turn_state(turn)


    def _queue_unhandled_busy_messages(self, turn: Turn) -> None:
        queued = 0
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)) and str(
                item.get("sender_id") or ""
            ) in turn.waited_agent_ids:
                continue
            arrival_count = int(item.get("tool_results_at_arrival", 0))
            handled = (
                not turn.cancelled
                and (
                    int(item.get("pending_tool_results_at_arrival", 0)) > 0
                    or turn.tool_result_events > arrival_count
                )
            )
            if handled:
                continue
            origin_is_agent = bool(item.get("origin_is_agent", False))
            text = str(item["text"])
            lock_authorized, lock_authorization = self._channel_lock_authorization(
                text, from_agent=origin_is_agent
            )
            self._enqueue_channel_turn(
                str(item["sender"]),
                str(item["sender_id"]),
                text,
                origin_is_agent=origin_is_agent,
                channel_message_seq=int(item["seq"]),
                participants=frozenset(item.get("participants") or ()),
                attachments=tuple(item.get("attachments") or ()),
                sent_at=int(item.get("sent_at") or 0),
                lock_authorized=lock_authorized,
                lock_authorization=lock_authorization,
                client_message_id=(
                    str(item.get("client_message_id") or "").strip() or None
                ),
            )
            queued += 1
        if queued:
            LOGGER.info(
                "queued deferred messages after busy turn id=%s count=%s",
                turn.turn_id,
                queued,
            )
        turn.busy_messages.clear()


    async def _restore_channel_turn(self, turn: Turn) -> None:
        try:
            if turn.conversation_snapshot is not None:
                await self._restore_conversation_snapshot(turn.conversation_snapshot)
        except Exception:
            LOGGER.exception("channel conversation rollback failed id=%s", turn.turn_id)


    async def _restore_channel_turn_bridge(self, turn: Turn) -> None:
        try:
            snapshot_id = turn.bridge_snapshot_id
            if snapshot_id:
                await self.backend.restore_conversation(snapshot_id)
                turn.bridge_snapshot_id = None
        except Exception:
            LOGGER.exception(
                "Aki bridge conversation rollback failed id=%s; using file snapshot",
                turn.turn_id,
            )
            if turn.conversation_snapshot is not None:
                await self._restore_conversation_snapshot(turn.conversation_snapshot)


    @staticmethod
    def _jsonl_row_text(row: Mapping[str, Any]) -> str:
        message = row.get("message")
        if not isinstance(message, Mapping):
            return ""
        return _content_text(message.get("content"))


    @classmethod
    def _is_human_channel_user_row(cls, row: Mapping[str, Any]) -> bool:
        message = row.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "user":
            return False
        metadata = message.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("compaction"):
            return False
        text = cls._jsonl_row_text(row)
        return (
            "[akid MQTT channel message]" in text
            and "\nmessage:\n" in text
        )


    @classmethod
    def _find_user_role_index(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        expected_prompt: str = "",
        human_only: bool = False,
    ) -> int | None:
        fallback: int | None = None
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            message = row.get("message")
            if (
                row.get("type") != "message"
                or not isinstance(message, Mapping)
                or message.get("role") != "user"
            ):
                continue
            metadata = message.get("metadata")
            if isinstance(metadata, Mapping) and metadata.get("compaction"):
                continue
            text = cls._jsonl_row_text(row)
            if expected_prompt and expected_prompt in text:
                return index
            if cls._is_human_channel_user_row(row):
                if fallback is None:
                    fallback = index
                continue
            if not human_only and fallback is None:
                fallback = index
        return fallback


    @staticmethod
    def _replace_messages_file(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise


    async def _merge_consumed_user_messages(
        self, turn: Turn, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        """Append provider-consumed human messages to the latest human user role."""
        if not messages:
            return
        if turn.origin not in ("mqtt", WAIT_GUARD_ORIGIN):
            raise ValueError("consumed overlay messages require a channel turn")
        path = self._conversation_dir() / "messages.jsonl"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        rows: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                rows.append({})
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(
                    f"conversation row {index + 1} is not valid JSON"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"conversation row {index + 1} is not an object")
            rows.append(value)
        human_only = turn.origin_is_agent or turn.origin == WAIT_GUARD_ORIGIN
        target_index = self._find_user_role_index(
            rows,
            expected_prompt="" if human_only else turn.message,
            human_only=human_only,
        )
        if target_index is None:
            raise ValueError("no human user role available for channel merge")
        target = rows[target_index]
        message = target.get("message")
        if not isinstance(message, dict):
            raise ValueError("target user role has no message object")
        content = message.get("content")
        if isinstance(content, str):
            parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = [
                dict(part)
                if isinstance(part, dict)
                else {"type": "text", "text": str(part)}
                for part in content
            ]
        else:
            parts = []
        blocks: list[str] = []
        for item in sorted(
            messages,
            key=lambda value: (
                int(value.get("inserted_at_ms") or value.get("sent_at") or 0),
                int(value.get("seq") or 0),
            ),
        ):
            record_item = item if isinstance(item, dict) else dict(item)
            record = self._build_insertion_record(turn, record_item)
            blocks.append(str(record["content"]))
        rendered = render_user_insertions(
            blocks, turn_id=turn.turn_id, during_work=True
        )
        if rendered:
            parts.append({"type": "text", "text": rendered})
        message["content"] = parts
        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            metadata["updated_at"] = now_iso()
        lines[target_index] = (
            json.dumps(target, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            self._replace_messages_file(path, b"".join(lines))
            await self.backend.reload_conversation(
                conversation_id=self.config.conversation_id,
                agent_id=self.config.agent_id,
            )
        except Exception:
            with contextlib.suppress(Exception):
                self._replace_messages_file(path, original)
                await self.backend.reload_conversation(
                    conversation_id=self.config.conversation_id,
                    agent_id=self.config.agent_id,
                )
            raise
        turn.user_role_entry_id = str(target.get("id") or "")
        turn.merged_user_messages.extend(dict(item) for item in messages)


    def _enqueue_merged_overlay_turn(
        self, turn: Turn, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        ordered = sorted(
            messages,
            key=lambda value: (
                int(value.get("seq") or 0),
                int(value.get("sent_at") or 0),
            ),
        )
        if not ordered:
            return
        text = "\n\n".join(
            str(item.get("text") or "").strip()
            for item in ordered
            if str(item.get("text") or "").strip()
        )
        attachments: list[ChannelImageAttachment] = []
        participants: set[str] = set()
        for item in ordered:
            attachments.extend(item.get("attachments") or ())
            participants.update(item.get("participants") or ())
        last = ordered[-1]
        client_message_id = ""
        for item in reversed(ordered):
            client_message_id = str(item.get("client_message_id") or "").strip()
            if client_message_id:
                break
        lock_authorized, lock_authorization = self._channel_lock_authorization(
            text, from_agent=False
        )
        self._enqueue_channel_turn(
            str(last.get("sender") or turn.channel_sender or "user"),
            str(last.get("sender_id") or turn.origin_sender_id or ""),
            text,
            origin_is_agent=False,
            channel_message_seq=int(last.get("seq") or 0) or None,
            participants=frozenset(participants),
            attachments=tuple(attachments),
            sent_at=int(last.get("sent_at") or 0),
            lock_authorized=lock_authorized,
            lock_authorization=lock_authorization,
            client_message_id=client_message_id or None,
        )
        LOGGER.info(
            "merged unconsumed overlay messages into a persistent turn "
            "turn=%s count=%s seq=%s",
            turn.turn_id,
            len(ordered),
            last.get("seq"),
        )


    def _cleanup_turn_overlay_files(self, turn: Turn) -> None:
        for path in list(turn.overlay_files.values()):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        turn.overlay_files.clear()
        with contextlib.suppress(OSError):
            (self._turn_overlay_dir() / turn.turn_id).rmdir()


    async def _finalize_turn_overlays(self, turn: Turn) -> None:
        human_items: list[dict[str, Any]] = []
        agent_items: list[dict[str, Any]] = []
        for item in turn.busy_messages:
            if bool(item.get("origin_is_agent", False)):
                agent_items.append(item)
            else:
                human_items.append(item)
        turn.busy_messages = agent_items
        human_items.sort(
            key=lambda item: (
                int(item.get("seq") or 0),
                int(item.get("sent_at") or 0),
            )
        )
        consumed: list[dict[str, Any]] = []
        unconsumed: list[dict[str, Any]] = []
        for item in human_items:
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0 and seq in turn.waited_user_seqs:
                self._remove_turn_overlay_file(turn, seq)
                continue
            path = turn.overlay_files.get(seq)
            if path is not None and not path.exists():
                turn.overlay_consumed_seqs.add(seq)
                consumed.append(item)
            else:
                unconsumed.append(item)
        if consumed:
            try:
                await self._merge_consumed_user_messages(turn, consumed)
            except Exception:
                LOGGER.exception(
                    "failed to merge consumed channel messages into user role "
                    "turn=%s; keeping them consumed without a duplicate turn",
                    turn.turn_id,
                )
        self._enqueue_merged_overlay_turn(turn, unconsumed)
        self._cleanup_turn_overlay_files(turn)


    async def _finalize_turn_state(self, turn: Turn) -> None:
        if self._wait_user_active:
            with contextlib.suppress(Exception):
                await self._set_wait_user_active(False)
        if self._wait_other_active:
            with contextlib.suppress(Exception):
                await self._set_wait_other_active(False)
        self._release_turn_lock(turn)
        turn.done_event.set()
        if self.active is turn:
            self.active = None
        self._schedule_status_refresh()
        await self._maybe_enqueue_proactive_turn()
        await self._maybe_start_next()


    async def _cancel_turn(self, turn: Turn, *, disconnect: bool = False) -> None:
        if turn.cancelled or turn.end_sent:
            return
        turn.cancelled = True
        for approval in list(self.approvals.values()):
            if approval.turn is turn and not approval.future.done():
                approval.future.set_result((False, "turn cancelled"))
        if self._shutting_down:
            self._release_turn_lock(turn)
            turn.end_sent = True
            turn.done_event.set()
            if self.active is turn:
                self.active = None
            return
        if bool(getattr(self.backend, "ready", False)):
            with contextlib.suppress(Exception):
                await self.backend.send_interrupt()
        LOGGER.info("turn cancel requested id=%s disconnect=%s", turn.turn_id, disconnect)
        try:
            await asyncio.wait_for(turn.done_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            await self._finish_turn(
                turn,
                "cancelled",
                {"message": "turn cancelled by client" if not disconnect else "client disconnected"},
            )


    def _release_turn_lock(self, turn: Turn) -> None:
        if not turn.lock_held and not self.execution_lock.held_by(
            profile=self.config.profile, turn_id=turn.turn_id
        ):
            return
        self.execution_lock.release(
            profile=self.config.profile, turn_id=turn.turn_id
        )
        turn.lock_held = False


    def _find_turn(self, turn_id: str) -> Turn | None:
        if self.active is not None and self.active.turn_id == turn_id:
            return self.active
        for turn in self.queue:
            if turn.turn_id == turn_id:
                return turn
        return None
