"""Switch the profile model, reasoning effort, and backend process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any, Mapping
from engine.aki_config import (
    atomic_write,
    load_config,
    set_key,
)
from engine.letta_llm import (
    DEFAULT_REASONING_EFFORT,
    supported_reasoning_efforts,
    write_provider_mod,
)
from engine.solo_memory import (
    FALLBACK_SUMMARY_STATUS,
    NSFW_ERROR_STATUS,
    NSFW_PENDING_STATUS,
    SOLO_NSFW_TIMEOUT_SECONDS,
    SOLO_SUMMARY_TIMEOUT_SECONDS,
    SoloMemorySession,
    capture_solo_entries,
    fallback_summary,
    strip_runtime_prompt_blocks,
)
from engine.model_privacy import is_local_model_route
from engine.solo_mode import (
    SOLO_REASONING_EFFORT,
    conversation_directory,
    ensure_solo_conversation,
    now_iso as solo_now_iso,
    solo_conversation_id,
)
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


    def _model_catalog(self) -> list[dict[str, Any]]:
        aki = self.config.aki
        routes = () if aki is None else tuple(getattr(aki, "enabled_models", ()))
        return [
            {
                "id": route.model_id,
                "handle": f"{self._provider_id()}/{route.model_id}",
                "upstream": route.upstream_name,
                "upstream_model": route.upstream_model,
                "local": is_local_model_route(route),
            }
            for route in routes
        ]


    def _model_status(self) -> dict[str, Any]:
        self._reload_model_catalog()
        model, _effort, _context = self._agent_info()
        aki = self.config.aki
        solo_model = str(getattr(aki, "solo_model", "") or "")
        solo_route = self._solo_model_override()
        if solo_route is not None:
            solo_handle = f"{self._provider_id()}/{solo_route.model_id}"
            solo_mode = "configured"
        elif self.solo_backend.configured:
            managed_route = self.solo_backend.last_route()
            solo_handle = (
                f"{self._provider_id()}/{managed_route.model_id}"
                if managed_route is not None
                else ""
            )
            solo_mode = "managed-local"
        else:
            solo_handle = model or ""
            solo_mode = "group"
        return {
            "model": model,
            "models": self._model_catalog(),
            "solo_model": solo_model,
            "solo_handle": solo_handle,
            "solo_mode": solo_mode,
        }


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


    def _solo_model_override(self):
        aki = self.config.aki
        if aki is None:
            return None
        model_id = str(getattr(aki, "solo_model", "") or "").strip()
        if not model_id:
            return None
        routes = tuple(getattr(aki, "models", ())) + tuple(
            getattr(aki, "enabled_models", ())
        )
        for route in routes:
            if str(getattr(route, "model_id", "") or "") == model_id:
                return route
        return None


    def _route_uses_managed_solo_backend(self, route) -> bool:
        if not self.solo_backend.configured:
            return False
        if route is None:
            return True
        endpoint = str(getattr(self.solo_backend, "endpoint", "") or "").rstrip("/")
        route_url = str(getattr(route, "url", "") or "").rstrip("/")
        if not endpoint or not route_url:
            return False
        expected = (endpoint + str(getattr(route, "path", "") or "")).rstrip("/")
        return route_url == expected


    def _solo_uses_managed_backend(self) -> bool:
        if not self.solo_backend.configured:
            return False
        override = self._solo_model_override()
        return override is None or self._route_uses_managed_solo_backend(override)


    def _sync_solo_conversation_model(self, managed_route=None) -> None:
        aki = self.config.aki
        if aki is None:
            return
        override = self._solo_model_override()
        route = override if override is not None else managed_route
        if route is None:
            if self.solo_backend.configured:
                ensure_solo_conversation(
                    backend_dir=self.config.backend_dir,
                    agent_id=self.config.agent_id,
                    cwd=self.config.cwd,
                    update_model=False,
                )
            else:
                ensure_solo_conversation(
                    backend_dir=self.config.backend_dir,
                    agent_id=self.config.agent_id,
                    cwd=self.config.cwd,
                    model="",
                )
            return
        provider_id = self._provider_id()
        ensure_solo_conversation(
            backend_dir=self.config.backend_dir,
            agent_id=self.config.agent_id,
            cwd=self.config.cwd,
            model=f"{provider_id}/{route.model_id}",
            provider_type=provider_id,
            context_window=getattr(route, "context_window", None),
            max_tokens=getattr(route, "max_tokens", None),
        )


    def _active_model_is_local(self) -> bool:
        if self._solo_session is not None:
            override = self._solo_model_override()
            if override is not None:
                return is_local_model_route(override)
            if self.solo_backend.configured:
                # The managed solo backend is by definition local.  Its
                # SoloRoute has endpoint/upstream_model but not the ModelRoute
                # url/upstream_name fields, so do not run the generic route
                # classification here.
                return True
        model, _reasoning, _limit = self._agent_info()
        return is_local_model_route(self._active_model_route(model))


    async def _model_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw = params.get("model")
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "model must be a string")
        if bool(params.get("solo", False)):
            return await self._solo_model_set(raw)
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


    async def _solo_model_set(self, raw: str) -> dict[str, Any]:
        """Select one configured model route for solo without touching group."""
        if self.active is not None or self.queue:
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "cannot change the solo model while a turn is active or queued",
            )
        self._reload_model_catalog()
        aki = self.config.aki
        path = getattr(aki, "path", None) if aki is not None else None
        if aki is None or not path:
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "profile config is unavailable",
            )
        catalog = self._model_catalog()
        known = {item["id"] for item in catalog}
        choice = raw.strip()
        if choice.casefold() in {"auto", "default", "managed"}:
            model_id = ""
        else:
            model_id = self._normalize_model_id(choice)
            if model_id not in known:
                raise RpcError(
                    ErrorCode.INVALID_PARAMS,
                    f"model must be one of: {', '.join(item['id'] for item in catalog)}",
                    {"models": catalog},
                )
        previous = str(getattr(aki, "solo_model", "") or "")
        set_key(Path(path), "solo", "model", model_id)
        self._reload_model_catalog()
        changed = previous != model_id
        backend_reloaded = False
        try:
            self._sync_solo_conversation_model()
            if (
                changed
                and self._solo_session is not None
                and self._active_conversation_id()
                == solo_conversation_id(self.config.agent_id)
                and bool(getattr(self.backend, "ready", False))
            ):
                await self.backend.reload_conversation(
                    conversation_id=self._active_conversation_id(),
                    agent_id=self.config.agent_id,
                )
                backend_reloaded = True
        except Exception as exc:
            set_key(Path(path), "solo", "model", previous)
            self._reload_model_catalog()
            with contextlib.suppress(Exception):
                self._sync_solo_conversation_model()
            raise RpcError(
                ErrorCode.INTERNAL_ERROR,
                f"cannot apply solo model: {exc}",
            ) from exc
        status = self._model_status()
        status.update(
            {
                "changed": changed,
                "backend_reloaded": backend_reloaded,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
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
        solo_route = self._solo_model_override()
        if solo_route is not None:
            model_id = solo_route.model_id
            api = solo_route.api
            route_path = solo_route.path
        elif self.solo_backend.configured:
            if not self.solo_backend.status().get("running"):
                return None
            managed_route = self.solo_backend.last_route()
            if managed_route is None:
                return None
            model_id = managed_route.model_id
            api = managed_route.api
            route_path = managed_route.path
        else:
            agent_model, _reasoning, _limit = self._agent_info()
            route = self._active_model_route(agent_model)
            if route is None:
                route = next(iter(aki.enabled_models), None)
            if route is None:
                return None
            model_id = route.model_id
            api = route.api
            route_path = route.path
        return {
            "base_prompt": self._compiled_persona_prompt(),
            "proxy_base_url": aki.proxy_base_url,
            "proxy_token": aki.proxy_token,
            "model": model_id,
            "api": api,
            "route_path": route_path,
        }


    def _solo_nsfw_llm_config(self) -> dict[str, str] | None:
        """Return the configured solo route, or the running managed local route."""
        aki = self.config.aki
        if aki is None or not aki.proxy_token:
            return None
        route = self._solo_model_override()
        if route is None:
            if not self.solo_backend.configured:
                return None
            if not self.solo_backend.status().get("running"):
                return None
            route = self.solo_backend.last_route()
        if route is None:
            return None
        return {
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
            nsfw=False,
            nsfw_status=NSFW_PENDING_STATUS,
            history_start_seq=int(self._solo_memory_start_seq),
            entries=entries,
        )
        if self.solo_backend.configured:
            try:
                aki = self.config.aki
                await self.solo_backend.start(
                    reserved_model_ids={
                        route.model_id for route in aki.enabled_models
                    }
                    if aki is not None
                    else set()
                )
            except Exception:
                LOGGER.exception(
                    "cannot start solo backend for session summary session=%s",
                    session.session_id,
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
        nsfw_config = self._solo_nsfw_llm_config()
        if nsfw_config is not None:
            await self.solo_memory.classify_nsfw(
                memory_session,
                **nsfw_config,
                timeout=SOLO_NSFW_TIMEOUT_SECONDS,
            )
        else:
            memory_session.nsfw = True
            memory_session.nsfw_status = NSFW_ERROR_STATUS
            LOGGER.warning(
                "solo NSFW classification unavailable; hiding session from group "
                "session=%s",
                memory_session.session_id,
            )
        self.solo_memory.add_session(memory_session)
        LOGGER.info(
            "solo memory stored session=%s entries=%s summary_status=%s "
            "nsfw=%s nsfw_status=%s",
            memory_session.session_id,
            len(entries),
            memory_session.summary_status,
            memory_session.nsfw,
            memory_session.nsfw_status,
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


    def _reasoning_status(
        self, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if params is not None and bool(params.get("solo", False)):
            return self._solo_reasoning_status()
        model, effort, _context = self._agent_info()
        allowed = supported_reasoning_efforts(self._active_model_route(model))
        return {
            "scope": "group",
            "model": model,
            "effort": effort or DEFAULT_REASONING_EFFORT,
            "default": DEFAULT_REASONING_EFFORT,
            "allowed": list(allowed),
        }


    def _solo_conversation_record_path(self) -> Path:
        return (
            conversation_directory(
                self.config.backend_dir,
                solo_conversation_id(self.config.agent_id),
                self.config.agent_id,
            )
            / "conversation.json"
        )


    def _solo_reasoning_target(self) -> tuple[str, Any | None, bool]:
        override = self._solo_model_override()
        if override is not None:
            return f"{self._provider_id()}/{override.model_id}", override, False
        solo_backend = getattr(self, "solo_backend", None)
        if bool(getattr(solo_backend, "configured", False)):
            route = solo_backend.last_route()
            handle = (
                f"{self._provider_id()}/{route.model_id}" if route is not None else ""
            )
            return handle, None, True
        model, _effort, _context = self._agent_info()
        return model or "", self._active_model_route(model), False


    def _solo_reasoning_status(self) -> dict[str, Any]:
        model, route, managed_local = self._solo_reasoning_target()
        configured_effort: str | None = None
        path = self._solo_conversation_record_path()
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict):
            settings = record.get("model_settings")
            if isinstance(settings, dict):
                value = settings.get("reasoning_effort")
                if isinstance(value, str) and value:
                    configured_effort = value
                else:
                    nested = settings.get("reasoning")
                    if isinstance(nested, dict):
                        value = nested.get("reasoning_effort")
                        if isinstance(value, str) and value:
                            configured_effort = value
        if managed_local:
            return {
                "scope": "solo",
                "model": model,
                "effort": SOLO_REASONING_EFFORT,
                "default": SOLO_REASONING_EFFORT,
                "allowed": [SOLO_REASONING_EFFORT],
                "configured": configured_effort is not None,
                "configured_effort": configured_effort,
                "managed_local": True,
                "effort_source": "fixed",
            }
        _agent_model, agent_effort, _context = self._agent_info()
        return {
            "scope": "solo",
            "model": model,
            "effort": configured_effort or agent_effort or DEFAULT_REASONING_EFFORT,
            "default": DEFAULT_REASONING_EFFORT,
            "allowed": list(supported_reasoning_efforts(route)),
            "configured": configured_effort is not None,
            "configured_effort": configured_effort,
            "managed_local": False,
            "effort_source": "conversation" if configured_effort else "agent",
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
        if bool(params.get("solo", False)):
            return await self._solo_reasoning_set(params)
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


    async def _solo_reasoning_set(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw = params.get("effort", DEFAULT_REASONING_EFFORT)
        if not isinstance(raw, str) or not raw.strip():
            raise RpcError(ErrorCode.INVALID_PARAMS, "effort must be a string")
        effort = raw.strip().lower()
        model, route, managed_local = self._solo_reasoning_target()
        if managed_local:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "managed local solo reasoning is fixed at medium; select a cloud "
                "solo model with 'akictl model set <id> --solo' first",
                {
                    "managed_local": True,
                    "model": model,
                    "effort": SOLO_REASONING_EFFORT,
                },
            )
        allowed = supported_reasoning_efforts(route)
        if effort not in allowed:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                f"reasoning effort must be one of: {', '.join(allowed)}",
                {"allowed": list(allowed), "scope": "solo"},
            )
        if self.active is not None or self.queue:
            raise RpcError(
                ErrorCode.INVALID_REQUEST,
                "cannot change solo reasoning while a turn is active or queued",
            )

        path = self._solo_conversation_record_path()
        original: str | None = None
        if path.is_file():
            try:
                original = path.read_text(encoding="utf-8")
                data = json.loads(original)
            except (OSError, ValueError) as exc:
                raise RpcError(
                    ErrorCode.INTERNAL_ERROR,
                    f"cannot read solo conversation: {exc}",
                ) from exc
        else:
            ensure_solo_conversation(
                backend_dir=self.config.backend_dir,
                agent_id=self.config.agent_id,
                cwd=self.config.cwd,
                update_model=False,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RpcError(
                ErrorCode.INTERNAL_ERROR,
                "solo conversation record is not an object",
            )
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
        previous = settings.get("reasoning_effort")
        if not isinstance(previous, str):
            nested = settings.get("reasoning")
            previous = (
                nested.get("reasoning_effort")
                if isinstance(nested, dict)
                else None
            )
        settings["reasoning_effort"] = effort
        nested = settings.get("reasoning")
        if not isinstance(nested, dict):
            nested = {}
        nested["reasoning_effort"] = effort
        settings["reasoning"] = nested
        data["model_settings"] = settings
        data["updated_at"] = solo_now_iso()
        atomic_write(
            path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )

        backend_reloaded = False
        try:
            if bool(getattr(self.backend, "ready", False)):
                await self.backend.reload_conversation(
                    conversation_id=solo_conversation_id(self.config.agent_id),
                    agent_id=self.config.agent_id,
                )
                backend_reloaded = True
        except Exception as exc:
            if original is not None:
                atomic_write(path, original)
            raise RpcError(
                ErrorCode.INTERNAL_ERROR,
                f"cannot apply solo reasoning: {exc}",
            ) from exc

        status = self._solo_reasoning_status()
        status.update(
            {
                "changed": previous != effort,
                "backend_reloaded": backend_reloaded,
                "backend_ready": bool(getattr(self.backend, "ready", False)),
            }
        )
        publish_status = getattr(self, "_publish_channel_status", None)
        if publish_status is not None:
            with contextlib.suppress(Exception):
                await publish_status()
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
