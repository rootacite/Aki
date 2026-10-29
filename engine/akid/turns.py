"""Queue a turn and run it against the Letta backend."""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from engine.letta_process import BackendUnavailable
from engine.live_context import render_channel_roster
from engine.tokifactor import (
    MAX_IMAGE_BYTES,
    detect_image_mime,
)
from .constants import (
    LOGGER,
    MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES,
    WAIT_GUARD_ORIGIN,
)

class TurnsMixin:

    # ------------------------------------------------------------------ turns
    def _spawn_turn(self, turn: Turn) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._run_turn(turn), name=f"turn:{turn.turn_id}")
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return task


    async def _build_user_content(
        self, turn: Turn
    ) -> str | list[dict[str, Any]]:
        if not turn.channel_attachments:
            return turn.message
        parts: list[dict[str, Any]] = [{"type": "text", "text": turn.message}]
        total = 0
        for attachment in sorted(turn.channel_attachments, key=lambda item: item.index):
            if attachment.missing or not attachment.path:
                continue
            path = Path(attachment.path)
            try:
                if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                    continue
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                continue
            if not data or total + len(data) > MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES:
                continue
            mime = attachment.mime or detect_image_mime(data)
            if mime is None:
                continue
            total += len(data)
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        return parts if len(parts) > 1 else turn.message


    async def _run_turn(self, turn: Turn) -> None:
        if turn.end_sent:
            return
        turn.state = "running"
        turn.started_at = time.time()
        self._schedule_status_refresh()
        LOGGER.info("turn started id=%s queue=%s", turn.turn_id, len(self.queue))
        try:
            await self._emit(
                turn,
                "turn_started",
                {
                    "client_message_id": turn.client_message_id,
                    "include_reasoning": turn.include_reasoning,
                    "origin": turn.origin,
                    "origin_sender_id": turn.origin_sender_id,
                    "origin_is_agent": turn.origin_is_agent,
                },
            )
            if not bool(getattr(self.backend, "ready", False)):
                await self._emit(
                    turn,
                    "error",
                    {"code": "BACKEND_UNAVAILABLE", "message": "Letta backend is not ready"},
                )
                await self._finish_turn(turn, "backend_unavailable")
                return
            # Legacy execution-lock blocks are removed by the one-time
            # migration. Runtime turns only add their own overlay.
            if not self._aki_bridge_available():
                await self._emit(
                    turn,
                    "error",
                    {
                        "code": "BRIDGE_REQUIRED",
                        "message": (
                            "Aki requires the customized Letta runtime with "
                            "aki_bridge"
                        ),
                    },
                )
                await self._finish_turn(turn, "bridge_unavailable")
                return
            overlay_parts = await self._build_turn_overlay(turn)
            live_block = await self._refresh_live_context()
            if live_block:
                overlay_parts.append(live_block)
            overlay = (
                {"turn_context": {"system_append": "\n\n".join(overlay_parts)}}
                if overlay_parts
                else {}
            )
            try:
                await self.backend.begin_aki_turn(turn.turn_id, overlay)
                turn.bridge_turn_started = True
            except Exception as exc:
                LOGGER.warning(
                    "failed to install Aki turn overlay turn=%s: %s",
                    turn.turn_id,
                    exc,
                )
            try:
                await self.backend.send_user(await self._build_user_content(turn))
            except BackendUnavailable:
                await self._emit(
                    turn,
                    "error",
                    {"code": "BACKEND_UNAVAILABLE", "message": "Letta backend is unavailable"},
                )
                await self._finish_turn(turn, "backend_unavailable")
                return
            await turn.done_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("turn failed id=%s", turn.turn_id)
            await self._finish_turn(turn, "internal_error")
        finally:
            if not turn.end_sent:
                await self._finish_turn(turn, "error")


    async def _build_turn_overlay(self, turn: Turn) -> list[str]:
        """Build the complete runtime system overlay for this turn."""
        overlay_parts: list[str] = [
            self._tool_ownership_block(),
            self._channel_policy_block(),
            self._channel_wake_routing_block(),
            self._channel_identity_block(),
        ]
        new_seq = (
            turn.channel_message_seq
            if turn.origin in ("mqtt", "proactive")
            else None
        )
        (
            group_block,
            solo_block,
            solo_mode_block,
            solo_memories_block,
            shared_memory_block,
        ) = await self._render_transcript_blocks(new_seq=new_seq)
        if solo_mode_block:
            overlay_parts.append(solo_mode_block)
        if solo_memories_block:
            overlay_parts.append(solo_memories_block)
        if shared_memory_block:
            overlay_parts.append(shared_memory_block)

        if turn.origin == "solo_notice":
            transcript = self._transcript_overlay_block(group_block, solo_block)
            if transcript:
                overlay_parts.append(transcript)
            await self._snapshot_turn_conversation(turn)
        elif turn.origin in ("mqtt", "proactive"):
            block = await self._refresh_channel_turn(turn)
            if block:
                overlay_parts.append(block)
            if group_block:
                overlay_parts.append(group_block)
            if solo_block:
                overlay_parts.append(solo_block)
            overlay_parts.append(
                render_channel_roster(
                    self_member=self._channel_self_member(),
                    members=self._channel_member_views(),
                )
            )
            if turn.origin_is_agent:
                await self._snapshot_turn_conversation(turn)
        elif turn.origin == WAIT_GUARD_ORIGIN:
            if group_block:
                overlay_parts.append(group_block)
            if solo_block:
                overlay_parts.append(solo_block)
            overlay_parts.append(
                render_channel_roster(
                    self_member=self._channel_self_member(),
                    members=self._channel_member_views(),
                )
            )
        return overlay_parts


    async def _update_busy_turn_overlay(self, turn: Turn) -> None:
        """Refresh a running bridge turn after a during-work message."""
        if not turn.bridge_turn_started or not self._aki_bridge_available():
            return
        try:
            parts = await self._build_turn_overlay(turn)
            live_block = await self._refresh_live_context()
            if live_block:
                parts.append(live_block)
            await self.backend.update_aki_turn(
                turn.turn_id,
                {"turn_context": {"system_append": "\n\n".join(parts)}},
            )
        except Exception as exc:
            LOGGER.warning(
                "failed to refresh busy turn overlay turn=%s: %s",
                turn.turn_id,
                exc,
            )
