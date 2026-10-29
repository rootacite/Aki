"""Track channel members and who is waiting."""

from __future__ import annotations

import time
from dataclasses import replace
from engine.live_context import ChannelMember
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    member_name_key,
    normalize_member_kind,
    truncate_member_description,
)
from engine.member_identity import (
    MemberIdentity,
    is_member_id,
    legacy_member_id,
)
from .constants import (
    CHANNEL_MEMBER_TIMEOUT,
    LOGGER,
)
from .types import AgentChannelState

class MembersMixin:

    def _member_key(
        self,
        metadata: Mapping[str, Any],
        sender_name: str = "",
        sender_id: str = "",
    ) -> str:
        explicit = str(metadata.get("memberId") or "").strip()
        if is_member_id(explicit):
            return explicit
        if is_member_id(sender_id):
            return sender_id.strip()
        name = (
            str(metadata.get("agentName") or "").strip()
            or sender_name.strip()
            or str(metadata.get("agentId") or metadata.get("agent") or "").strip()
            or sender_id.strip()
        )
        known = self._resolve_label_member_id(name)
        if known:
            return known
        return legacy_member_id(name) if name else ""


    def _resolve_label_member_id(self, label: object) -> str:
        if self._identity_registry is not None:
            owner = self._identity_registry.resolve(label)
            if owner:
                return owner
        key = member_name_key(label)
        if not key:
            return ""
        for member_id, member in self._channel_members.items():
            aliases = {
                member_name_key(member.get("name")),
                *(member_name_key(alias) for alias in member.get("aliases") or ()),
            }
            if key in aliases:
                return member_id
        return ""


    def _update_channel_member(
        self,
        envelope: TextEnvelope,
        metadata: Mapping[str, Any],
    ) -> str:
        member_id = self._member_key(metadata, envelope.sender, envelope.sender_id)
        if not member_id or member_id == self._channel_self_key():
            return member_id
        now = time.monotonic()
        source_id = (
            str(metadata.get("sourceId") or "").strip()
            or envelope.sender_id.strip()
            or member_id
        )
        previous = self._channel_members.get(member_id)
        sources = dict((previous or {}).get("sources") or {})
        if previous and not sources:
            previous_source = str(previous.get("source_id") or member_id)
            sources[previous_source] = float(previous.get("last_seen", 0.0))
        sources[source_id] = now
        owner = str((previous or {}).get("source_id") or source_id)
        update_profile = source_id == owner
        name = str(
            (metadata.get("agentName") if update_profile else "")
            or (envelope.sender if update_profile else "")
            or (previous or {}).get("name")
            or member_id
        ).strip()
        if update_profile and "aliases" in metadata:
            aliases = tuple(
                item.strip()
                for item in str(metadata.get("aliases") or "").split(",")
                if item.strip()
            )
        else:
            aliases = tuple((previous or {}).get("aliases") or ())
        description = (
            truncate_member_description(metadata.get("description"))
            if update_profile and "description" in metadata
            else str((previous or {}).get("description") or "")
        )
        member_kind = (
            normalize_member_kind(metadata.get("memberKind"))
            if update_profile and "memberKind" in metadata
            else normalize_member_kind((previous or {}).get("kind"))
        )
        claimed = True
        if self._identity_registry is not None:
            identity = MemberIdentity(
                id=member_id,
                name=name,
                aliases=aliases,
                kind=member_kind,
            )
            claimed, conflicts = self._identity_registry.observe(
                identity, source=source_id
            )
            for conflict in conflicts:
                LOGGER.warning(
                    "remote identity label conflict label=%s owner=%s member=%s source=%s",
                    conflict.get("label"),
                    conflict.get("owner"),
                    conflict.get("member_id"),
                    source_id,
                )
            if not claimed:
                # The existing owner keeps its labels.  Keep the conflicting id
                # online, but do not let it capture mention routing.
                aliases = tuple((previous or {}).get("aliases") or ())
                if previous:
                    name = str(previous.get("name") or member_id)
                else:
                    name = member_id
        self._channel_members[member_id] = {
            "id": member_id,
            "name": name[:80] or member_id,
            "aliases": aliases[:16],
            "description": description,
            "kind": member_kind,
            "labelsClaimed": claimed,
            "source_id": owner,
            "sources": sources,
            "last_seen": now,
        }
        if member_kind == MEMBER_KIND_USER:
            self._normalize_channel_history_kind(member_id, member_kind)
        return member_id


    def _next_channel_event_seq(self) -> int:
        self._channel_event_seq += 1
        return self._channel_event_seq


    def _agent_channel_state(self, member_id: str) -> AgentChannelState:
        state = self._channel_agent_states.get(member_id)
        if state is None:
            state = AgentChannelState()
            self._channel_agent_states[member_id] = state
        return state


    def _observe_channel_turn_event(
        self,
        member_id: str,
        metadata: Mapping[str, Any],
        text: str,
        *,
        sent_at: int = 0,
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        if metadata.get("schema") != "akid.v1" or metadata.get("kind") != "turn":
            return
        phase = str(metadata.get("phase") or "").strip()
        if phase not in ("started", "final", "ended", "no_reply"):
            return
        state = self._agent_channel_state(member_id)
        seq = self._next_channel_event_seq()
        turn_id = str(metadata.get("turnId") or "")
        timestamp = sent_at if sent_at > 0 else int(time.time() * 1000)
        if phase == "started":
            state.active = True
            state.active_turn_id = turn_id
            state.last_final_text = ""
            state.last_final_turn_id = ""
            state.last_turn_event_sent_at = timestamp
            return
        if phase == "final":
            state.last_final_text = text
            state.last_final_turn_id = turn_id
            state.last_turn_event_sent_at = timestamp
            return
        state.active = False
        state.active_turn_id = ""
        state.queue_depth = 0
        state.last_completion_seq = seq
        state.last_completion_phase = phase
        state.last_completion_sent_at = timestamp
        state.last_turn_event_sent_at = timestamp
        if phase == "no_reply":
            state.last_final_text = ""
            state.last_final_turn_id = ""


    def _observe_channel_status(
        self,
        member_id: str,
        metadata: Mapping[str, Any],
        *,
        sent_at: int = 0,
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        state = self._agent_channel_state(member_id)
        timestamp = sent_at if sent_at > 0 else int(time.time() * 1000)
        if timestamp < max(
            state.last_completion_sent_at, state.last_turn_event_sent_at
        ):
            return
        try:
            queue_depth = max(0, int(metadata.get("queueDepth") or 0))
        except (TypeError, ValueError):
            queue_depth = 0
        state.queue_depth = queue_depth
        state.active = str(metadata.get("activeTurn") or "").lower() == "true"
        state.last_status_seq = self._next_channel_event_seq()
        state.last_status_sent_at = timestamp
        if not state.active and queue_depth == 0:
            state.active_turn_id = ""


    def _observe_channel_waiting_user(
        self, member_id: str, metadata: Mapping[str, Any]
    ) -> None:
        if not member_id or member_id == self._channel_self_key():
            return
        value = metadata.get("waitingUser")
        if value is None:
            return
        waiting = str(value).strip().lower() in ("true", "1", "yes", "on")
        if waiting:
            self._channel_waiting_user[member_id] = time.monotonic()
        else:
            self._channel_waiting_user.pop(member_id, None)


    def _channel_waiting_user_ids(self) -> set[str]:
        cutoff = time.monotonic() - CHANNEL_MEMBER_TIMEOUT
        self_key = self._channel_self_key()
        self._channel_waiting_user = {
            member_id: seen_at
            for member_id, seen_at in self._channel_waiting_user.items()
            if seen_at >= cutoff
        }
        waiting = set(self._channel_waiting_user)
        if self._wait_user_active:
            waiting.add(self_key)
        return waiting


    async def _set_wait_user_active(self, active: bool) -> None:
        if self._wait_user_active == active:
            return
        self._wait_user_active = active
        self._schedule_status_refresh()
        channel = self.channel
        if channel is None or not channel.connected:
            return
        metadata = {
            **self._channel_live_metadata(),
            "kind": "wait_user",
            "waitingUser": "true" if active else "false",
        }
        try:
            await channel.publish_text(
                "waiting for user" if active else "stopped waiting for user",
                metadata,
            )
        except Exception as exc:
            LOGGER.warning("failed to publish wait_user state active=%s: %s", active, exc)


    async def _set_wait_other_active(
        self, active: bool, targets: tuple[str, ...] = ()
    ) -> None:
        normalized = tuple(label.strip() for label in targets if label.strip())
        if self._wait_other_active == active and self._wait_other_targets == normalized:
            return
        self._wait_other_active = active
        self._wait_other_targets = normalized if active else ()
        self._schedule_status_refresh()
        channel = self.channel
        if channel is None or not channel.connected:
            return
        metadata = {
            **self._channel_live_metadata(),
            "kind": "wait_other",
            "waitingOther": "true" if active else "false",
            "waitingOtherTargets": ",".join(self._wait_other_targets),
        }
        try:
            await channel.publish_text(
                "waiting for other agents" if active else "stopped waiting for other agents",
                metadata,
            )
        except Exception as exc:
            LOGGER.warning("failed to publish wait_other state active=%s: %s", active, exc)


    def _mark_channel_member_offline(self, member_id: str) -> None:
        self._channel_waiting_user.pop(member_id, None)
        state = self._channel_agent_states.get(member_id)
        if state is None:
            return
        state.active = False
        state.active_turn_id = ""
        state.queue_depth = 0
        state.last_completion_seq = self._next_channel_event_seq()
        state.last_completion_phase = "offline"
        state.last_completion_sent_at = int(time.time() * 1000)


    def _normalize_channel_history_kind(self, member_id: str, member_kind: str) -> None:
        if member_kind != MEMBER_KIND_USER:
            return
        changed = False
        normalized: list[ChannelTranscriptEntry] = []
        for entry in self._channel_history:
            resolved = self._history_member_id(entry.sender, entry.sender_id)
            if resolved == member_id and (entry.from_agent or entry.sender_id != member_id):
                entry = replace(
                    entry,
                    sender_id=member_id,
                    from_agent=False if entry.from_agent else entry.from_agent,
                )
                changed = True
            normalized.append(entry)
        if not changed:
            return
        self._channel_history = normalized
        self._persist_channel_history()


    def _drop_channel_member_source(self, member_id: str, source_id: str) -> None:
        member = self._channel_members.get(member_id)
        if member is None:
            return
        sources = dict(member.get("sources") or {})
        sources.pop(source_id, None)
        if not sources:
            self._channel_members.pop(member_id, None)
            self._channel_waiting_user.pop(member_id, None)
            self._mark_channel_member_offline(member_id)
            return
        updated = dict(member)
        updated["sources"] = sources
        updated["last_seen"] = max(sources.values())
        self._channel_members[member_id] = updated


    def _prune_channel_members(self) -> None:
        cutoff = time.monotonic() - CHANNEL_MEMBER_TIMEOUT
        kept: dict[str, dict[str, Any]] = {}
        for key, member in self._channel_members.items():
            sources = dict(member.get("sources") or {})
            if not sources:
                if float(member.get("last_seen", 0.0)) >= cutoff:
                    kept[key] = member
                continue
            active = {
                source_id: seen_at
                for source_id, seen_at in sources.items()
                if float(seen_at) >= cutoff
            }
            if not active:
                continue
            updated = dict(member)
            updated["sources"] = active
            updated["last_seen"] = max(active.values())
            kept[key] = updated
        self._channel_members = kept
        self._channel_waiting_user = {
            member_id: seen_at
            for member_id, seen_at in self._channel_waiting_user.items()
            if seen_at >= cutoff
        }


    def _channel_self_key(self) -> str:
        return self.config.channel_member_id or legacy_member_id(
            self.config.channel_name
        )


    def _channel_self_member(self) -> ChannelMember:
        return ChannelMember(
            id=self._channel_self_key(),
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            description=truncate_member_description(self.config.channel_description),
            is_self=True,
            kind=MEMBER_KIND_AGENT,
        )


    def _history_member_id(self, sender: str, sender_id: str = "") -> str:
        candidate = str(sender_id or "").strip()
        if is_member_id(candidate):
            return candidate
        for label in (sender, candidate):
            owner = self._resolve_label_member_id(label)
            if owner:
                return owner
        name = str(sender or candidate).strip()
        return legacy_member_id(name) if name else ""


    def _member_display_name(self, member_id: str, fallback: str = "") -> str:
        if member_id and member_id == self._channel_self_key():
            return self.config.channel_name
        member = self._channel_members.get(member_id) if member_id else None
        if member is not None:
            return str(member.get("name") or fallback)
        if self._identity_registry is not None and member_id:
            identity = self._identity_registry.get(member_id)
            if identity is not None:
                return identity.name
        return str(fallback or "")


    def _channel_member_views(self) -> list[ChannelMember]:
        self._prune_channel_members()
        views = [self._channel_self_member()]
        for member in self._channel_members.values():
            views.append(
                ChannelMember(
                    id=str(member["id"]),
                    name=str(member["name"]),
                    aliases=tuple(member.get("aliases") or ()),
                    description=str(member.get("description") or ""),
                    kind=normalize_member_kind(member.get("kind")),
                )
            )
        return views
