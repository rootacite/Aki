"""Publish channel presence, status, and control messages."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from typing import Mapping
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    truncate_member_description,
)
from .constants import (
    CAPABILITIES,
    CHANNEL_PROTOCOL_VERSION,
    CHANNEL_ROSTER_PROBE_INTERVAL,
    CHANNEL_STATUS_INTERVAL,
    LOGGER,
)

class ChannelStatusMixin:

    def _mqtt_status(self) -> dict[str, Any]:
        channel = self.channel
        return {
            "enabled": channel is not None,
            "host": self.config.mqtt_host,
            "port": self.config.mqtt_port,
            "topic": self.config.mqtt_topic,
            "state": channel.state if channel is not None else "disabled",
            "connected": channel.connected if channel is not None else False,
            "last_error": channel.last_error if channel is not None else None,
            "persisted": self.mqtt_state_path.exists(),
            "state_path": str(self.mqtt_state_path),
        }


    # -------------------------------------------------------- channel control
    def _start_channel_status_loop(self) -> None:
        if self._status_task is not None and not self._status_task.done():
            return
        self._status_stop.clear()
        self._status_task = asyncio.create_task(
            self._channel_status_loop(), name="akid-channel-status"
        )


    async def _channel_status_loop(self) -> None:
        next_status = 0.0
        next_probe = 0.0
        while not self._status_stop.is_set():
            accelerated = False
            now = time.monotonic()
            timeout = max(0.2, min(next_status, next_probe) - now)
            try:
                await asyncio.wait_for(self._status_event.wait(), timeout=timeout)
                accelerated = True
            except asyncio.TimeoutError:
                pass
            self._status_event.clear()
            if self._status_stop.is_set():
                break
            self._prune_channel_members()
            channel = self.channel
            if channel is None or not channel.connected:
                next_status = time.monotonic() + 1.0
                next_probe = next_status
                continue
            now = time.monotonic()
            if accelerated or now >= next_status:
                try:
                    await self._publish_channel_status()
                    next_status = now + CHANNEL_STATUS_INTERVAL
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.warning("channel status publish failed: %s", exc)
                    next_status = now + CHANNEL_STATUS_INTERVAL
            if now >= next_probe:
                try:
                    await self._publish_channel_probe()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.warning("channel roster probe failed: %s", exc)
                next_probe = now + CHANNEL_ROSTER_PROBE_INTERVAL + random.uniform(0.0, 2.0)


    def _schedule_status_refresh(self) -> None:
        if self.channel is not None:
            self._status_event.set()


    async def _on_channel_connected(self) -> None:
        with contextlib.suppress(Exception):
            await self._publish_channel_probe()
        self._schedule_status_refresh()
        with contextlib.suppress(Exception):
            await self._publish_channel_status()


    @staticmethod
    def _token_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, Mapping):
            for key in ("total_tokens", "total", "tokens", "count", "context_tokens"):
                candidate = value.get(key)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    return candidate
            numeric = [
                item
                for item in value.values()
                if isinstance(item, int) and not isinstance(item, bool)
            ]
            if numeric:
                return sum(numeric)
        return 0


    def _channel_capabilities(self) -> str:
        return ",".join(
            sorted(
                name
                for name, value in CAPABILITIES.items()
                if value is True and name not in {"queue_max", "protocol_version"}
            )
        )


    def _channel_identity_metadata(self) -> dict[str, str]:
        config = self.config
        member_id = self._channel_self_key()
        return {
            "schema": "akid.v1",
            "memberId": member_id,
            "memberName": config.channel_name,
            "sourceId": member_id,
            # Old clients keep using agentId as their stable key.
            "agentId": member_id,
            "agent": member_id,
            "agentName": config.channel_name,
            "description": truncate_member_description(config.channel_description),
            "memberKind": MEMBER_KIND_AGENT,
            "aliases": ",".join(config.channel_aliases[:16]),
            "protocolVersion": str(CHANNEL_PROTOCOL_VERSION),
        }


    def _channel_live_metadata(self) -> dict[str, str]:
        model, reasoning, _context_limit = self._agent_info()
        channel = self.channel
        channel_ready = bool(channel is not None and channel.connected)
        metadata = self._channel_identity_metadata()
        metadata.update(
            {
                "capabilities": self._channel_capabilities(),
                "model": model or "",
                "reasoning": reasoning or "",
                "backendReady": "true"
                if bool(getattr(self.backend, "ready", False))
                else "false",
                "channelReady": "true" if channel_ready else "false",
                "waitingUser": "true" if self._wait_user_active else "false",
                "waitingOther": "true" if self._wait_other_active else "false",
                "waitingOtherTargets": ",".join(self._wait_other_targets),
            }
        )
        return metadata


    def _channel_control_metadata(self) -> dict[str, str]:
        model, reasoning, context_limit = self._agent_info()
        usage = self.last_usage if isinstance(self.last_usage, dict) else {}
        context_tokens = self._token_count(
            usage.get("context_tokens") or usage.get("prompt_tokens")
        )
        if context_limit is None:
            value = usage.get("context_limit")
            context_limit = value if isinstance(value, int) else None
        cached_tokens = self._token_cache[1]
        sandbox = self._sandbox_health()
        sandbox_ready = not sandbox.get("enabled", False) or bool(
            sandbox.get("available", False)
        )
        metadata = self._channel_live_metadata()
        metadata.update(
            {
                "kind": "status",
                "contextLimit": str(context_limit or 0),
                "contextTokens": str(max(context_tokens, 0)),
                "sandboxReady": "true" if sandbox_ready else "false",
                "queueDepth": str(len(self.queue)),
                "activeTurn": "true" if self.active is not None else "false",
                "pendingApprovals": str(len(self.approvals)),
                "uptimeS": str(int(max(0.0, time.time() - self.started_at))),
                "memoryTokens": str(self._token_count(cached_tokens)),
            }
        )
        return metadata


    async def _publish_channel_control(
        self,
        text: str,
        metadata: Mapping[str, Any],
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> bool:
        channel = self.channel
        if channel is None or not channel.connected:
            return False
        await channel.publish_control(text, metadata, qos=qos, retain=retain)
        return True


    async def _publish_channel_status(self) -> None:
        await self._publish_channel_control(
            f"{self.config.channel_name} status", self._channel_control_metadata()
        )


    async def _publish_channel_probe(self) -> None:
        metadata = self._channel_live_metadata()
        metadata.update(
            {"kind": "probe", "probeId": f"probe-{uuid.uuid4().hex}"}
        )
        await self._publish_channel_control(
            f"{self.config.channel_name} probe", metadata
        )


    async def _publish_channel_presence(self, probe_id: str) -> None:
        metadata = self._channel_live_metadata()
        metadata.update({"kind": "presence", "probeId": probe_id})
        await self._publish_channel_control(
            f"{self.config.channel_name} is online", metadata
        )


    async def _publish_channel_offline(self) -> None:
        metadata = self._channel_identity_metadata()
        metadata.update({"kind": "offline"})
        await self._publish_channel_control(
            f"{self.config.channel_name} is offline", metadata
        )
