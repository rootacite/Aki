"""Local socket connections and the synthetic channel connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import (
    dataclass,
    field,
)
from engine.live_context import (
    CHANNEL_WAKE_ALL,
    CHANNEL_WAKE_MENTIONS,
    CHANNEL_WAKE_USER,
    match_channel_mentions,
    route_channel_reply,
    strip_channel_mentions,
    strip_channel_non_wakes,
)
from engine.tokifactor import ImageEnvelope
from engine.protocol import encode_message
from .constants import (
    CHANNEL_EVENT_QUEUE_MAX,
    LOGGER,
)
from .no_reply import contains_no_reply_marker
from .tool_detail import (
    render_tool_result,
    render_tool_value,
    tool_detail_body,
)
from .types import ChannelTurnState



@dataclass(eq=False)
class Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connection_id: str
    uid: int | None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    handshaken: bool = False
    client_name: str = ""
    client_version: str = ""
    closed: bool = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    async def send(self, message: Mapping[str, Any]) -> None:
        if self.closed:
            return
        payload = encode_message(message)
        async with self.send_lock:
            if self.closed or self.writer.is_closing():
                return
            self.writer.write(payload)
            await self.writer.drain()



class NullConnection:
    """Event sink for internal proactive turns that must not publish themselves."""

    connection_id = "proactive"
    client_name = "akid-proactive"
    client_version = "1"
    uid: int | None = None
    closed = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = {}

    async def send(self, message: Mapping[str, Any]) -> None:
        return



@dataclass(eq=False)
class ChannelConnection:
    """Synthetic initiator for turns originating on the MQTT channel."""

    server: "GatewayServer"
    connection_id: str = "mqtt-channel"
    client_name: str = "mqtt-channel"
    client_version: str = "1"
    uid: int | None = None
    closed: bool = False
    idempotent: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    _queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CHANNEL_EVENT_QUEUE_MAX)
    )
    _worker: asyncio.Task[None] | None = None
    _pending_progress: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    _turns: dict[str, "ChannelTurnState"] = field(default_factory=dict)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(
            self._publish_loop(), name="akid-channel-events"
        )

    async def stop(self) -> None:
        self.closed = True
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        self._pending_progress.clear()
        self._turns.clear()

    def _turn_state(self, turn_id: str) -> "ChannelTurnState":
        state = self._turns.get(turn_id)
        if state is None:
            state = ChannelTurnState()
            self._turns[turn_id] = state
        return state

    def _enqueue(self, item: dict[str, Any], *, key: tuple[str, int] | None = None) -> None:
        if key is not None:
            pending = self._pending_progress.get(key)
            if pending is not None:
                pending.update(item)
                return
            item["_key"] = key
            self._pending_progress[key] = item
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if key is not None:
                self._pending_progress.pop(key, None)
            LOGGER.warning("channel event queue full; dropping type=%s", item.get("kind"))
            return
        self._ensure_worker()

    def _enqueue_text(
        self,
        text: str,
        metadata: Mapping[str, Any],
        *,
        key: tuple[str, int] | None = None,
        images: list[dict[str, Any]] | None = None,
        client_message_id: str = "",
    ) -> None:
        values = {key: str(value) for key, value in metadata.items()}
        if client_message_id and "clientMessageId" not in values:
            values["clientMessageId"] = client_message_id
        self._enqueue(
            {
                "kind": str(values.get("kind") or "event"),
                "text": text,
                "metadata": values,
                "images": list(images or []),
            },
            key=key,
        )

    def _enqueue_turn(
        self,
        turn_id: str,
        seq: int,
        phase: str,
        text: str,
        *,
        segment: int = 0,
        wake: str = "",
        wake_ids: str = "",
        images: list[dict[str, Any]] | None = None,
        client_message_id: str = "",
        ignored: bool = False,
        nighttalk: bool = False,
    ) -> None:
        metadata = {
            "schema": "akid.v1",
            "kind": "turn",
            "turnId": turn_id,
            "segment": segment,
            "seq": seq,
            "phase": phase,
            "format": "markdown",
        }
        if phase == "final":
            metadata["wake"] = wake or CHANNEL_WAKE_ALL
            if wake_ids:
                metadata["wakeIds"] = wake_ids
            if nighttalk:
                metadata["nighttalk"] = "1"
        if ignored:
            metadata["ignored"] = "true"
        self._enqueue_text(
            text,
            metadata,
            key=(turn_id, segment) if phase == "progress" else None,
            images=images,
            client_message_id=client_message_id,
        )

    async def _flush_progress(
        self,
        turn_id: str,
        seq: int,
        state: "ChannelTurnState",
        *,
        force: bool = False,
        phase: str = "progress",
    ) -> None:
        members = self.server._channel_member_views()
        raw_text = state.text.strip()
        if contains_no_reply_marker(raw_text):
            state.no_reply = True
            state.text = ""
            return
        cleaned_text, images = await self.server._extract_agent_channel_images(
            raw_text, load_data=phase == "final"
        )
        wake = ""
        wake_ids = ""
        if phase == "final":
            if self.server._solo_session is not None:
                text = strip_channel_mentions(cleaned_text, members)
                wake = CHANNEL_WAKE_USER
                wake_ids = ""
            else:
                route = route_channel_reply(
                    cleaned_text,
                    members,
                    user_name=self.server.config.username,
                )
                text = route.text.strip()
                mentioned = match_channel_mentions(text, members)
                blocked = {
                    member_id: self.server._solo_targets.get(member_id, {})
                    for member_id in mentioned
                    if member_id in self.server._solo_targets
                }
                if blocked:
                    target_ids = tuple(
                        member_id
                        for member_id in route.target_ids
                        if member_id not in blocked
                    )
                    if route.wake == CHANNEL_WAKE_MENTIONS and not target_ids:
                        route_wake = CHANNEL_WAKE_USER
                        route_target_ids: tuple[str, ...] = ()
                    else:
                        route_wake = route.wake
                        route_target_ids = target_ids
                    names = [
                        str(value.get("target_name") or member_id)
                        for member_id, value in blocked.items()
                    ]
                    self.server._enqueue_solo_notice(", ".join(names))
                else:
                    route_wake = route.wake
                    route_target_ids = route.target_ids
                wake = route_wake if text else CHANNEL_WAKE_USER
                wake_ids = ",".join(route_target_ids) if text else ""
            if not text and not images:
                return
            state.wake = wake
            state.wake_ids = wake_ids
        else:
            text, _excluded = strip_channel_non_wakes(cleaned_text, members)
            if self.server._solo_session is not None:
                text = strip_channel_mentions(text, members)
            if not text:
                return
        if not force and text == state.last_text:
            return
        self._enqueue_turn(
            turn_id,
            seq,
            phase,
            text,
            segment=state.segment,
            wake=wake,
            wake_ids=wake_ids,
            images=images,
            client_message_id=state.client_message_id,
            nighttalk=state.nighttalk,
        )
        state.last_text = text

    async def _publish_loop(self) -> None:
        while True:
            item = await self._queue.get()
            key = item.pop("_key", None)
            if key is not None:
                self._pending_progress.pop(key, None)
            channel = self.server.channel
            if channel is None:
                self._queue.task_done()
                continue
            try:
                if item.get("images"):
                    published = await channel.publish_message(
                        item["text"], item.get("images") or (), item["metadata"]
                    )
                else:
                    await channel.publish_text(item["text"], item["metadata"])
                    published = {}
                phase = str(item["metadata"].get("phase") or "")
                if item.get("kind") == "turn" and phase in ("progress", "final"):
                    attachments: list[ChannelImageAttachment] = []
                    for index, source in enumerate(item.get("images") or []):
                        stored = self.server._store_channel_image(
                            ImageEnvelope(
                                sender=self.server.config.channel_name,
                                data=bytes(source["data"]),
                                sender_id=self.server._channel_self_key(),
                                message_id=str(published.get("message_id") or ""),
                                name=str(source.get("name") or ""),
                                attachment_index=index,
                                attachment_count=len(item.get("images") or []),
                                sent_at=int(time.time() * 1000),
                                mime=str(source.get("mime") or ""),
                                transfer_id=str(
                                    (published.get("images") or [{}])[index].get("transfer_id")
                                    if index < len(published.get("images") or ())
                                    else ""
                                ),
                            ),
                            name=str(source.get("name") or f"图片{index + 1}"),
                            index=index + 1,
                        )
                        if stored is not None:
                            attachments.append(stored)
                    self.server._record_channel_message(
                        sender=self.server.config.channel_name,
                        sender_id=self.server._channel_self_key(),
                        text=str(item["text"]),
                        from_agent=True,
                        phase=phase,
                        attachments=tuple(attachments),
                    )
                    if phase == "final":
                        self.server._notify_layer(
                            self.server._layer_own_character(),
                            str(item["text"]),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "channel publish failed type=%s error=%s", item.get("kind"), exc
                )
            finally:
                self._queue.task_done()

    async def send(self, message: Mapping[str, Any]) -> None:
        if self.closed or message.get("method") != "aki.event":
            return
        params = message.get("params") or {}
        event_type = params.get("type")
        data = params.get("data") or {}
        if self.server.channel is None:
            return
        turn_id = str(params.get("turn_id") or "")
        seq_value = params.get("seq")
        seq = int(seq_value) if isinstance(seq_value, int) else 0
        if not turn_id:
            return
        state = self._turn_state(turn_id)
        if state.suppressed and event_type == "turn_end":
            self._turns.pop(turn_id, None)
            return
        if state.suppressed:
            return
        if state.guard_blocked:
            if event_type == "turn_end":
                self._enqueue_turn(
                    turn_id,
                    seq,
                    "ended",
                    "",
                    client_message_id=state.client_message_id,
                    ignored=True,
                )
                self._turns.pop(turn_id, None)
            return
        if event_type == "turn_started":
            state.client_message_id = str(data.get("client_message_id") or "")
            state.origin_is_agent = bool(data.get("origin_is_agent"))
            state.nighttalk = bool(data.get("nighttalk"))
            if (
                state.origin_is_agent
                and not state.nighttalk
                and not state.initiation_charged
            ):
                state.initiation_charged = self.server._claim_agent_initiation(turn_id)
                if not state.initiation_charged:
                    state.suppressed = True
                    return
            self._enqueue_turn(
                turn_id,
                seq,
                "started",
                "Aki is working…",
                client_message_id=state.client_message_id,
            )
            state.last_emit = asyncio.get_running_loop().time()
            return
        if event_type == "assistant_delta":
            if data.get("reasoning"):
                return
            delta = str(data.get("text") or "")
            if not delta:
                return
            state.text += delta
            return
        if event_type == "assistant_segment":
            await self._flush_progress(turn_id, seq, state, force=True)
            state.segment += 1
            state.text = ""
            state.last_text = ""
            state.finalized = False
            return
        if event_type == "assistant_message":
            full = str(data.get("content") or data.get("text") or "")
            if contains_no_reply_marker(full):
                state.no_reply = True
                state.text = ""
                return
            if state.segment == 0 and not state.text.strip() and full:
                state.text = full
            await self._flush_progress(turn_id, seq, state, force=True, phase="final")
            state.finalized = True
            return
        if event_type == "tool_call":
            await self._flush_progress(turn_id, seq, state, force=True)
            tool_name = str(data.get("name") or "unknown")
            tool_call_id = str(data.get("tool_call_id") or "")
            arguments = render_tool_value(data.get("arguments"))
            if tool_call_id:
                state.tool_names[tool_call_id] = tool_name
                state.tool_arguments[tool_call_id] = arguments
            self._enqueue_text(
                tool_detail_body(arguments=arguments, result=None),
                {
                    "schema": "akid.v1",
                    "kind": "tool",
                    "turnId": turn_id,
                    "seq": seq,
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "toolStatus": "running",
                },
                client_message_id=state.client_message_id,
            )
            state.segment += 1
            state.text = ""
            state.last_text = ""
            state.finalized = False
            return
        if event_type == "tool_result":
            status = str(data.get("status") or "success").lower()
            tool_call_id = str(data.get("tool_call_id") or "")
            tool_name = str(state.tool_names.get(tool_call_id, "tool"))
            arguments = state.tool_arguments.get(tool_call_id, "")
            self._enqueue_text(
                tool_detail_body(
                    arguments=arguments,
                    result=render_tool_result(data),
                ),
                {
                    "schema": "akid.v1",
                    "kind": "tool",
                    "turnId": turn_id,
                    "seq": seq,
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "toolStatus": "error" if status == "error" else "success",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "approval_request":
            await self._flush_progress(turn_id, seq, state, force=True)
            approval_id = str(data.get("approval_id") or "")
            tool_name = str(data.get("tool_name") or "unknown")
            tool_call_id = str(data.get("tool_call_id") or "")
            if tool_call_id:
                state.tool_names[tool_call_id] = tool_name
            try:
                rendered = json.dumps(data.get("input"), ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                rendered = str(data.get("input"))
            if len(rendered) > 2000:
                rendered = rendered[:2000] + "\n... (truncated)"
            timeout_s = data.get("timeout_s")
            lines = [
                "akid approval required",
                f"tool: {tool_name}",
                f"approval_id: {approval_id}",
            ]
            if rendered and rendered != "null":
                lines.extend(["input:", rendered])
            if timeout_s is not None:
                lines.append(f"timeout: {timeout_s}s")
            lines.extend([f"reply: akid-approve {approval_id}", f"       akid-deny {approval_id}"])
            self._enqueue_text(
                "\n".join(lines),
                {
                    "schema": "akid.v1",
                    "kind": "approval",
                    "turnId": turn_id,
                    "seq": seq,
                    "approvalId": approval_id,
                    "requesterId": str(data.get("requester_id") or ""),
                    "toolName": tool_name,
                    "toolCallId": tool_call_id,
                    "timeoutS": str(timeout_s if timeout_s is not None else ""),
                    "details": rendered if rendered and rendered != "null" else "",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "approval_resolved":
            approval_id = str(data.get("approval_id") or "")
            decision = str(data.get("decision") or "deny")
            tool_name = str(data.get("tool_name") or "tool")
            self._enqueue_text(
                f"akid: {decision} {approval_id} (tool={tool_name})",
                {
                    "schema": "akid.v1",
                    "kind": "approval_resolved",
                    "turnId": turn_id,
                    "seq": seq,
                    "approvalId": approval_id,
                    "toolName": tool_name,
                    "decision": decision,
                    "details": str(data.get("message") or ""),
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "error":
            text = str(data.get("message") or data.get("code") or "unknown error")
            self._enqueue_text(
                f"akid error: {text[:1000]}",
                {
                    "schema": "akid.v1",
                    "kind": "error",
                    "turnId": turn_id,
                    "seq": seq,
                    "phase": "ended",
                },
                client_message_id=state.client_message_id,
            )
            return
        if event_type == "turn_end":
            if not state.no_reply and not state.finalized:
                await self._flush_progress(
                    turn_id, seq, state, force=True, phase="final"
                )
            self._enqueue_turn(
                turn_id,
                seq,
                "no_reply" if state.no_reply else "ended",
                "",
                client_message_id=state.client_message_id,
            )
            self._turns.pop(turn_id, None)
            return

    def discard_turn_output(self, turn_id: str) -> None:
        """Drop buffered/public output for a turn rejected by the wait guard."""
        state = self._turn_state(turn_id)
        state.text = ""
        state.last_text = ""
        state.finalized = True
        state.guard_blocked = True
