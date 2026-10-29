"""Read and rename this member's channel identity."""

from __future__ import annotations

import contextlib
from engine.aki_config import sync_channel_identity
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    truncate_member_description,
)
from engine.member_identity import (
    IdentityConflict,
    IdentityError,
    MemberIdentity,
    canonical_name,
    write_manifest,
)
from engine.protocol import (
    ErrorCode,
    RpcError,
    require_string,
)

class IdentityMixin:

    # ---------------------------------------------------------------- identity
    def _identity_get(self) -> dict[str, Any]:
        registry_identity = (
            self._identity_registry.get(self._channel_self_key())
            if self._identity_registry is not None
            else None
        )
        identity = MemberIdentity(
            id=self._channel_self_key(),
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            kind=MEMBER_KIND_AGENT,
        )
        return {
            "id": identity.id,
            "name": identity.name,
            "aliases": list(identity.aliases),
            "kind": identity.kind,
            "created_at": registry_identity.created_at if registry_identity else "",
            "description": truncate_member_description(
                self.config.channel_description
            ),
            "profile": self.config.profile,
            "agent_id": self.config.agent_id,
            "userdata": str(self._userdata_dir()),
            "manifest": (
                str(self._identity_manifest_path)
                if self._identity_manifest_path is not None
                else str(self._userdata_dir() / "identity.json")
            ),
            "registry": (
                str(self._identity_registry.path)
                if self._identity_registry is not None
                else None
            ),
            "authority": "client-local",
        }


    async def _identity_rename(self, params: Mapping[str, Any]) -> dict[str, Any]:
        name = require_string(params, "name").strip()
        if not name:
            raise RpcError(ErrorCode.INVALID_PARAMS, "name must not be empty")
        if len(name) > 80:
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "name must be at most 80 characters"
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise RpcError(
                ErrorCode.INVALID_PARAMS, "name contains control characters"
            )
        current_id = self._channel_self_key()
        current = MemberIdentity(
            id=current_id,
            name=self.config.channel_name,
            aliases=self.config.channel_aliases,
            kind=MEMBER_KIND_AGENT,
        )
        renamed = current.renamed(name)
        target_key = canonical_name(renamed.name)
        for member_id, member in self._channel_members.items():
            if member_id == current_id:
                continue
            labels = {
                canonical_name(member.get("name")),
                *(canonical_name(alias) for alias in member.get("aliases") or ()),
            }
            if target_key in labels:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    f"name {name!r} already belongs to another member",
                    {"owner": member_id},
                )
        if self._identity_registry is not None:
            try:
                renamed = self._identity_registry.rename(current_id, renamed.name)
            except IdentityConflict as exc:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS, str(exc)
                ) from exc
            except (IdentityError, OSError) as exc:
                raise RpcError(ErrorCode.INTERNAL_ERROR, str(exc)) from exc
        if self._identity_manifest_path is not None:
            try:
                write_manifest(
                    self._identity_manifest_path,
                    renamed,
                    profile=self.config.profile,
                    agent_id=self.config.agent_id,
                    userdata=self.config.aki.userdata
                    if self.config.aki is not None
                    else str(self._userdata_dir()),
                )
            except (IdentityError, OSError) as exc:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR, f"cannot write identity manifest: {exc}"
                ) from exc
        config_path = self._profile_config_path()
        try:
            sync_channel_identity(
                config_path,
                member_id=renamed.id,
                name=renamed.name,
                aliases=renamed.aliases,
            )
        except OSError as exc:
            raise RpcError(
                ErrorCode.INTERNAL_ERROR, f"cannot mirror identity into config: {exc}"
            ) from exc
        self.config.channel_name = renamed.name
        self.config.channel_aliases = renamed.aliases
        self.config.channel_member_id = renamed.id
        self.config.channel_id = renamed.id
        self.config.mqtt_sender_name = renamed.name
        self.config.mqtt_sender_id = renamed.id
        if self.channel is not None:
            self.channel.sender_name = renamed.name
            self.channel.sender_id = renamed.id
        if self.channel is not None and self.channel.connected:
            with contextlib.suppress(Exception):
                await self._publish_channel_status()
        return self._identity_get()
