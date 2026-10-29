"""JSON-RPC dispatch and health snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from engine.aki_config import (
    discover_profiles,
    read_mcp_excluded,
    write_mcp_excluded,
)
from engine.letta_llm import prepare_letta
from engine.live_context import (
    desktop_environment,
    render_channel_identity,
    render_channel_policy,
    render_channel_wake_routing,
)
from engine.tool_ownership import (
    EXCLUSIVE_TOOL_NAMES,
    render_tool_ownership_prompt,
    tools_for_profile,
)
from engine.protocol import (
    PROTOCOL_VERSION,
    SERVER_VERSION,
    ErrorCode,
    RpcError,
    optional_string,
    require_string,
)
from .constants import (
    CAMERA_TOOL_NAME,
    CAPABILITIES,
    IMAGE_GENERATION_TOOL_NAME,
    LOGGER,
    SCREENSHOT_TOOL_NAME,
    TOKIFACTOR_IMAGE_TOOL_NAME,
)
from .paths import (
    _is_video_device,
    default_camera_dir,
    default_screenshot_dir,
)

from .access import pkg

class HealthMixin:

    # --------------------------------------------------------------- dispatch
    async def _dispatch(
        self, conn: Connection, request: Request
    ) -> tuple[Any, Callable[[], Any] | None]:
        method = request.method
        if method == "aki.health":
            return self._health(), None
        if method == "aki.hello":
            return self._hello(conn, request.params), None

        if not conn.handshaken:
            raise RpcError(ErrorCode.INVALID_REQUEST, "aki.hello is required first")

        if method == "aki.status":
            return await self._status(), None
        if method == "aki.conversation.reset":
            return await self._reset_conversation(), None
        if method == "aki.username.get":
            return self._username_get(), None
        if method == "aki.username.set":
            return await self._username_set(request.params), None
        if method == "aki.identity.get":
            return self._identity_get(), None
        if method == "aki.identity.rename":
            return await self._identity_rename(request.params), None
        if method == "aki.conversation.recompile":
            return await self._recompile_conversation(), None
        if method == "aki.conversation.compact":
            return await self._compact_conversation(request.params), None
        if method == "aki.approval.respond":
            return self._approval_respond(conn, request.params), None
        if method == "aki.memory.status":
            return await self._memory_status(), None
        if method == "aki.memory.tokens":
            return await self._memory_tokens(), None
        if method == "aki.memory.tree":
            return self._memory_tree(request.params), None
        if method == "aki.memory.read":
            return self._memory_read(request.params), None
        if method == "aki.memory.search":
            return self._memory_search(request.params), None
        if method == "aki.fs.writable.list":
            return self._writable_status(), None
        if method == "aki.fs.writable.add":
            return await self._writable_add(request.params), None
        if method == "aki.fs.writable.remove":
            return await self._writable_remove(request.params), None
        if method == "aki.fs.writable.clear":
            return await self._writable_clear(), None
        if method == "aki.mqtt.status":
            return self._mqtt_status(), None
        if method == "aki.mqtt.set":
            return await self._mqtt_set(request.params), None
        if method == "aki.mcp.status":
            return self._mcp_native_status(), None
        if method == "aki.mcp.set":
            return await self._mcp_native_set(request.params), None
        if method == "aki.avatar.status":
            return self._avatar_health(), None
        if method == "aki.reasoning.get":
            return self._reasoning_status(), None
        if method == "aki.reasoning.set":
            return await self._reasoning_set(request.params), None
        if method == "aki.model.get":
            return self._model_status(), None
        if method == "aki.model.set":
            return await self._model_set(request.params), None
        if method == "aki.admin.zero_initiation_budget":
            return self._zero_initiation_budget(request.params), None
        raise RpcError(ErrorCode.METHOD_NOT_FOUND, f"unknown method: {method}")


    def _zero_initiation_budget(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        source = require_string(params, "source_profile").strip()
        if source != "aki":
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "only source_profile 'aki' may zero reply budgets",
            )
        if source == self.config.profile:
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "BUDGET_SELF_FORBIDDEN: Aki cannot zero her own reply budget",
            )
        previous = self._agent_initiation_remaining
        self._agent_initiation_remaining = 0
        reason = optional_string(params, "reason") or ""
        LOGGER.info(
            "reply budget zeroed by %s previous=%s target=%s reason=%s",
            source,
            previous,
            self.config.profile,
            reason[:200],
        )
        return {
            "target_profile": self.config.profile,
            "previous": previous,
            "remaining": self._agent_initiation_remaining,
            "reset_on": "next-user-message",
        }


    def _hello(self, conn: Connection, params: Mapping[str, Any]) -> dict[str, Any]:
        version = params.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise RpcError(ErrorCode.INVALID_PARAMS, "protocol_version must be an integer")
        if version != PROTOCOL_VERSION:
            raise RpcError(
                ErrorCode.PROTOCOL_VERSION,
                f"unsupported protocol_version {version}",
                {"supported": PROTOCOL_VERSION},
            )
        conn.client_name = require_string(params, "client_name")
        conn.client_version = require_string(params, "client_version")
        conn.handshaken = True
        capabilities = dict(CAPABILITIES)
        allowed = {tool["name"] for tool in tools_for_profile(self.config.profile)}
        capabilities["exclusive_tools"] = bool(allowed)
        capabilities["schedule_tools"] = {
            "aki_schedule_create",
            "aki_schedule_update",
            "aki_schedule_delete",
            "aki_schedule_list",
        }.issubset(allowed)
        capabilities["life_ledger"] = "aki_ledger_add" in allowed
        capabilities["proactive_triggers"] = self.config.profile == "aki"
        return {
            "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "agent_id": self.config.agent_id,
            "conversation_id": self.config.conversation_id,
            "capabilities": capabilities,
        }


    def _backend_health(self) -> dict[str, Any]:
        return {
            "state": getattr(self.backend, "state", "unknown"),
            "ready": bool(getattr(self.backend, "ready", False)),
            "pid": getattr(self.backend, "pid", None),
            "restart_count": getattr(self.backend, "restart_count", 0),
            "last_error": getattr(self.backend, "last_error", None),
            "last_ready_at": getattr(self.backend, "last_ready_at", None),
            "letta_bin": self.config.letta_bin,
            "aki_bridge": getattr(self.backend, "aki_bridge", {}),
            "aki_bridge_ready": bool(
                getattr(self.backend, "supports_aki_bridge", False)
            ),
        }


    def _channel_health(self) -> dict[str, Any]:
        if self.channel is None:
            return {
                "enabled": False,
                "state": "disabled",
                "connected": False,
                "host": self.config.mqtt_host,
                "port": self.config.mqtt_port,
                "topic": self.config.mqtt_topic,
                "wait_for": list(self.config.channel_wait_for),
                "transcript": dict(self._channel_transcript_status),
            }
        health = self.channel.health()
        health["wait_for"] = list(self.config.channel_wait_for)
        health["transcript"] = dict(self._channel_transcript_status)
        return health


    def _sandbox_health(self) -> dict[str, Any]:
        return self._writable_status()


    def _load_mcp_native_state(self) -> None:
        path = self.mcp_native_state_path
        try:
            excluded = read_mcp_excluded(path)
        except (OSError, ValueError) as exc:
            LOGGER.warning("invalid MCP native state file: %s (%s)", path, exc)
            return
        if excluded is None:
            return
        self._mcp_native_excluded = set(excluded)


    def _save_mcp_native_state(self) -> None:
        write_mcp_excluded(
            self.mcp_native_state_path, sorted(self._mcp_native_excluded)
        )


    def _prepare_letta_provider(self) -> None:
        """Prepare Letta provider metadata without owning the proxy process."""
        aki = self.config.aki
        if aki is None:
            return
        letta_home = self.config.letta_home or self.writable.letta_home
        self.config.aki = prepare_letta(
            aki,
            backend_dir=self.config.backend_dir,
            letta_home=letta_home,
        )


    def _mcp_native_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.mcp_native_tools_enabled,
            "allowed_servers": list(self.config.mcp_native_servers),
            "excluded_servers": sorted(self._mcp_native_excluded),
            "active_servers": list(self._mcp_native_active_servers),
            "tool_count": len(self._mcp_tools),
            "tools": [tool["name"] for tool in self._mcp_tools],
            "registered": self._external_tools_registered,
            "pending": self._external_tools_pending,
            "error": self._mcp_tools_error,
            "state_path": str(self.mcp_native_state_path),
        }


    def _external_tools_health(self) -> dict[str, Any]:
        tools = [tool["name"] for tool in self._external_tools()]
        return {
            "registered": self._external_tools_registered,
            "error": self._external_tools_error,
            "tools": tools,
            "exclusive_tools": [
                tool["name"] for tool in tools_for_profile(self.config.profile)
            ],
            "mcp_native_enabled": self.config.mcp_native_tools_enabled,
            "mcp_native_tool_count": len(self._mcp_tools),
            "mcp_native_error": self._mcp_tools_error,
            "mcp_native_excluded": sorted(self._mcp_native_excluded),
            "mcp_native_servers": list(self._mcp_native_active_servers),
            "require_approval": {
                TOKIFACTOR_IMAGE_TOOL_NAME: self.config.tokifactor_image_require_approval,
                CAMERA_TOOL_NAME: self.config.camera_require_approval,
                SCREENSHOT_TOOL_NAME: self.config.screenshot_require_approval,
                IMAGE_GENERATION_TOOL_NAME: bool(
                    self.config.image_generation
                    and self.config.image_generation.require_approval
                ),
            },
            "camera": self._camera_health(),
            "screenshot": self._screenshot_health(),
            "image_generation": self._image_generation_health(),
        }


    def _image_generation_health(self) -> dict[str, Any]:
        image = self.config.image_generation
        error: str | None = None
        if image is None:
            error = "image generation is not configured"
        elif not image.enabled:
            error = "image generation tool is disabled"
        elif not image.api_key:
            error = "image generation API key is empty"
        return {
            "enabled": bool(image and image.enabled),
            "available": bool(image and image.available),
            "provider": "minimax" if image is not None else None,
            "model": image.model if image is not None else None,
            "endpoint": image.path if image is not None else None,
            "output_dir": image.output_dir if image is not None else None,
            "require_approval": bool(image and image.require_approval),
            "error": error,
        }


    def _tool_ownership_status(self) -> dict[str, Any]:
        allowed = sorted(
            tool["name"] for tool in tools_for_profile(self.config.profile)
        )
        return {
            "profile": self.config.profile,
            "allowed": allowed,
            "blocked": sorted(EXCLUSIVE_TOOL_NAMES - set(allowed)),
            "blocked_count": len(EXCLUSIVE_TOOL_NAMES - set(allowed)),
        }


    def _tool_policy_status(self) -> dict[str, Any]:
        policy = self.config.tool_policy
        return {
            "profile": self.config.profile,
            "explicit": policy.explicit,
            "toolset": policy.toolset,
            "allow": list(policy.allow),
            "deny": list(policy.deny),
            "write_roots": list(policy.write_roots),
        }


    def _camera_health(self) -> dict[str, Any]:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        device = Path(self.config.camera_device).expanduser()
        error: str | None = None
        if not self.config.camera_enabled:
            error = "camera tool is disabled"
        elif not ffmpeg:
            error = f"ffmpeg is unavailable: {self.config.ffmpeg_bin}"
        else:
            try:
                resolved = device.resolve(strict=True)
            except OSError:
                error = f"camera device is unavailable: {device}"
            else:
                if not pkg()._is_video_device(resolved):
                    error = "camera device is not a V4L2 /dev/videoN character device"
                else:
                    device = resolved
        return {
            "enabled": self.config.camera_enabled,
            "available": error is None,
            "device": str(device),
            "ffmpeg": ffmpeg,
            "ffprobe": shutil.which(self.config.ffprobe_bin) if self.config.ffprobe_bin else None,
            "output_dir": str(
                self.config.camera_output_dir or default_camera_dir()
            ),
            "require_approval": self.config.camera_require_approval,
            "error": error,
        }


    def _wayland_display_candidates(self) -> list[str]:
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        ).expanduser()
        candidates: list[str] = []
        configured = self.config.screenshot_wayland_display or os.environ.get(
            "WAYLAND_DISPLAY"
        )
        if configured:
            candidates.append(configured)
        try:
            sockets = [
                path.name
                for path in runtime.glob("wayland-*")
                if path.is_socket()
            ]
        except OSError:
            sockets = []
        def socket_mtime(name: str) -> float:
            try:
                return (runtime / name).stat().st_mtime
            except OSError:
                return 0.0

        sockets.sort(key=socket_mtime, reverse=True)
        for name in sockets:
            if name not in candidates:
                candidates.append(name)
        return candidates


    def _desktop_is_kde(self) -> bool:
        now = time.monotonic()
        if now - self._kde_detected_at < 30.0:
            return self._kde_detected
        desktop = " ".join(
            value
            for value in (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_DESKTOP", ""),
                os.environ.get("DESKTOP_SESSION", ""),
            )
            if value
        ).lower()
        detected = "kde" in desktop or "plasma" in desktop or os.environ.get(
            "KDE_FULL_SESSION", ""
        ).lower() in ("1", "true")
        if not detected and not desktop:
            qdbus = self.config.qdbus_bin or shutil.which("qdbus") or shutil.which(
                "qdbus6"
            )
            if qdbus:
                try:
                    result = subprocess.run(
                        [qdbus, "org.kde.KWin", "/Scripting"],
                        env=desktop_environment(),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1.0,
                    )
                    detected = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    detected = False
        self._kde_detected_at = now
        self._kde_detected = detected
        return detected


    def _screenshot_backends(self) -> list[tuple[str, str]]:
        if self._desktop_is_kde():
            spectacle = shutil.which(self.config.spectacle_bin)
            return [("spectacle", spectacle)] if spectacle else []
        grim = shutil.which(self.config.grim_bin)
        return [("grim", grim)] if grim else []


    def _screenshot_health(self) -> dict[str, Any]:
        grim = shutil.which(self.config.grim_bin)
        spectacle = shutil.which(self.config.spectacle_bin)
        backends = self._screenshot_backends()
        candidates = self._wayland_display_candidates()
        error: str | None = None
        if not self.config.screenshot_enabled:
            error = "screenshot tool is disabled"
        elif not backends:
            error = "no supported screenshot backend found (spectacle/grim)"
        elif not candidates:
            error = "no Wayland display socket found"
        return {
            "enabled": self.config.screenshot_enabled,
            "available": error is None,
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP")
            or os.environ.get("XDG_SESSION_DESKTOP"),
            "backend": backends[0][0] if backends else None,
            "backends": [name for name, _ in backends],
            "spectacle": spectacle,
            "grim": grim,
            "wayland_display": candidates[0] if candidates else None,
            "candidates": candidates,
            "output_dir": str(
                self.config.screenshot_output_dir or default_screenshot_dir()
            ),
            "require_approval": self.config.screenshot_require_approval,
            "error": error,
        }


    def _avatar_health(self) -> dict[str, Any]:
        if self.avatar is not None:
            status = self.avatar.status()
        else:
            status = {
                "configured": self.config.avatar_path is not None,
                "loaded": False,
                "path": str(self.avatar_path) if self.avatar_path else None,
                "error": self.avatar_error,
            }
        if self.channel is not None:
            channel_health = self.channel.health()
            status["channel"] = channel_health.get("avatar")
        return status


    def _writable_status(self) -> dict[str, Any]:
        if not self.config.sandbox_enabled:
            return {
                "enabled": False,
                "available": True,
                "backend": "none",
                "blocked_reason": None,
                "revision": self.writable.revision,
                "defaults": [str(path) for path in self.writable.defaults],
                "internal": [str(path) for path in self.writable.internal],
                "registered": [
                    {"path": entry.path, "added_at": entry.added_at}
                    for entry in self.writable._roots
                ],
                "effective": [str(path) for path in self.writable.effective_roots()],
                "invalid": list(self.writable._invalid),
                "state_path": str(self.writable.state_path),
            }
        status = self.writable.status()
        status["blocked_reason"] = self._sandbox_blocked_reason
        return status


    def _health(self) -> dict[str, Any]:
        backend = self._backend_health()
        channel = self._channel_health()
        sandbox = self._sandbox_health()
        channel_ready = not channel.get("enabled", False) or bool(channel.get("connected", False))
        sandbox_ready = not sandbox.get("enabled", False) or bool(sandbox.get("available", False))
        bridge_ready = self._aki_bridge_available()
        ready = bool(backend["ready"]) and bridge_ready and channel_ready and sandbox_ready
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "backend_ready": bool(backend["ready"]),
            "bridge_ready": bridge_ready,
            "channel_ready": channel_ready,
            "sandbox_ready": sandbox_ready,
            "version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
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
            "backend": backend,
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
            "connections": len(self.connection_set),
            "queue_depth": len(self.queue),
        }


    def _live_context_health(self) -> dict[str, Any]:
        status = dict(self._live_context_status)
        status["enabled"] = bool(self.config.live_context_enabled)
        status["budget_tokens"] = self.config.live_context_budget_tokens
        return status


    def _agent_record_path(self) -> Path | None:
        folder = self.config.backend_dir / "agents"
        try:
            paths = sorted(folder.glob("*.json"))
        except OSError:
            return None
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id") == self.config.agent_id:
                return path
        return None


    def _tool_ownership_block(self) -> str:
        online = set(discover_profiles())
        online.add(self.config.profile)
        return render_tool_ownership_prompt(
            self.config.profile,
            online_profiles=online,
        )


    def _channel_policy_block(self) -> str:
        return render_channel_policy(
            user_name=self.config.username or "Master",
            emoji_entries=self._emoji_entries(),
        )


    def _channel_wake_routing_block(self) -> str:
        return render_channel_wake_routing(self.config.username or "Master")


    def _channel_identity_block(self) -> str:
        return render_channel_identity(self._channel_self_member())
