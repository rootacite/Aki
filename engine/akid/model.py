"""Switch the profile model, reasoning effort, and backend process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from engine.aki_config import (
    atomic_write,
    load_config,
)
from engine.letta_llm import (
    DEFAULT_REASONING_EFFORT,
    supported_reasoning_efforts,
    write_provider_mod,
)
from engine.solo_memory import (
    FALLBACK_SUMMARY_STATUS,
    SOLO_SUMMARY_TIMEOUT_SECONDS,
    SoloMemorySession,
    capture_solo_entries,
    fallback_summary,
    strip_runtime_prompt_blocks,
)
from engine.solo_mode import now_iso as solo_now_iso
from engine.protocol import (
    ErrorCode,
    RpcError,
)
from .constants import LOGGER

class ModelMixin:

    def _provider_id(self) -> str:
        aki = self.config.aki
        value = getattr(aki, "provider_id", None) if aki is not None else None
        if isinstance(value, str) and value:
            return value
        return "aki" if self.config.profile == "aki" else f"aki-{self.config.profile}"


    def _reload_model_catalog(self) -> None:
        aki = self.config.aki
        path = getattr(aki, "path", None) if aki is not None else None
        if not path:
            return
        try:
            loaded = load_config(
                Path(path),
                profile=getattr(aki, "profile", self.config.profile),
            )
        except (OSError, ValueError) as exc:
            raise RpcError(ErrorCode.INTERNAL_ERROR, f"cannot read aki.conf: {exc}") from exc
        self.config.aki = loaded


    def _model_catalog(self) -> list[dict[str, str]]:
        aki = self.config.aki
        routes = () if aki is None else tuple(getattr(aki, "enabled_models", ()))
        return [
            {
                "id": route.model_id,
                "handle": f"{self._provider_id()}/{route.model_id}",
                "upstream": route.upstream_name,
                "upstream_model": route.upstream_model,
            }
            for route in routes
        ]


    def _model_status(self) -> dict[str, Any]:
        self._reload_model_catalog()
        model, _effort, _context = self._agent_info()
        return {"model": model, "models": self._model_catalog()}


    def _normalize_model_id(self, raw: str) -> str:
        text = raw.strip()
        provider_id = self._provider_id()
        while "/" in text:
            prefix, remainder = text.split("/", 1)
            if not (prefix == provider_id or prefix == "aki" or prefix.startswith("aki-")):
                break
            text = remainder
        if not text or "/" in text:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "model must be a configured id, for example gemini-3.8-flash",
            )
        return text


    async def _model_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw = params.get("model")
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "model must be a string")
        self._reload_model_catalog()
        model_id = self._normalize_model_id(raw)
        catalog = self._model_catalog()
        known = {item["id"] for item in catalog}
        if model_id not in known:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"model must be one of: {', '.join(item['id'] for item in catalog)}",
                {"models": catalog},
            )
        provider_id = self._provider_id()
        handle = f"{provider_id}/{model_id}"
        active_route = None
        if self.config.aki is not None:
            active_route = next(
                (route for route in self.config.aki.enabled_models if route.model_id == model_id),
                None,
            )
        previous = self._write_agent_model(
            handle,
            context_window=getattr(active_route, "context_window", None),
            max_tokens=getattr(active_route, "max_tokens", None),
        )
        changed = previous != handle
        if changed and hasattr(self.config.aki, "proxy_base_url"):
            letta_home = self.config.letta_home or self.writable.letta_home
            write_provider_mod(letta_home, self.config.aki)
        restart = (
            await self._restart_backend_for_mcp_change(
                code="MODEL_CHANGED",
                message="model changed; restarting backend",
                finish_reason="model_restart",
                finish_message="backend restarted for model change",
            )
            if changed
            else {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        )
        status = self._model_status()
        status.update(
            {
                "changed": changed,
                "backend_restarted": restart.get("backend_restarted", False),
                "interrupted_turn_id": restart.get("interrupted_turn_id"),
                "backend_ready": restart.get("backend_ready", False),
            }
        )
        return status


    def _write_agent_model(
        self,
        handle: str,
        *,
        context_window: int | None = None,
        max_tokens: int | None = None,
    ) -> str | None:
        target, data = self._agent_record()
        previous = data.get("model")
        data["model"] = handle
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
            data["model_settings"] = settings
        settings["provider_type"] = self._provider_id()
        if context_window is not None:
            settings["context_window_limit"] = context_window
        if max_tokens is not None:
            settings["max_tokens"] = max_tokens
        atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return previous if isinstance(previous, str) else None


    def _agent_record(self) -> tuple[Path, dict[str, Any]]:
        agent_id = self.config.agent_id
        agents = self.config.backend_dir / "agents"
        if agents.is_dir():
            for path in agents.glob("*.json"):
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(loaded, dict) and loaded.get("id") == agent_id:
                    return path, loaded
        raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "agent file is missing")


    def _compiled_persona_prompt(self) -> str:
        prompt_path = self._conversation_dir() / "system-prompt.json"
        candidates: list[str] = []
        try:
            record = json.loads(prompt_path.read_text(encoding="utf-8"))
            content = record.get("content") if isinstance(record, dict) else None
            if isinstance(content, str) and content.strip():
                candidates.append(content)
        except (OSError, ValueError):
            pass
        if not candidates:
            try:
                _path, record = self._agent_record()
                system = record.get("system")
                if isinstance(system, str) and system.strip():
                    candidates.append(system)
            except Exception:
                pass
        return strip_runtime_prompt_blocks(candidates[0] if candidates else "")


    def _solo_memory_llm_config(self) -> dict[str, str] | None:
        aki = self.config.aki
        if aki is None or not aki.proxy_token:
            return None
        model, _reasoning, _limit = self._agent_info()
        route = self._active_model_route(model)
        if route is None:
            route = next(iter(aki.enabled_models), None)
        if route is None:
            return None
        return {
            "base_prompt": self._compiled_persona_prompt(),
            "proxy_base_url": aki.proxy_base_url,
            "proxy_token": aki.proxy_token,
            "model": route.model_id,
            "api": route.api,
            "route_path": route.path,
        }


    async def _finalize_solo_memory_session(
        self,
        session: SoloSession,
        history: Sequence[ChannelTranscriptEntry],
    ) -> None:
        entries = capture_solo_entries(
            history,
            session_id=session.session_id,
            start_seq=self._solo_memory_start_seq,
        )
        if not entries:
            LOGGER.info(
                "solo memory skipped empty session=%s", session.session_id
            )
            return
        memory_session = SoloMemorySession(
            session_id=session.session_id,
            started_at=session.started_at,
            ended_at=solo_now_iso(),
            summary="",
            summary_status=FALLBACK_SUMMARY_STATUS,
            entries=entries,
        )
        config = self._solo_memory_llm_config()
        if config is not None:
            await self.solo_memory.summarize(
                memory_session,
                base_prompt=config["base_prompt"],
                proxy_base_url=config["proxy_base_url"],
                proxy_token=config["proxy_token"],
                model=config["model"],
                api=config["api"],
                route_path=config["route_path"],
                timeout=SOLO_SUMMARY_TIMEOUT_SECONDS,
            )
        else:
            memory_session.summary = fallback_summary(entries)
            memory_session.summary_status = FALLBACK_SUMMARY_STATUS
        self.solo_memory.add_session(memory_session)
        LOGGER.info(
            "solo memory stored session=%s entries=%s summary_status=%s",
            memory_session.session_id,
            len(entries),
            memory_session.summary_status,
        )


    def _active_model_route(self, model: str | None):
        aki = self.config.aki
        if aki is None or not model:
            return None
        prefix, separator, model_id = model.partition("/")
        if not separator:
            model_id = model
        elif prefix != self._provider_id():
            return None
        for route in aki.enabled_models:
            if route.model_id == model_id:
                return route
        return None


    def _reasoning_status(self) -> dict[str, Any]:
        model, effort, _context = self._agent_info()
        allowed = supported_reasoning_efforts(self._active_model_route(model))
        return {
            "model": model,
            "effort": effort or DEFAULT_REASONING_EFFORT,
            "default": DEFAULT_REASONING_EFFORT,
            "allowed": list(allowed),
        }


    def _write_reasoning_effort(self, effort: str) -> str | None:
        target, data = self._agent_record()
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
            data["model_settings"] = settings
        previous = settings.get("reasoning_effort")
        if not isinstance(previous, str):
            nested = settings.get("reasoning")
            previous = nested.get("reasoning_effort") if isinstance(nested, dict) else None
        settings["reasoning_effort"] = effort
        nested = settings.get("reasoning")
        if not isinstance(nested, dict):
            nested = {}
        nested["reasoning_effort"] = effort
        settings["reasoning"] = nested
        atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return previous if isinstance(previous, str) else None


    async def _reasoning_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw = params.get("effort", DEFAULT_REASONING_EFFORT)
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "effort must be a string")
        effort = raw.strip().lower()
        model, _current, _context = self._agent_info()
        allowed = supported_reasoning_efforts(self._active_model_route(model))
        if effort not in allowed:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"reasoning effort must be one of: {', '.join(allowed)}",
                {"allowed": list(allowed)},
            )
        previous = self._write_reasoning_effort(effort)
        changed = previous != effort
        restart = (
            await self._restart_backend_for_mcp_change(
                code="REASONING_CHANGED",
                message="reasoning effort changed; restarting backend",
                finish_reason="reasoning_restart",
                finish_message="backend restarted for reasoning effort change",
            )
            if changed
            else {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        )
        status = self._reasoning_status()
        status.update(
            {
                "changed": changed,
                "backend_restarted": restart.get("backend_restarted", False),
                "interrupted_turn_id": restart.get("interrupted_turn_id"),
                "backend_ready": restart.get("backend_ready", False),
            }
        )
        return status


    async def _restart_backend_for_mcp_change(
        self,
        *,
        code: str = "MCP_POLICY_CHANGED",
        message: str = "MCP native tool policy changed; restarting backend",
        finish_reason: str = "mcp_restart",
        finish_message: str = "backend restarted for MCP native tool policy change",
    ) -> dict[str, Any]:
        if self._compacting:
            raise RpcError(
                ErrorCode.COMPACT_FAILED, "conversation compact in progress"
            )
        if not bool(getattr(self.backend, "ready", False)):
            return {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": False,
                "external_tools_pending": self._external_tools_pending,
            }
        self._policy_restarting = True
        try:
            return await self._restart_backend_for_mcp_change_locked(
                code=code,
                message=message,
                finish_reason=finish_reason,
                finish_message=finish_message,
            )
        finally:
            self._policy_restarting = False
            if bool(getattr(self.backend, "ready", False)):
                await self._maybe_start_next()


    async def _restart_backend_for_mcp_change_locked(
        self,
        *,
        code: str = "MCP_POLICY_CHANGED",
        message: str = "MCP native tool policy changed; restarting backend",
        finish_reason: str = "mcp_restart",
        finish_message: str = "backend restarted for MCP native tool policy change",
    ) -> dict[str, Any]:
        interrupted_turn_id: str | None = None
        turn = self.active
        if turn is not None and not turn.end_sent:
            interrupted_turn_id = turn.turn_id
            turn.cancelled = True
            for approval in list(self.approvals.values()):
                if approval.turn is turn and not approval.future.done():
                    approval.future.set_result((False, message))
            await self._emit(turn, "error", {"code": code, "message": message})
            await self._finish_turn(
                turn,
                finish_reason,
                {"interrupted": True, "message": finish_message},
            )

        try:
            with contextlib.suppress(Exception):
                await self.backend.stop()
            self.backend.start()
            ready = await self._wait_backend_ready(20.0)
        except Exception as exc:
            LOGGER.exception("backend restart failed after MCP native tool policy change")
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"backend restart failed: {exc}",
                {"interrupted_turn_id": interrupted_turn_id, "backend_ready": False},
            ) from exc
        if not ready:
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "backend did not become ready",
                {
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "backend_error": getattr(self.backend, "last_error", None),
                },
            )
        try:
            await asyncio.wait_for(self._external_tools_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            LOGGER.warning("MCP native tool registration still pending after restart")
        return {
            "backend_restarted": True,
            "interrupted_turn_id": interrupted_turn_id,
            "backend_ready": True,
            "external_tools_pending": self._external_tools_pending,
        }


    async def _restart_for_policy_change(self) -> dict[str, Any]:
        if self._compacting:
            raise RpcError(
                ErrorCode.COMPACT_FAILED, "conversation compact in progress"
            )
        if not self.config.sandbox_enabled:
            return {
                "backend_restarted": False,
                "interrupted_turn_id": None,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        self._policy_restarting = True
        try:
            return await self._restart_for_policy_change_locked()
        finally:
            self._policy_restarting = False
            if bool(getattr(self.backend, "ready", False)):
                await self._maybe_start_next()


    async def _restart_for_policy_change_locked(self) -> dict[str, Any]:
        interrupted_turn_id: str | None = None
        turn = self.active
        if turn is not None and not turn.end_sent:
            interrupted_turn_id = turn.turn_id
            turn.cancelled = True
            for approval in list(self.approvals.values()):
                if approval.turn is turn and not approval.future.done():
                    approval.future.set_result((False, "writable policy changed"))
            await self._emit(
                turn,
                "error",
                {
                    "code": "SANDBOX_POLICY_CHANGED",
                    "message": "writable directory policy changed; restarting backend",
                },
            )
            await self._finish_turn(
                turn,
                "sandbox_restart",
                {"interrupted": True, "message": "backend restarted for writable policy change"},
            )

        self._sandbox_blocked_reason = None
        self.writable.ensure_runtime_dirs()
        available, _bwrap_path, error = self.writable.probe(refresh=True)
        if not available:
            self._sandbox_blocked_reason = error or "sandbox unavailable"
            with contextlib.suppress(Exception):
                await self.backend.stop()
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                "writable policy saved but sandbox is unavailable",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "sandbox_error": self._sandbox_blocked_reason,
                },
            )

        try:
            with contextlib.suppress(Exception):
                await self.backend.stop()
            self.backend.start()
            ready = await self._wait_backend_ready(20.0)
        except Exception as exc:
            LOGGER.exception("backend restart failed after writable policy change")
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                f"writable policy saved but backend restart failed: {exc}",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                },
            ) from exc
        if not ready:
            raise RpcError(
                ErrorCode.SANDBOX_RESTART_FAILED,
                "writable policy saved but backend did not become ready",
                {
                    "revision": self.writable.revision,
                    "interrupted_turn_id": interrupted_turn_id,
                    "backend_ready": False,
                    "backend_error": getattr(self.backend, "last_error", None),
                },
            )
        return {
            "backend_restarted": True,
            "interrupted_turn_id": interrupted_turn_id,
            "backend_ready": True,
            "revision": self.writable.revision,
        }


    async def _wait_backend_ready(self, timeout: float) -> bool:
        wait_ready = getattr(self.backend, "wait_ready", None)
        if wait_ready is not None:
            return bool(await wait_ready(timeout))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bool(getattr(self.backend, "ready", False)):
                return True
            await asyncio.sleep(0.05)
        return bool(getattr(self.backend, "ready", False))
