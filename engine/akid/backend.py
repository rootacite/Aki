"""Discover backend tools and track the backend output queue."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections import deque
from engine.tool_ownership import tools_for_profile
from .constants import (
    ACQUIRE_EXECUTION_LOCK_TOOL,
    CAMERA_TOOL,
    IMAGE_GENERATION_TOOL_NAME,
    LOGGER,
    MCP_TOOL_PREFIX,
    SCREENSHOT_TOOL,
    TOKIFACTOR_IMAGE_TOOL,
    WAIT_OTHER_TOOL_SCHEMA,
    WAIT_USER_TOOL_SCHEMA,
)

class BackendMixin:

    # -------------------------------------------------------------- backend rx
    async def _on_backend_ready(self, payload: dict[str, Any]) -> None:
        LOGGER.info("backend ready conversation=%s", payload.get("conversation_id"))
        if not self._aki_bridge_available():
            LOGGER.error(
                "backend is not the customized Aki Letta runtime: "
                "missing aki_bridge capability"
            )
            return
        # Never rewrite persisted prompt blocks from the runtime.
        self._reset_backend_queue_state()
        self._external_tools_registered = False
        self._external_tools_error = None
        self._mcp_tools = []
        self._mcp_tool_names = set()
        self._mcp_tools_error = None
        self._external_tools_ready.clear()
        self._external_tools_pending = bool(
            self.config.mcp_native_tools_enabled or self._external_tools()
        )
        if self._external_tools_pending:
            task = asyncio.create_task(
                self._prepare_external_tools(), name="prepare-external-tools"
            )
            self._external_tool_tasks.add(task)
            task.add_done_callback(self._external_tool_tasks.discard)
        else:
            self._external_tools_ready.set()
            await self._maybe_start_next()


    def _external_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            ACQUIRE_EXECUTION_LOCK_TOOL,
        ]
        tools.extend(tools_for_profile(self.config.profile))
        if not (
            self.config.image_generation
            and self.config.image_generation.available
        ):
            tools = [
                tool
                for tool in tools
                if tool["name"] != IMAGE_GENERATION_TOOL_NAME
            ]
        if self.config.camera_enabled and shutil.which(self.config.ffmpeg_bin):
            tools.append(CAMERA_TOOL)
        if self.config.screenshot_enabled and self._screenshot_backends():
            tools.append(SCREENSHOT_TOOL)
        if self.channel is not None:
            tools.append(TOKIFACTOR_IMAGE_TOOL)
            tools.append(WAIT_USER_TOOL_SCHEMA)
        if (
            self.channel is not None
            and self.config.channel_wait_for
            and self.config.profile != "aki"
        ):
            tools.append(WAIT_OTHER_TOOL_SCHEMA)
        tools.extend(self._mcp_tools)
        policy = self.config.tool_policy
        if policy.explicit:
            tools = [tool for tool in tools if tool["name"] in policy.allow_set]
        return tools


    async def _prepare_external_tools(self) -> None:
        try:
            if self.config.mcp_native_tools_enabled:
                await self._discover_mcp_tools()
            if self._external_tools():
                await self._register_external_tools()
        except Exception:
            LOGGER.exception("external tool preparation failed")
        finally:
            self._external_tools_pending = False
            self._external_tools_ready.set()
            await self._maybe_start_next()


    async def _discover_mcp_tools(self) -> None:
        self._mcp_tools = []
        self._mcp_tool_names = set()
        self._mcp_tools_error = None
        self._mcp_native_active_servers = []
        if self._aki_bridge_available():
            await self._discover_mcp_tools_bridge()
            return
        try:
            servers = await self._run_letta_json_value(
                ["mcp", "list", "--agent", self.config.agent_id], timeout=10.0
            )
        except Exception as exc:
            self._mcp_tools_error = str(exc)
            LOGGER.warning("MCP server discovery failed: %s", exc)
            return
        if not isinstance(servers, list):
            self._mcp_tools_error = "letta mcp list returned unexpected JSON"
            LOGGER.warning(self._mcp_tools_error)
            return

        all_names = [
            str(server.get("name"))
            for server in servers
            if isinstance(server, dict) and server.get("name")
        ]
        allowed = set(self.config.mcp_native_servers)
        names = [
            name
            for name in all_names
            if (not allowed or name in allowed)
            and name not in self._mcp_native_excluded
        ]
        self._mcp_native_active_servers = list(names)
        if not names:
            return

        discovered: dict[str, dict[str, Any]] = {}
        for server_name in names:
            try:
                raw_tools = await self._run_letta_json_value(
                    [
                        "mcp",
                        "tools",
                        server_name,
                        "--full",
                        "--agent",
                        self.config.agent_id,
                    ],
                    timeout=10.0,
                )
            except Exception as exc:
                LOGGER.warning(
                    "MCP tool discovery failed server=%s: %s", server_name, exc
                )
                continue
            if not isinstance(raw_tools, list):
                continue
            for item in raw_tools:
                spec = self._mcp_tool_spec(item, server_name)
                if spec is not None:
                    discovered[spec["name"]] = spec

        tools = list(discovered.values())
        limit = max(0, self.config.mcp_native_max_tools)
        if limit:
            tools = tools[:limit]
        else:
            tools = []
        self._mcp_tools = tools
        self._mcp_tool_names = {tool["name"] for tool in tools}
        LOGGER.info(
            "discovered MCP native tools count=%s servers=%s",
            len(tools),
            ",".join(names),
        )


    async def _discover_mcp_tools_bridge(self) -> None:
        try:
            server_payload = await self.backend.mcp_list(
                agent_id=self.config.agent_id
            )
            tool_payload = await self.backend.mcp_tools(
                agent_id=self.config.agent_id
            )
        except Exception as exc:
            self._mcp_tools_error = str(exc)
            LOGGER.warning("MCP bridge discovery failed: %s", exc)
            return
        raw_servers = server_payload.get("servers")
        all_names = (
            [
                str(item.get("name"))
                for item in raw_servers
                if isinstance(item, dict) and item.get("name")
            ]
            if isinstance(raw_servers, list)
            else []
        )
        allowed = set(self.config.mcp_native_servers)
        names = [
            name
            for name in all_names
            if (not allowed or name in allowed)
            and name not in self._mcp_native_excluded
        ]
        self._mcp_native_active_servers = list(names)
        raw_tools = tool_payload.get("tools")
        if not isinstance(raw_tools, list):
            self._mcp_tools_error = "aki_mcp_tools returned unexpected payload"
            return
        tools: list[dict[str, Any]] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            parts = name.split("__")
            if len(parts) < 3 or parts[0] != "mcp":
                continue
            server_name = parts[1]
            if server_name not in names:
                continue
            spec = self._mcp_tool_spec(item, server_name)
            if spec is not None:
                tools.append(spec)
        limit = max(0, self.config.mcp_native_max_tools)
        self._mcp_tools = tools[:limit] if limit else []
        self._mcp_tool_names = {tool["name"] for tool in self._mcp_tools}
        LOGGER.info(
            "discovered MCP native tools via bridge count=%s servers=%s",
            len(self._mcp_tools),
            ",".join(names),
        )


    def _mcp_tool_spec(
        self, item: Any, server_name: str
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        if not name.startswith(MCP_TOOL_PREFIX) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", name
        ):
            return None
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        description = str(item.get("description") or "").strip()
        if not description:
            description = f"MCP tool {name} from {server_name}."
        return {
            "name": name,
            "label": name,
            "description": description,
            "parameters": schema,
            "auto_background": False,
            "timeout_ms": max(1000, int(self.config.mcp_tool_timeout * 1000)),
            "mcp_server": server_name,
        }


    async def _register_external_tools(self) -> None:
        tools = self._external_tools()
        if not tools:
            self._external_tools_error = "no external tools available"
            return
        for attempt in range(3):
            try:
                payload = await self.backend.control_request(
                    "register_external_tools",
                    {"tools": tools},
                    timeout=10.0,
                )
                registered = int(payload.get("registered", 0))
                self._external_tools_registered = registered > 0
                self._external_tools_error = None
                LOGGER.info(
                    "registered external tools=%s count=%s",
                    ",".join(tool["name"] for tool in tools),
                    registered,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._external_tools_error = str(exc)
                if attempt == 2:
                    LOGGER.warning("external tool registration failed: %s", exc)
                    return
                await asyncio.sleep(1.0)


    async def _on_backend_exit(self, returncode: int | None, detail: str | None) -> None:
        self._reset_backend_queue_state()
        self._external_tools_registered = False
        self._external_tools_error = "backend exited"
        self._external_tools_pending = False
        self._external_tools_ready.set()
        turn = self.active
        if turn is not None and not turn.end_sent:
            await self._emit(
                turn,
                "error",
                {
                    "code": "BACKEND_UNAVAILABLE",
                    "message": f"Letta backend exited with code {returncode}",
                },
            )
            await self._finish_turn(turn, "backend_error", {"returncode": returncode})


    def _reset_backend_queue_state(self) -> None:
        self._backend_queue.clear()
        self._backend_current_batch = None


    def _track_backend_queue_item(self, message: Mapping[str, Any]) -> None:
        item_id = str(message.get("item_id") or "")
        if not item_id:
            return
        self._backend_queue.append(
            {
                "id": item_id,
                "client_message_id": str(message.get("client_message_id") or ""),
                "source": str(message.get("source") or ""),
                "kind": str(message.get("kind") or ""),
            }
        )


    def _track_backend_queue_batch(self, message: Mapping[str, Any]) -> None:
        raw_ids = message.get("item_ids")
        item_ids = (
            [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        )
        try:
            merged_count = max(0, int(message.get("merged_count") or 0))
        except (TypeError, ValueError):
            merged_count = 0

        observed: list[dict[str, Any]] = []
        if item_ids:
            wanted = set(item_ids)
            remaining: deque[dict[str, Any]] = deque()
            for item in self._backend_queue:
                if item["id"] in wanted:
                    observed.append(item)
                    wanted.discard(item["id"])
                else:
                    remaining.append(item)
            self._backend_queue = remaining
        if not observed and merged_count:
            for _ in range(min(merged_count, len(self._backend_queue))):
                observed.append(self._backend_queue.popleft())

        missing = max(0, merged_count - len(observed))
        if not observed and missing == 0:
            self._backend_current_batch = None
            return
        # If the batch references items we never saw enqueued, assume it may
        # contain a user message so a legitimate turn result is never dropped.
        has_user = bool(missing) or any(
            item.get("source") == "user" for item in observed
        )
        self._backend_current_batch = {
            "batch_id": str(message.get("batch_id") or ""),
            "items": observed,
            "has_user": has_user,
            "merged_count": merged_count,
            "missing": missing,
        }
        LOGGER.debug(
            "backend queue batch id=%s merged=%s observed=%s missing=%s has_user=%s",
            self._backend_current_batch["batch_id"],
            merged_count,
            len(observed),
            missing,
            has_user,
        )


    def _backend_batch_is_internal(self) -> bool:
        batch = self._backend_current_batch
        return batch is not None and not bool(batch.get("has_user"))
