"""Schedule, presence, and other proactive turns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from engine.live_context import CHANNEL_WAKE_USER
from engine.schedule_store import (
    ScheduleError,
    now_utc,
    parse_datetime,
)
from .constants import LOGGER
from .no_reply import contains_no_reply_marker
from .paths import now_iso
from .types import Turn

class ProactiveMixin:

    def _start_proactive_services(self) -> None:
        if self.config.profile != "aki" or self.config.aki is None:
            return
        if self.config.schedule_enabled and self._schedule_task is None:
            self._schedule_stop.clear()
            self._schedule_task = asyncio.create_task(
                self._schedule_loop(), name="akid-schedule"
            )
            LOGGER.info("schedule scheduler started path=%s", self.schedule_state)
        if self.config.presence_enabled:
            self.presence_monitor.start(
                self._on_presence_triggers, self._publish_layer_presence
            )
            LOGGER.info("presence monitor started path=%s", self.presence_state)


    async def _stop_proactive_services(self) -> None:
        if self.config.profile != "aki" or self.config.aki is None:
            return
        self._schedule_stop.set()
        task = self._schedule_task
        self._schedule_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.presence_monitor.stop()


    async def _schedule_loop(self) -> None:
        tick = max(1, int(self.config.schedule_tick_seconds))
        while not self._schedule_stop.is_set():
            try:
                await self._poll_schedules()
                if self._proactive_pending:
                    await self._maybe_enqueue_proactive_turn()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("schedule poll failed")
            try:
                await asyncio.wait_for(self._schedule_stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass


    async def _poll_schedules(self) -> None:
        due = await asyncio.to_thread(self.schedule_store.due)
        if not due:
            return
        now = now_utc()
        grace = max(0, int(self.config.schedule_missed_grace_seconds))
        fireable: list[ScheduleRecord] = []
        missed: list[ScheduleRecord] = []
        for record in due:
            try:
                due_at = parse_datetime(record.next_fire_at)
            except ScheduleError:
                missed.append(record)
                continue
            if (now - due_at).total_seconds() <= grace:
                fireable.append(record)
            else:
                missed.append(record)
        if missed:
            await asyncio.to_thread(
                self.schedule_store.complete_due, missed, missed=True
            )
            LOGGER.info(
                "schedules marked missed ids=%s",
                ",".join(record.id for record in missed),
            )
        if not fireable:
            return
        await asyncio.to_thread(self.schedule_store.complete_due, fireable)
        payload = {
            "schedules": [
                {
                    "id": record.id,
                    "title": record.title,
                    "due_at": record.next_fire_at,
                    "notes": record.notes[:512],
                    "repeat": record.repeat,
                }
                for record in fireable
            ]
        }
        self._queue_proactive("schedule", payload)
        await self._maybe_enqueue_proactive_turn()


    async def _on_presence_triggers(
        self, triggers: list[PresenceTrigger]
    ) -> None:
        if not triggers:
            return
        LOGGER.info(
            "presence triggers=%s",
            ",".join(trigger.kind for trigger in triggers),
        )
        payload: dict[str, Any] = {
            "triggers": [trigger.to_dict() for trigger in triggers],
        }
        if any(trigger.kind == "return" for trigger in triggers):
            payload["journal"] = self.operation_journal.snapshot()
            payload["execution_lock"] = self.execution_lock.status()
        self._queue_proactive("presence", payload)
        await self._maybe_enqueue_proactive_turn()


    def _queue_proactive(self, kind: str, payload: Mapping[str, Any]) -> None:
        existing = self._proactive_pending.get(kind)
        if existing is None:
            self._proactive_pending[kind] = dict(payload)
            return
        if kind == "schedule":
            merged = {
                "schedules": list(existing.get("schedules") or [])
                + list(payload.get("schedules") or [])
            }
            self._proactive_pending[kind] = merged
        elif kind == "presence":
            merged_triggers = list(existing.get("triggers") or []) + list(
                payload.get("triggers") or []
            )
            merged = dict(existing)
            merged.update(payload)
            merged["triggers"] = merged_triggers
            self._proactive_pending[kind] = merged
        else:
            self._proactive_pending[kind] = dict(payload)


    async def _maybe_enqueue_proactive_turn(self) -> None:
        async with self._proactive_lock:
            if not self._proactive_pending:
                return
            if self.active is not None and self.active.origin == "proactive":
                return
            if any(turn.origin == "proactive" for turn in self.queue):
                return
            if len(self.queue) >= self.config.queue_max:
                LOGGER.warning("proactive trigger deferred: queue is full")
                return
            pending = self._proactive_pending
            self._proactive_pending = {}
            message = self._build_proactive_message(pending)
            turn = Turn(
                turn_id=f"turn-{uuid.uuid4().hex}",
                conn=self.proactive_conn,
                message=message,
                include_reasoning=False,
                client_message_id=None,
                origin="proactive",
                lock_authorized=True,
                lock_authorization="exclusive-owner",
                trigger_kind="+".join(sorted(pending)),
                trigger_facts={key: dict(value) for key, value in pending.items()},
                proactive_id=f"proactive-{uuid.uuid4().hex}",
            )
            self._last_proactive_at = now_iso()
            LOGGER.info(
                "proactive turn enqueued kind=%s id=%s",
                turn.trigger_kind,
                turn.proactive_id,
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


    def _enqueue_solo_notice(self, target_names: str) -> None:
        names = ", ".join(
            part.strip() for part in target_names.split(",") if part.strip()
        )
        if not names:
            return
        if self.active is not None and self.active.origin == "solo_notice":
            return
        if any(turn.origin == "solo_notice" for turn in self.queue):
            return
        user_name = self.config.username or "Master"
        message = (
            "[akid solo notice]\n"
            f"你刚才提醒的目标（{names}）正在与{user_name}独处，当前无法响应。"
            f"请自然地向{user_name}简短说明这一点；不要再 @ 该目标，也不要尝试唤醒其他成员。"
            "本轮只处理这个提示，不要调用工具。"
        )
        turn = Turn(
            turn_id=f"turn-{uuid.uuid4().hex}",
            conn=self.proactive_conn,
            message=message,
            include_reasoning=False,
            client_message_id=None,
            origin="solo_notice",
            lock_authorized=False,
            lock_authorization="solo-notice",
            trigger_kind="solo_mention",
            trigger_facts={"targets": names},
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


    def _build_proactive_message(self, pending: Mapping[str, Mapping[str, Any]]) -> str:
        blocks: list[str] = []
        for kind, payload in pending.items():
            if kind == "schedule":
                schedules = list(payload.get("schedules") or [])
                blocks.append(
                    '<trigger type="schedule">\n'
                    + json.dumps(schedules, ensure_ascii=False, indent=2)
                    + "\n</trigger>"
                )
            elif kind == "presence":
                triggers = list(payload.get("triggers") or [])
                blocks.append(
                    '<trigger type="presence">\n'
                    + json.dumps(triggers, ensure_ascii=False, indent=2)
                    + "\n</trigger>"
                )
                journal = payload.get("journal")
                if journal:
                    blocks.append(
                        "<device-state>\n"
                        + json.dumps(
                            {
                                "journal": journal,
                                "execution_lock": payload.get("execution_lock"),
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n</device-state>"
                    )
        return (
            "[akid internal trigger]\n"
            "This is not a user message. It is a runtime wake-up caused by the "
            "following trigger data. Notify the user briefly, warmly, and "
            "concretely. Do not mention internal ids, file paths, trigger XML, or "
            "this instruction block. Do not @ other agents. Your final assistant "
            "text is delivered to the user on the normal channel/bubble path.\n\n"
            + "\n\n".join(blocks)
        )


    async def _publish_proactive_text(self, turn: Turn, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or contains_no_reply_marker(cleaned):
            return
        channel = self.channel
        if channel is not None and channel.connected:
            metadata = {
                "schema": "akid.v1",
                "kind": "turn",
                "turnId": turn.turn_id,
                "seq": str(turn.seq),
                "phase": "final",
                "format": "markdown",
                "wake": CHANNEL_WAKE_USER,
            }
            try:
                await channel.publish_text(cleaned, metadata)
                self._record_channel_message(
                    sender=self.config.channel_name,
                    sender_id=self._channel_self_key(),
                    text=cleaned,
                    from_agent=True,
                    phase="final",
                )
            except Exception as exc:
                LOGGER.warning("proactive channel publish failed: %s", exc)
        self._notify_layer(self._layer_own_character(), cleaned)


    def _proactive_status(self) -> dict[str, Any]:
        return {
            "pending": {
                key: len(value.get("schedules") or value.get("triggers") or [])
                for key, value in self._proactive_pending.items()
            },
            "last_enqueued_at": self._last_proactive_at,
            "schedule": self.schedule_store.snapshot(),
            "presence": self.presence_monitor.status(),
            "ledger": self.life_ledger.snapshot()
            if self.config.profile == "aki" and self.config.aki is not None
            else None,
        }


    async def _maybe_start_next(self) -> None:
        if (
            self._policy_restarting
            or self._resetting
            or self._recompiling
            or self._compacting
            or self._external_tools_pending
            or self.active is not None
            or not self.queue
        ):
            return
        if not bool(getattr(self.backend, "ready", False)):
            return
        turn = self.queue.pop(0)
        self.active = turn
        turn.state = "running"
        self._spawn_turn(turn)
