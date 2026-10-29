"""Writable-directory and native MCP configuration RPCs."""

from __future__ import annotations

from engine.protocol import (
    ErrorCode,
    RpcError,
    require_string,
)

class WritableRpcMixin:

    # -------------------------------------------------------- writable dirs
    async def _writable_add(self, params: Mapping[str, Any]) -> dict[str, Any]:
        path = require_string(params, "path")
        async with self._writable_lock:
            result = self.writable.add(path)
            if not result.get("added"):
                result.update(
                    {
                        "backend_restarted": False,
                        "interrupted_turn_id": None,
                        "backend_ready": bool(getattr(self.backend, "ready", False)),
                    }
                )
                return result
            result.update(await self._restart_for_policy_change())
            return result


    async def _writable_remove(self, params: Mapping[str, Any]) -> dict[str, Any]:
        path = require_string(params, "path")
        async with self._writable_lock:
            result = self.writable.remove(path)
            result.update(await self._restart_for_policy_change())
            return result


    async def _writable_clear(self) -> dict[str, Any]:
        async with self._writable_lock:
            result = self.writable.clear()
            if not result.get("removed_count"):
                result.update(
                    {
                        "backend_restarted": False,
                        "interrupted_turn_id": None,
                        "backend_ready": bool(getattr(self.backend, "ready", False)),
                    }
                )
                return result
            result.update(await self._restart_for_policy_change())
            return result


    async def _mcp_native_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        server = require_string(params, "server").strip()
        if not server:
            raise RpcError(ErrorCode.INVALID_PARAMS, "server must be non-empty")
        enabled = params.get("enabled")
        if not isinstance(enabled, bool):
            raise RpcError(ErrorCode.INVALID_PARAMS, "enabled must be a boolean")
        async with self._mcp_lock:
            before = set(self._mcp_native_excluded)
            if enabled:
                self._mcp_native_excluded.discard(server)
            else:
                self._mcp_native_excluded.add(server)
            changed = before != self._mcp_native_excluded
            if not changed:
                return {
                    "server": server,
                    "enabled": enabled,
                    "changed": False,
                    "backend_restarted": False,
                    "interrupted_turn_id": None,
                    "backend_ready": bool(getattr(self.backend, "ready", False)),
                    "status": self._mcp_native_status(),
                }
            try:
                self._save_mcp_native_state()
            except OSError as exc:
                self._mcp_native_excluded = before
                raise RpcError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"cannot persist MCP native state: {exc}",
                ) from exc
            if self.config.mcp_native_tools_enabled:
                restart = await self._restart_backend_for_mcp_change()
            else:
                restart = {
                    "backend_restarted": False,
                    "interrupted_turn_id": None,
                    "backend_ready": bool(getattr(self.backend, "ready", False)),
                    "external_tools_pending": self._external_tools_pending,
                }
            return {
                "server": server,
                "enabled": enabled,
                "changed": True,
                **restart,
                "status": self._mcp_native_status(),
            }
