"""Run tools that only this profile is allowed to call."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from engine.aki_config import (
    discover_profiles,
    load_config,
)
from engine.execution_policy import (
    LOCK_REQUIRED_ERROR,
    WAIT_OTHER_TOOL,
    WAIT_USER_TOOL,
    is_lock_control_tool,
    requires_execution_lock,
)
from engine.life_ledger import LedgerError
from engine.schedule_store import ScheduleError
from engine.tool_ownership import (
    authorize_exclusive_tool,
    is_exclusive_tool,
    ownership_denied_message,
)
from engine.protocol import SERVER_VERSION
from .constants import (
    CAMERA_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    LOGGER,
    MCP_TOOL_PREFIX,
    SCREENSHOT_TOOL_NAME,
    TOKIFACTOR_IMAGE_TOOL_NAME,
)



class ExclusiveToolError(ValueError):
    """A failed exclusive-tool operation with a model-facing code."""

class ExclusiveMixin:

    async def _handle_external_tool(self, message: dict[str, Any]) -> None:
        request = message.get("request") or {}
        tool_call_id = str(
            request.get("tool_call_id") or message.get("request_id") or ""
        )
        tool_name = str(request.get("tool_name") or "")
        tool_input = request.get("input") or {}
        if not tool_call_id:
            LOGGER.warning("external tool request missing tool_call_id")
            return
        turn = self.active
        if turn is None or turn.end_sent:
            await self._send_external_result(
                tool_call_id, "external tool requires an active turn", True
            )
            return
        if not isinstance(tool_input, dict):
            await self._send_external_result(
                tool_call_id, "external tool input must be an object", True
            )
            return
        if self._backend_batch_is_internal():
            await self._send_external_result(
                tool_call_id, "external tool is not available in an internal backend turn", True
            )
            return
        await self._emit_tool_call_once(turn, tool_call_id, tool_name, tool_input)
        allowed, _owner = authorize_exclusive_tool(self.config.profile, tool_name)
        if not allowed:
            await self._send_external_result(
                tool_call_id, ownership_denied_message(tool_name), True
            )
            return
        if tool_name == IMAGE_GENERATION_TOOL_NAME:
            await self._handle_image_generation_tool(
                turn, tool_call_id, tool_name, tool_input
            )
            return
        if is_exclusive_tool(tool_name):
            await self._handle_exclusive_tool(
                turn, tool_call_id, tool_name, tool_input
            )
            return
        if tool_name == WAIT_OTHER_TOOL:
            await self._handle_wait_other_tool(turn, tool_call_id)
            return
        if tool_name == WAIT_USER_TOOL:
            await self._handle_wait_user_tool(turn, tool_call_id, tool_input)
            return
        if is_lock_control_tool(tool_name):
            await self._handle_execution_lock_tool(turn, tool_call_id, tool_name)
            return
        if requires_execution_lock(tool_name) and not turn.lock_held:
            await self._send_external_result(tool_call_id, LOCK_REQUIRED_ERROR, True)
            return
        if tool_name.startswith(MCP_TOOL_PREFIX):
            await self._handle_mcp_tool(turn, tool_call_id, tool_name, tool_input)
            return
        if tool_name not in (
            TOKIFACTOR_IMAGE_TOOL_NAME,
            CAMERA_TOOL_NAME,
            SCREENSHOT_TOOL_NAME,
            IMAGE_GENERATION_TOOL_NAME,
        ):
            text = f"unknown external tool: {tool_name}"
            await self._send_external_result(tool_call_id, text, True)
            return
        if tool_name == TOKIFACTOR_IMAGE_TOOL_NAME:
            await self._handle_tokifactor_image_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        elif tool_name == SCREENSHOT_TOOL_NAME:
            await self._handle_screenshot_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        elif tool_name == IMAGE_GENERATION_TOOL_NAME:
            await self._handle_image_generation_tool(
                turn, tool_call_id, tool_name, tool_input
            )
        else:
            await self._handle_camera_tool(turn, tool_call_id, tool_name, tool_input)


    async def _handle_exclusive_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        try:
            if tool_name.startswith("aki_schedule_"):
                result = await self._run_schedule_tool(tool_name, tool_input)
            elif tool_name.startswith("aki_ledger_"):
                result = await self._run_ledger_tool(tool_name, tool_input)
            elif tool_name == "aki_zero_reply_budget":
                result = await self._run_budget_tool(tool_input)
            else:
                raise ExclusiveToolError(f"unknown exclusive tool: {tool_name}")
        except (ScheduleError, LedgerError, ExclusiveToolError) as exc:
            await self._external_tool_failure(turn, tool_call_id, str(exc))
            return
        except Exception as exc:
            LOGGER.exception("exclusive tool failed tool=%s", tool_name)
            await self._external_tool_failure(
                turn, tool_call_id, f"{tool_name} failed: {exc}"
            )
            return
        await self._external_tool_success(
            turn,
            tool_call_id,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        )


    async def _run_schedule_tool(
        self, tool_name: str, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "aki_schedule_create":
            record = await asyncio.to_thread(
                self.schedule_store.create, tool_input
            )
            return {"ok": True, "schedule": record.to_dict()}
        if tool_name == "aki_schedule_update":
            record = await asyncio.to_thread(
                self.schedule_store.update, tool_input
            )
            return {"ok": True, "schedule": record.to_dict()}
        if tool_name == "aki_schedule_delete":
            record = await asyncio.to_thread(
                self.schedule_store.delete, tool_input.get("id")
            )
            return {"ok": True, "cancelled": record.to_dict()}
        if tool_name == "aki_schedule_list":
            records = await asyncio.to_thread(
                self.schedule_store.list,
                status=str(tool_input.get("status") or "active"),
                start=tool_input.get("from"),
                end=tool_input.get("to"),
            )
            return {
                "ok": True,
                "schedules": [record.to_dict() for record in records],
            }
        raise ExclusiveToolError(f"unknown schedule tool: {tool_name}")


    async def _run_ledger_tool(
        self, tool_name: str, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        if tool_name == "aki_ledger_add":
            result = await asyncio.to_thread(
                self.life_ledger.add, tool_input.get("text")
            )
            return {"ok": True, **result}
        if tool_name == "aki_ledger_delete":
            record = await asyncio.to_thread(
                self.life_ledger.delete, tool_input.get("id")
            )
            return {"ok": True, "deleted": record.to_dict()}
        if tool_name == "aki_ledger_list":
            records = await asyncio.to_thread(self.life_ledger.records)
            return {
                "ok": True,
                "records": [record.to_dict() for record in records],
                "bytes": self.life_ledger.snapshot()["bytes"],
            }
        raise ExclusiveToolError(f"unknown ledger tool: {tool_name}")


    async def _run_budget_tool(
        self, tool_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        target = str(tool_input.get("target") or "").strip()
        if not target:
            raise ExclusiveToolError("BUDGET_TARGET_UNKNOWN: target is empty")
        reason = str(tool_input.get("reason") or "").strip()
        profile, config_path, socket_path = await asyncio.to_thread(
            self._resolve_budget_target, target
        )
        if not socket_path.is_socket():
            raise ExclusiveToolError(
                f"BUDGET_TARGET_OFFLINE: {profile} is not running"
            )

        def call_target() -> dict[str, Any]:
            from engine.client import GatewayClient

            with GatewayClient(
                socket_path,
                client_name="akid-aki",
                client_version=SERVER_VERSION,
            ) as client:
                client.handshake()
                result = client.call(
                    "aki.admin.zero_initiation_budget",
                    {"source_profile": self.config.profile, "reason": reason},
                    timeout=10.0,
                )
                return result if isinstance(result, dict) else {}

        try:
            result = await asyncio.to_thread(call_target)
        except Exception as exc:
            raise ExclusiveToolError(
                f"BUDGET_TARGET_OFFLINE: cannot reach {profile}: {exc}"
            ) from exc
        return {
            "ok": True,
            "target": profile,
            "target_config": str(config_path),
            **result,
        }


    def _resolve_budget_target(self, target: str) -> tuple[str, Path, Path]:
        wanted = target.casefold()
        matches: list[tuple[int, str, Path, Path]] = []
        for profile, config_path in discover_profiles().items():
            if profile == self.config.profile:
                continue
            try:
                conf = load_config(config_path, profile=profile)
            except (OSError, ValueError):
                continue
            labels = {
                profile.casefold(),
                conf.channel.name.casefold(),
                *{alias.casefold() for alias in conf.channel.aliases},
            }
            if wanted not in labels:
                continue
            socket_path = self._profile_socket(profile, conf.socket)
            score = 0 if wanted == profile.casefold() else 1
            matches.append((score, profile, config_path, socket_path))
        if not matches:
            if wanted in {"aki", self.config.channel_name.casefold(), *{
                alias.casefold() for alias in self.config.channel_aliases
            }}:
                raise ExclusiveToolError(
                    "BUDGET_SELF_FORBIDDEN: Aki cannot zero her own reply budget"
                )
            raise ExclusiveToolError(
                f"BUDGET_TARGET_UNKNOWN: no other member matches {target!r}"
            )
        _score, profile, config_path, socket_path = sorted(matches)[0]
        return profile, config_path, socket_path


    @staticmethod
    def _profile_socket(profile: str, configured: str) -> Path:
        if configured:
            return Path(configured).expanduser()
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        )
        filename = "akid.sock" if profile == "aki" else f"{profile}.sock"
        return runtime / "aki" / filename
