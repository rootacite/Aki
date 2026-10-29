"""Build the per-turn prompt overlay and the status payload."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from engine.live_context import (
    LiveContextOptions,
    collect_live_context,
    render_channel_turn,
    render_channel_transcript,
    render_live_context,
    render_solo_memories,
    render_solo_mode,
    render_solo_transcript,
)
from engine.solo_memory import SOLO_MEMORY_BUDGET_TOKENS
from engine.solo_mode import load_solo_scene
from engine.life_ledger import render_ledger_block
from engine.protocol import SERVER_VERSION
from .constants import LOGGER
from .paths import (
    default_screenshot_dir,
    unique_capture_path,
)

class PromptsMixin:

    async def _refresh_channel_turn(self, turn: Turn | None) -> str:
        if turn is None or turn.origin != "mqtt":
            return ""
        solo = self._solo_session is not None
        wait_labels = (
            () if solo else self._wait_labels_for_participants(turn.channel_participants)
        )
        block = render_channel_turn(
            self_member=self._channel_self_member(),
            members=self._channel_member_views(),
            message=turn.channel_text,
            message_seq=turn.channel_message_seq,
            message_sender=turn.channel_sender,
            message_sender_id=turn.origin_sender_id,
            initiation_remaining=self._agent_initiation_remaining,
            initiation_limit=self.config.agent_initiation_limit,
            from_agent=turn.origin_is_agent,
            user_name=self.config.username,
            wait_labels=wait_labels,
            message_attachments=turn.channel_attachments,
            solo=solo,
        )
        return block


    async def _render_transcript_blocks(
        self,
        *,
        new_seq: int | None = None,
        limit: int | None = None,
    ) -> tuple[str, str, str, str, str]:
        """Render transcript/solo/shared blocks without touching persisted prompts."""
        path = self._agent_record_path()

        def with_attachment_state(
            entries: list[ChannelTranscriptEntry],
        ) -> list[ChannelTranscriptEntry]:
            return [
                replace(
                    entry,
                    attachments=tuple(
                        replace(
                            attachment,
                            missing=attachment.missing
                            or not attachment.path
                            or not Path(attachment.path).is_file(),
                        )
                        for attachment in entry.attachments
                    ),
                )
                if entry.attachments
                else entry
                for entry in entries
            ]

        solo_mode_block = ""
        if self._solo_session is not None:
            group_entries = with_attachment_state(
                list(self._frozen_group_history)
                if limit is None
                else self._frozen_group_history[: max(0, limit)]
            )
            group_block = render_channel_transcript(
                group_entries,
                new_seq=new_seq,
                budget_tokens=self.config.channel_transcript_budget_tokens,
                frozen=True,
            )
            solo_entries = with_attachment_state(
                list(self._channel_history)
                if limit is None
                else self._channel_history[: max(0, limit)]
            )
            solo_block = render_solo_transcript(
                solo_entries,
                budget_tokens=self.config.aki.solo_transcript_budget_tokens
                if self.config.aki is not None
                else 64 * 1024,
            )
            scene = self._solo_scene or load_solo_scene(
                self.config.profile,
                username=self.config.username,
                explicit=(
                    self.config.aki.solo_prompt_file
                    if self.config.aki is not None
                    else ""
                ),
            )
            self._solo_scene = scene
            solo_mode_block = render_solo_mode(
                member_name=self.config.channel_name,
                user_name=self.config.username,
                session_id=self._solo_session.session_id,
                scene=scene,
            )
            rendered_entries = len(group_entries)
            solo_entries_count = len(solo_entries)
        else:
            group_block = render_channel_transcript(
                with_attachment_state(
                    list(self._channel_history)
                    if limit is None
                    else self._channel_history[: max(0, limit)]
                ),
                new_seq=new_seq,
                budget_tokens=self.config.channel_transcript_budget_tokens,
            )
            solo_block = ""
            rendered_entries = len(self._channel_history)
            solo_entries_count = 0

        memory_config = self._solo_memory_llm_config()
        if memory_config is not None:
            solo_memories_block = await self.solo_memory.render_for_prompt(
                current_session_id=(
                    self._solo_session.session_id
                    if self._solo_session is not None
                    else ""
                ),
                **memory_config,
            )
        else:
            solo_memories_block = render_solo_memories(
                [
                    session
                    for session in self.solo_memory.sessions
                    if self._solo_session is None
                    or session.session_id != self._solo_session.session_id
                ],
                budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
            )
        shared_memory_block = self._public_memory_prompt_block()
        self._channel_transcript_status.update(
            {
                "budget_tokens": self.config.channel_transcript_budget_tokens,
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "rendered_seq": new_seq,
                "rendered_entries": rendered_entries,
                "block_tokens": len(group_block.encode("utf-8"))
                if group_block
                else 0,
                "solo_mode": self._solo_session is not None,
                "solo_entries": solo_entries_count,
                "solo_block_tokens": len(solo_block.encode("utf-8"))
                if solo_block
                else 0,
                "solo_memories_entries": len(self.solo_memory.sessions),
                "solo_memories_block_tokens": len(
                    solo_memories_block.encode("utf-8")
                )
                if solo_memories_block
                else 0,
                "solo_memories_error": self.solo_memory.last_error,
                "agent_path": str(path) if path is not None else "",
            }
        )
        return (
            group_block,
            solo_block,
            solo_mode_block,
            solo_memories_block,
            shared_memory_block,
        )


    async def _refresh_channel_transcript(
        self,
        *,
        new_seq: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Compatibility helper returning only the per-turn transcript overlay."""
        group_block, solo_block, _solo_mode, _solo_memories, _shared = (
            await self._render_transcript_blocks(new_seq=new_seq, limit=limit)
        )
        return self._transcript_overlay_block(group_block, solo_block)


    def _transcript_overlay_block(
        self,
        group_block: str,
        solo_block: str,
    ) -> str:
        parts: list[str] = []
        if self._solo_session is not None:
            if group_block:
                parts.append(group_block)
            if solo_block:
                parts.append(solo_block)
        elif group_block:
            parts.append(group_block)
        return "\n\n".join(parts)


    async def _capture_live_screenshot(self) -> dict[str, Any] | None:
        output_dir = self.config.screenshot_output_dir or default_screenshot_dir()
        output = unique_capture_path(output_dir / "live", "desktop", ".png")
        return await self._execute_screenshot_capture(
            {"include_cursor": False},
            output_path=output,
            timeout=min(self.config.live_context_timeout, 2.5),
        )


    async def _refresh_live_context(self) -> str:
        if not self.config.live_context_enabled:
            return ""
        path = self._agent_record_path()
        captured_at = datetime.now().astimezone()
        options = LiveContextOptions(
            ps_bin=self.config.ps_bin,
            qdbus_bin=self.config.qdbus_bin,
            history_path=self.config.zsh_history_path,
            history_limit=self.config.live_context_history_limit,
            process_limit=self.config.live_context_process_limit,
            timeout=self.config.live_context_timeout,
            screenshot_timeout=min(self.config.live_context_timeout, 2.5),
            include_screenshot=self.config.screenshot_enabled,
        )
        try:
            snapshot = await collect_live_context(
                options,
                screenshot_capture=(
                    self._capture_live_screenshot
                    if self.config.screenshot_enabled
                    else None
                ),
                now=captured_at,
            )
            block = render_live_context(
                snapshot,
                budget_tokens=self.config.live_context_budget_tokens,
                ledger_block=(
                    render_ledger_block(self.life_ledger.records())
                    if self.config.profile == "aki" and self.config.aki is not None
                    else ""
                ),
            )
            self._live_context_status.update(
                {
                    "enabled": True,
                    "budget_tokens": self.config.live_context_budget_tokens,
                    "last_captured_at": snapshot.captured_at.isoformat(
                        timespec="seconds"
                    ),
                    "last_sections": snapshot.sections(),
                    "last_errors": list(snapshot.errors),
                    "block_bytes": len(block.encode("utf-8")),
                    "agent_path": str(path) if path is not None else "",
                    "updated": True,
                }
            )
            return block
        except Exception as exc:
            LOGGER.warning("live context refresh failed: %s", exc)
            self._live_context_status.update(
                {
                    "last_captured_at": captured_at.isoformat(timespec="seconds"),
                    "last_errors": [str(exc)],
                    "agent_path": str(path) if path is not None else "",
                    "updated": False,
                }
            )
            return ""


    def _agent_info(self) -> tuple[str | None, str | None, int | None]:
        try:
            for path in (self.config.backend_dir / "agents").glob("*.json"):
                try:
                    data = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("id") == self.config.agent_id:
                    model = data.get("model")
                    settings = data.get("model_settings") or {}
                    reasoning = settings.get("reasoning_effort")
                    if reasoning is None:
                        reasoning = (settings.get("reasoning") or {}).get("reasoning_effort")
                    context_limit = settings.get("context_window_limit")
                    return (
                        model if isinstance(model, str) else None,
                        reasoning if isinstance(reasoning, str) else None,
                        context_limit if isinstance(context_limit, int) else None,
                    )
        except OSError:
            pass
        return None, None, None


    async def _status(self) -> dict[str, Any]:
        model, reasoning, context_limit = self._agent_info()
        tokens, token_error = await self._memory_tokens_cached(allow_error=True)
        active = None
        if self.active is not None:
            active = {
                "turn_id": self.active.turn_id,
                "state": self.active.state,
                "cancelled": self.active.cancelled,
                "started_at": self.active.started_at,
                "client_name": self.active.conn.client_name,
            }
        channel = self._channel_health()
        sandbox = self._sandbox_health()
        backend_ready = bool(getattr(self.backend, "ready", False))
        channel_ready = not channel.get("enabled", False) or bool(channel.get("connected", False))
        sandbox_ready = not sandbox.get("enabled", False) or bool(sandbox.get("available", False))
        return {
            "server_version": SERVER_VERSION,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self.started_at, 3),
            "socket": str(self.config.socket_path),
            "agent_id": self.config.agent_id,
            "profile": self.config.profile,
            "conversation_id": self.config.conversation_id,
            "channel_identity": {
                "id": self._channel_self_key(),
                "channel_id": self.config.channel_id,
                "name": self.config.channel_name,
                "aliases": list(self.config.channel_aliases),
                "description": self.config.channel_description,
            },
            "proxy": {
                "listen": f"{self.config.aki.proxy_host}:{self.config.aki.proxy_port}"
                if self.config.aki is not None
                else None,
                "owned_by_akid": False,
            },
            "agent_initiation": {
                "remaining": self._agent_initiation_remaining,
                "limit": self.config.agent_initiation_limit,
            },
            "execution_lock": self.execution_lock.status(),
            "model": model,
            "reasoning": reasoning,
            "context_limit": context_limit,
            "last_usage": self.last_usage,
            "active_turn": active,
            "compacting": self._compacting,
            "queue_depth": len(self.queue),
            "connections": len(self.connection_set),
            "memory_tokens": tokens,
            "memory_tokens_error": token_error,
            "backend": self._backend_health(),
            "channel": channel,
            "solo": self._solo_status(),
            "sandbox": sandbox,
            "external_tools": self._external_tools_health(),
            "tool_ownership": self._tool_ownership_status(),
            "tool_policy": self._tool_policy_status(),
            "proactive": self._proactive_status(),
            "live_context": self._live_context_health(),
            "layer": self._layer_health(),
            "avatar": self._avatar_health(),
            "backend_ready": backend_ready,
            "bridge_ready": self._aki_bridge_available(),
            "channel_ready": channel_ready,
            "sandbox_ready": sandbox_ready,
            "ready": (
                backend_ready
                and self._aki_bridge_available()
                and channel_ready
                and sandbox_ready
            ),
        }
