"""Enter and leave solo mode for this member."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from engine.channel import TokifactorChannel
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_FAILED,
    SOLO_READY,
    SOLO_START,
    SoloSession,
    clear_solo_session,
    derive_solo_magic,
    load_solo_scene,
    now_iso as solo_now_iso,
    save_solo_session,
)
from engine.member_profile import MEMBER_KIND_USER
from engine.mqtt_settings import save_mqtt_server
from engine.protocol import (
    ErrorCode,
    RpcError,
    require_int,
    require_string,
)
from .constants import (
    CHANNEL_PROBE_RATE_LIMIT,
    CHANNEL_PROBE_SEEN_TTL,
    CHANNEL_PROTOCOL_VERSION,
    LOGGER,
)

class SoloMixin:

    def _solo_status(self) -> dict[str, Any]:
        session = self._solo_session
        return {
            "active": session is not None,
            "session_id": session.session_id if session else "",
            "target_id": session.target_id if session else "",
            "target_name": session.target_name if session else "",
            "started_at": session.started_at if session else "",
            "group_magic_session": self.config.mqtt_magic,
            "known_solo_targets": sorted(self._solo_targets),
            "group_transcript_frozen": session is not None,
            "solo_history_path": str(self.solo_channel_history_path),
            "memory": self.solo_memory.status(),
        }


    async def _cancel_mqtt_turns_for_solo(self) -> None:
        queued = [turn for turn in self.queue if turn.origin == "mqtt"]
        if queued:
            self.queue = [turn for turn in self.queue if turn.origin != "mqtt"]
            for turn in queued:
                turn.cancelled = True
                turn.end_sent = True
                turn.done_event.set()
        active = self.active
        if active is not None and active.origin == "mqtt" and not active.end_sent:
            await self._cancel_turn(active)


    async def _activate_solo(self, session: SoloSession) -> None:
        if session.target_id != self._channel_self_key():
            return
        if self._solo_session is not None:
            if self._solo_session.session_id == session.session_id:
                await self._publish_solo_ready(session)
            else:
                await self._publish_channel_control(
                    "solo busy",
                    {
                        **self._channel_identity_metadata(),
                        "kind": SOLO_FAILED,
                        "soloId": session.session_id,
                        "soloTargetId": session.target_id,
                        "soloError": "another solo session is already active",
                    },
                    qos=1,
                )
            return
        scene = load_solo_scene(
            self.config.profile,
            username=self.config.username,
            explicit=(
                self.config.aki.solo_prompt_file
                if self.config.aki is not None
                else ""
            ),
        )
        await self._cancel_mqtt_turns_for_solo()
        self._persist_channel_history()
        self._frozen_group_history = list(self._channel_history)
        self._frozen_group_history_seq = self._channel_history_seq
        self._solo_session = session
        self._solo_scene = scene
        save_solo_session(self.solo_state_path, session)
        self._solo_targets.pop(self._channel_self_key(), None)
        await self._publish_solo_ready(session)

        channel = self.channel
        if channel is not None:
            channel.set_active_magic(
                derive_solo_magic(self.config.mqtt_magic, self._channel_self_key())
            )
            channel.discard_inbox()
        self._load_channel_history(self.solo_channel_history_path)
        self._solo_memory_start_seq = int(self._channel_history_seq)
        await self._refresh_channel_transcript()
        await self._publish_channel_control(
            f"{self.config.channel_name} solo active",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_ACTIVE,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
                "soloStartedAt": session.started_at,
            },
            qos=1,
            retain=True,
        )
        LOGGER.info(
            "solo channel entered target=%s session=%s",
            session.target_name,
            session.session_id,
        )


    async def _publish_solo_ready(self, session: SoloSession) -> None:
        await self._publish_channel_control(
            f"{self.config.channel_name} solo ready",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_READY,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
                "soloStartedAt": session.started_at,
            },
            qos=1,
        )


    async def _deactivate_solo(self, *, session_id: str = "") -> None:
        session = self._solo_session
        if session is None:
            return
        if session_id and session_id != session.session_id:
            return
        solo_history = list(self._channel_history)
        await self._publish_channel_control(
            f"{self.config.channel_name} solo end",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_END,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
            },
            qos=1,
            retain=True,
        )
        self._persist_channel_history()
        self._solo_session = None
        self._solo_scene = ""
        clear_solo_session(self.solo_state_path)
        channel = self.channel
        if channel is not None:
            channel.set_active_magic(self.config.mqtt_magic)
            channel.discard_inbox()
        self._load_channel_history(self.group_channel_history_path)
        if not self._channel_history and self._frozen_group_history:
            self._channel_history = list(self._frozen_group_history)
            self._channel_history_seq = self._frozen_group_history_seq
        self._frozen_group_history = []
        self._frozen_group_history_seq = 0
        await self._finalize_solo_memory_session(session, solo_history)
        self._solo_memory_start_seq = int(self._channel_history_seq)
        await self._refresh_channel_transcript()
        await self._publish_channel_control(
            f"{self.config.channel_name} solo ended",
            {
                **self._channel_identity_metadata(),
                "kind": SOLO_END,
                "soloId": session.session_id,
                "soloTargetId": session.target_id,
                "soloTargetName": session.target_name,
            },
            qos=1,
            retain=True,
        )
        LOGGER.info("solo channel exited session=%s", session.session_id)


    async def _on_channel_control(self, envelope: TextEnvelope) -> None:
        metadata = envelope.metadata
        if metadata.get("schema") != "akid.v1":
            return
        kind = metadata.get("kind")
        if kind in (SOLO_START, SOLO_READY, SOLO_FAILED, SOLO_ACTIVE, SOLO_END):
            await self._on_solo_control(envelope)
            return
        if kind not in ("probe", "presence", "status", "offline"):
            return
        if metadata.get("protocolVersion") != str(CHANNEL_PROTOCOL_VERSION):
            return
        member_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
        if not member_id or member_id == self._channel_self_key():
            return
        if kind == "probe":
            probe_id = str(metadata.get("probeId") or "").strip()
            if not probe_id or not envelope.text.strip():
                return
            now = time.monotonic()
            cutoff = now - CHANNEL_PROBE_SEEN_TTL
            self._probe_seen = {
                key: value
                for key, value in self._probe_seen.items()
                if value >= cutoff
            }
            self._probe_rate = {
                key: value
                for key, value in self._probe_rate.items()
                if value >= cutoff
            }
            if probe_id in self._probe_seen:
                return
            sender_key = envelope.sender_id or envelope.sender or "unknown"
            if now - self._probe_rate.get(sender_key, float("-inf")) < CHANNEL_PROBE_RATE_LIMIT:
                return
            self._probe_seen[probe_id] = now
            self._probe_rate[sender_key] = now
            if member_id:
                self._update_channel_member(envelope, metadata)
            await self._publish_channel_presence(probe_id)
            return
        if not member_id:
            return
        if kind == "offline":
            source_id = (
                str(metadata.get("sourceId") or "").strip()
                or envelope.sender_id.strip()
                or member_id
            )
            self._drop_channel_member_source(member_id, source_id)
            return
        self._update_channel_member(envelope, metadata)
        if kind in ("presence", "status"):
            self._observe_channel_waiting_user(member_id, metadata)
        if kind == "status":
            self._observe_channel_status(
                member_id, metadata, sent_at=envelope.sent_at
            )


    async def _on_solo_control(self, envelope: TextEnvelope) -> None:
        metadata = envelope.metadata
        if metadata.get("protocolVersion") != str(CHANNEL_PROTOCOL_VERSION):
            return
        kind = str(metadata.get("kind") or "")
        session_id = str(metadata.get("soloId") or "").strip()
        target_id = str(metadata.get("soloTargetId") or "").strip()
        target_name = str(metadata.get("soloTargetName") or "").strip()
        if not target_id:
            return
        if kind == SOLO_START:
            if not session_id:
                return
            if metadata.get("memberKind") != MEMBER_KIND_USER:
                return
            sender_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
            if not sender_id or sender_id == self._channel_self_key():
                return
            if target_id == self._channel_self_key():
                session = SoloSession(
                    session_id=session_id,
                    target_id=target_id,
                    target_name=target_name or self.config.channel_name,
                    profile=self.config.profile,
                    started_at=str(metadata.get("soloStartedAt") or solo_now_iso()),
                    group_magic=self.config.mqtt_magic,
                )
                await self._activate_solo(session)
            else:
                self._solo_targets[target_id] = {
                    "session_id": session_id,
                    "target_name": target_name or target_id,
                    "started_at": str(metadata.get("soloStartedAt") or ""),
                }
            return
        if kind == SOLO_END:
            self._solo_targets.pop(target_id, None)
            if self._solo_session is not None and (
                not target_id or target_id == self._channel_self_key()
            ):
                await self._deactivate_solo(session_id=session_id)
            return
        if kind == SOLO_ACTIVE:
            return
        if kind == SOLO_READY:
            if target_id == self._channel_self_key():
                await self._publish_solo_ready(
                    self._solo_session
                    or SoloSession(
                        session_id=session_id,
                        target_id=target_id,
                        target_name=target_name or self.config.channel_name,
                        profile=self.config.profile,
                        started_at=str(metadata.get("soloStartedAt") or ""),
                        group_magic=self.config.mqtt_magic,
                    )
                )
            return
        if kind == SOLO_FAILED:
            return


    async def _mqtt_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        host = require_string(params, "host")
        host = host.strip()
        if not host or len(host) > 255 or any(char.isspace() for char in host):
            raise RpcError(ErrorCode.INVALID_PARAMS, "host must be a valid MQTT hostname or IP")
        port = require_int(params, "port", minimum=1, maximum=65535)
        async with self._mqtt_lock:
            if self.channel is None:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    "MQTT channel is disabled; set [mqtt] enabled = true in ~/.aki/aki.conf",
                )
            try:
                save_mqtt_server(host, port, self.mqtt_state_path)
            except OSError as exc:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR, f"cannot save MQTT settings: {exc}"
                ) from exc
            for approval in list(self.approvals.values()):
                if approval.turn.origin == "mqtt" and not approval.future.done():
                    approval.future.set_result((False, "MQTT server changed"))
            old = self.channel
            with contextlib.suppress(Exception):
                await old.stop()
            self.config.mqtt_host = host
            self.config.mqtt_port = port
            self._load_channel_history()
            active_magic = (
                derive_solo_magic(self.config.mqtt_magic, self._channel_self_key())
                if self._solo_session is not None
                else self.config.mqtt_magic
            )
            new_channel = TokifactorChannel(
                host=host,
                port=port,
                magic=active_magic,
                session_magic=self.config.mqtt_magic,
                topic=self.config.mqtt_topic,
                sender_name=self.config.mqtt_sender_name,
                sender_id=self.config.mqtt_sender_id,
                dedup_path=self.channel_history_path.with_suffix(".seen.json"),
                avatar_cache_path=self.channel_history_path.with_suffix(".avatars"),
                on_text=self._on_channel_text,
                on_image=self._on_channel_image,
                on_control=self._on_channel_control,
                on_connected=self._on_channel_connected,
                logger=logging.getLogger("akid.channel"),
            )
            self.channel = new_channel
            await new_channel.start()
            deadline = time.monotonic() + 10.0
            while not new_channel.connected and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            result = self._mqtt_status()
            result["persisted"] = True
            LOGGER.info(
                "MQTT broker changed host=%s port=%s connected=%s",
                host,
                port,
                new_channel.connected,
            )
            return result
