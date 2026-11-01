"""Recompile, compact, and reset the Letta conversation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from datetime import (
    datetime,
    timezone,
)
from typing import Mapping
from engine.letta_compact import reflection_transcript_paths, run_letta_compact
from engine.solo_mode import conversation_directory, solo_conversation_id
from engine.protocol import (
    ErrorCode,
    RpcError,
    optional_bool,
    optional_string,
    require_int,
)
from .constants import LOGGER
from .paths import now_iso

class ConversationMixin:

    async def _recompile_conversation(self) -> dict[str, Any]:
        async with self._recompile_lock:
            self._recompiling = True
            try:
                return await self._recompile_conversation_locked()
            finally:
                self._recompiling = False
                if not self._shutting_down:
                    await self._maybe_start_next()


    async def _recompile_conversation_locked(self) -> dict[str, Any]:
        if self._shutting_down:
            raise RpcError(ErrorCode.RECOMPILE_FAILED, "gateway is shutting down")
        if self._resetting or self._compacting or self._policy_restarting:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED,
                "another maintenance operation is in progress",
            )
        if self.active is not None or self.queue:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED, "cannot recompile while a turn is active"
            )
        try:
            result = await self.backend.recompile_conversation(
                conversation_id=self._active_conversation_id(),
                agent_id=self.config.agent_id,
                timeout=60.0,
            )
        except Exception as exc:
            raise RpcError(
                ErrorCode.RECOMPILE_FAILED,
                "Letta conversation recompile failed",
                {"detail": str(exc)},
            ) from exc
        compiled_at = (
            str(result.get("compiled_at") or "")
            if isinstance(result, Mapping)
            else ""
        )
        revision = (
            str(result.get("memfs_revision") or "")
            if isinstance(result, Mapping)
            else ""
        )
        LOGGER.info(
            "conversation prompt recompiled by Letta revision=%s",
            revision,
        )
        return {
            "recompiled": True,
            "conversation_id": self._active_conversation_id(),
            "memfs_revision": revision or None,
            "compiled_at": compiled_at or None,
            "backend_ready": bool(getattr(self.backend, "ready", False)),
        }


    async def _compact_conversation(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        mode = optional_string(params, "mode")
        if mode is not None and mode not in {
            "all",
            "sliding_window",
            "self_compact_all",
            "self_compact_sliding_window",
        }:
            raise RpcError(
                ErrorCode.INVALID_PARAMS,
                "mode must be all, sliding_window, self_compact_all, "
                "or self_compact_sliding_window",
            )
        timeout = require_int(
            params, "timeout", default=900, minimum=30, maximum=3600
        )
        omit_tool_results = optional_bool(params, "omit_tool_results", False)
        async with self._compact_lock:
            self._compacting = True
            try:
                if self._shutting_down:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED, "gateway is shutting down"
                    )
                if self._resetting or self._recompiling or self._policy_restarting:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "another maintenance operation is in progress",
                    )
                if self.active is not None or self.queue:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "cannot compact while a turn is active or queued",
                    )

                result: dict[str, Any] | None = None
                if self._aki_bridge_available():
                    try:
                        result = await self._compact_conversation_bridge(
                            mode, float(timeout)
                        )
                    except RpcError:
                        raise
                    except Exception as exc:
                        raise RpcError(
                            ErrorCode.COMPACT_FAILED,
                            str(exc),
                            {"conversation_id": self._active_conversation_id()},
                        ) from exc
                    LOGGER.info(
                        "conversation compacted via bridge mode=%s reflection=%s "
                        "before=%s after=%s",
                        result.get("mode"),
                        result.get("reflection"),
                        result.get("num_messages_before"),
                        result.get("num_messages_after"),
                    )
                else:
                    with contextlib.suppress(Exception):
                        await self.backend.stop()
                    failure: Exception | None = None
                    try:
                        result = await asyncio.to_thread(
                            run_letta_compact,
                            letta_bin=self.config.letta_bin,
                            agent_id=self.config.agent_id,
                            conversation_id=self._active_conversation_id(),
                            cwd=self.config.cwd,
                            mode=mode,
                            timeout=float(timeout),
                            backend_dir=self.config.backend_dir,
                            letta_home=(
                                self.config.letta_home or self.writable.letta_home
                            ),
                            env=os.environ,
                        )
                    except Exception as exc:
                        failure = exc
                    finally:
                        try:
                            self.backend.start()
                            ready = await self._wait_backend_ready(60.0)
                        except Exception as exc:
                            LOGGER.exception("backend restart failed after compaction")
                            ready = False
                            if failure is None:
                                failure = exc

                    if failure is not None:
                        raise RpcError(
                            ErrorCode.COMPACT_FAILED,
                            str(failure),
                            {
                                "conversation_id": self._active_conversation_id(),
                                "backend_ready": ready,
                                "backend_error": getattr(self.backend, "last_error", None),
                            },
                        )
                    if result is None:
                        raise RpcError(
                            ErrorCode.COMPACT_FAILED,
                            "compaction returned no result",
                            {"backend_ready": ready},
                        )
                    if not ready:
                        raise RpcError(
                            ErrorCode.COMPACT_FAILED,
                            "conversation compacted but backend did not become ready",
                            {
                                **result,
                                "backend_ready": False,
                                "backend_error": getattr(self.backend, "last_error", None),
                            },
                        )
                    LOGGER.info(
                        "conversation compacted mode=%s reflection=%s before=%s after=%s",
                        result.get("mode"),
                        result.get("reflection"),
                        result.get("num_messages_before"),
                        result.get("num_messages_after"),
                    )
                if result is None:
                    raise RpcError(
                        ErrorCode.COMPACT_FAILED,
                        "compaction returned no result",
                    )
                if omit_tool_results:
                    result = {
                        **result,
                        **await self._omit_conversation_tool_results(),
                    }
                return {
                    **result,
                    "conversation_id": self._active_conversation_id(),
                    "backend_ready": True,
                }
            finally:
                self._compacting = False
                if not self._shutting_down:
                    await self._maybe_start_next()


    async def _compact_conversation_bridge(
        self, mode: str | None, timeout: float
    ) -> dict[str, Any]:
        before = await self.backend.reflection_status(
            conversation_id=self._active_conversation_id(),
            agent_id=self.config.agent_id,
        )
        compacted = await self.backend.compact_conversation(
            conversation_id=self._active_conversation_id(),
            agent_id=self.config.agent_id,
            mode=mode,
            timeout=timeout,
        )
        reflection = "not_launched"
        try:
            launched = await self.backend.reflect_conversation(
                conversation_id=self._active_conversation_id(),
                agent_id=self.config.agent_id,
            )
            if not launched.get("launched"):
                reflection = str(launched.get("reason") or "not_launched")
            else:
                reflection = await self._wait_bridge_reflection(before, timeout)
        except Exception as exc:
            LOGGER.warning("bridge reflection failed: %s", exc)
            reflection = "failed"
        return {
            "compacted": True,
            "mode": mode or "default",
            "num_messages_before": compacted.get("num_messages_before"),
            "num_messages_after": compacted.get("num_messages_after"),
            "summary": compacted.get("summary"),
            "reflection": reflection,
        }


    async def _wait_bridge_reflection(
        self, before: Mapping[str, Any], timeout: float
    ) -> str:
        before_succeeded = str(before.get("last_reflection_succeeded_at") or "")
        before_started = str(before.get("last_reflection_started_at") or "")
        deadline = time.monotonic() + max(30.0, timeout)
        seen = False
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            try:
                state = await self.backend.reflection_status(
                    conversation_id=self._active_conversation_id(),
                    agent_id=self.config.agent_id,
                )
            except Exception:
                continue
            started = str(state.get("last_reflection_started_at") or "")
            succeeded = str(state.get("last_reflection_succeeded_at") or "")
            if started and started != before_started:
                seen = True
            if succeeded and succeeded != before_succeeded:
                return "completed"
            if seen and not started:
                return "finished"
        return "timeout"


    async def _omit_conversation_tool_results(self) -> dict[str, Any]:
        omitted_content = "[Omitted]"
        omitted_parts = [{"type": "text", "text": omitted_content}]
        conversation_dir = self._conversation_dir()
        path = conversation_dir / "messages.jsonl"
        if not path.is_file():
            raise RpcError(
                ErrorCode.COMPACT_FAILED,
                "conversation messages file not found",
                {"path": str(path)},
            )
        try:
            original = path.read_bytes()
        except OSError as exc:
            raise RpcError(
                ErrorCode.COMPACT_FAILED,
                f"cannot read conversation messages: {exc}",
            ) from exc

        replaced = 0
        skipped = 0
        lines = original.splitlines(keepends=True)
        output: list[bytes] = []
        for index, line in enumerate(lines):
            if not line.strip():
                output.append(line)
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise RpcError(
                    ErrorCode.COMPACT_FAILED,
                    f"conversation row {index + 1} is not valid JSON",
                ) from exc
            message = row.get("message") if isinstance(row, dict) else None
            if not isinstance(message, dict) or message.get("role") != "toolResult":
                output.append(line)
                continue
            content = message.get("content")
            already_omitted = (
                isinstance(content, str)
                and content.strip() == omitted_content
            ) or (
                isinstance(content, list)
                and bool(content)
                and all(
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and str(part.get("text") or "").strip() == omitted_content
                    for part in content
                )
            )
            if already_omitted:
                skipped += 1
                output.append(line)
                continue
            message["content"] = (
                omitted_content
                if isinstance(content, str)
                else [dict(part) for part in omitted_parts]
            )
            output.append(
                (
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
            )
            replaced += 1

        if replaced == 0:
            return {
                "tool_results_replaced": 0,
                "tool_results_skipped": skipped,
                "omit_tool_results_backup": None,
            }

        try:
            backup = self._backup_conversation(
                conversation_dir, suffix="omit-tool-results"
            )
        except Exception as exc:
            raise RpcError(
                ErrorCode.COMPACT_FAILED,
                f"cannot back up conversation before omitting tool results: {exc}",
            ) from exc

        try:
            self._replace_messages_file(path, b"".join(output))
            await self.backend.reload_conversation(
                conversation_id=self._active_conversation_id(),
                agent_id=self.config.agent_id,
            )
        except Exception as exc:
            restore_error: Exception | None = None
            try:
                self._replace_messages_file(path, original)
                await self.backend.reload_conversation(
                    conversation_id=self._active_conversation_id(),
                    agent_id=self.config.agent_id,
                )
            except Exception as restore_exc:
                restore_error = restore_exc
            detail = {
                "omit_tool_results_backup": str(backup),
                "detail": str(exc),
            }
            if restore_error is not None:
                detail["restore_detail"] = str(restore_error)
                raise RpcError(
                    ErrorCode.COMPACT_FAILED,
                    "tool-result omission failed and conversation rollback also failed",
                    detail,
                ) from exc
            raise RpcError(
                ErrorCode.COMPACT_FAILED,
                "tool-result omission failed; original conversation restored",
                detail,
            ) from exc
        LOGGER.info(
            "conversation tool results omitted replaced=%s skipped=%s backup=%s",
            replaced,
            skipped,
            backup,
        )
        return {
            "tool_results_replaced": replaced,
            "tool_results_skipped": skipped,
            "omit_tool_results_backup": str(backup),
        }


    # ------------------------------------------------------------- conversation
    def _active_conversation_id(self) -> str:
        active = getattr(self, "_backend_conversation_id", "")
        if isinstance(active, str) and active:
            return active
        return self.config.conversation_id


    def _conversation_dir_for(self, conversation_id: str) -> Path:
        return conversation_directory(
            self.config.backend_dir,
            conversation_id,
            self.config.agent_id,
        )


    def _conversation_dir(self) -> Path:
        return self._conversation_dir_for(self._active_conversation_id())


    def _conversation_backup_root(self) -> Path:
        return self.config.backend_dir.parent / "backups"


    def _reflection_dir_for(self, conversation_id: str) -> Path:
        _transcript, state = reflection_transcript_paths(
            os.environ, self.config.agent_id, conversation_id
        )
        return state.parent


    def _backup_soft_reset(
        self,
        conversation_id: str,
        conversation_dir: Path,
        reflection_dir: Path,
    ) -> Path:
        root = self._conversation_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"aki-{conversation_id}-soft-reset-{stamp}"
        candidate = root / name
        counter = 1
        while candidate.exists():
            candidate = root / f"{name}-{counter}"
            counter += 1
        candidate.mkdir(mode=0o700)
        shutil.copytree(
            conversation_dir, candidate / "conversation", symlinks=True
        )
        if reflection_dir.is_dir():
            shutil.copytree(
                reflection_dir, candidate / "reflection", symlinks=True
            )
        return candidate


    def _clear_conversation_messages(self, conversation_dir: Path) -> int:
        messages_path = conversation_dir / "messages.jsonl"
        cleared_bytes = 0
        if messages_path.exists():
            cleared_bytes = messages_path.stat().st_size
        with messages_path.open("wb"):
            pass
        os.chmod(messages_path, 0o600)

        conversation_path = conversation_dir / "conversation.json"
        if not conversation_path.exists():
            raise ValueError("conversation.json is missing")
        try:
            record = json.loads(conversation_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("conversation.json is invalid") from exc
        if not isinstance(record, dict):
            raise ValueError("conversation.json is not an object")
        record["in_context_message_ids"] = []
        record["summary"] = None
        record["last_message_at"] = None
        record["updated_at"] = now_iso()
        temp_path = conversation_path.with_name(conversation_path.name + ".tmp")
        temp_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, conversation_path)
        return cleared_bytes


    def _clear_reflection_artifacts(self, conversation_id: str) -> dict[str, Any]:
        transcript_path, state_path = reflection_transcript_paths(
            os.environ, self.config.agent_id, conversation_id
        )
        root = state_path.parent
        cleared_paths: list[str] = []
        for path in (transcript_path, state_path):
            if path.is_file() or path.is_symlink():
                path.unlink()
                cleared_paths.append(str(path))
        for path in sorted(root.glob("payload-*.json")):
            if path.is_file() or path.is_symlink():
                path.unlink()
                cleared_paths.append(str(path))
        payload_dir = root / "multi-reflection-payloads"
        if payload_dir.is_symlink():
            payload_dir.unlink()
            cleared_paths.append(str(payload_dir))
        elif payload_dir.is_dir():
            shutil.rmtree(payload_dir)
            cleared_paths.append(str(payload_dir))
        return {
            "transcript_path": str(transcript_path),
            "state_path": str(state_path),
            "cleared_paths": cleared_paths,
        }


    async def _soft_reset_conversation(
        self, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        solo = optional_bool(params, "solo", False)
        conversation_id = (
            solo_conversation_id(self.config.agent_id)
            if solo
            else self.config.conversation_id
        )
        async with self._reset_lock:
            if self._compacting or self._recompiling:
                raise RpcError(
                    ErrorCode.RESET_FAILED,
                    "another maintenance operation is in progress",
                )
            self._resetting = True
            interrupted_turn_id: str | None = None
            dropped_turn_ids: list[str] = []
            backup_path: Path | None = None
            cleared_bytes = 0
            try:
                previous_active = self._active_conversation_id()
                conversation_dir = self._conversation_dir_for(conversation_id)
                if not conversation_dir.is_dir():
                    raise RpcError(
                        ErrorCode.RESET_FAILED,
                        "conversation directory not found",
                        {
                            "conversation_id": conversation_id,
                            "solo": solo,
                            "agent_id": self.config.agent_id,
                            "path": str(conversation_dir),
                        },
                    )
                reflection_dir = self._reflection_dir_for(conversation_id)

                turn = self.active
                if turn is not None and not turn.end_sent:
                    interrupted_turn_id = turn.turn_id
                    turn.cancelled = True
                    for approval in list(self.approvals.values()):
                        if approval.turn is turn and not approval.future.done():
                            approval.future.set_result((False, "soft reset"))

                queued = list(self.queue)
                self.queue.clear()
                for item in queued:
                    dropped_turn_ids.append(item.turn_id)
                    item.cancelled = True
                    item.state = "cancelled"
                    item.end_sent = True
                    item.done_event.set()
                    with contextlib.suppress(Exception):
                        await self._emit(
                            item,
                            "turn_end",
                            {
                                "reason": "soft_reset",
                                "cancelled": True,
                                "message": "soft reset",
                            },
                        )

                if turn is not None and not turn.end_sent:
                    with contextlib.suppress(Exception):
                        await self._emit(
                            turn,
                            "error",
                            {
                                "code": "SOFT_RESET",
                                "message": "soft reset; turn interrupted",
                            },
                        )
                    await self._finish_turn(
                        turn,
                        "soft_reset",
                        {"interrupted": True, "message": "soft reset"},
                    )

                with contextlib.suppress(Exception):
                    await self.backend.stop()
                backup_path = self._backup_soft_reset(
                    conversation_id, conversation_dir, reflection_dir
                )
                cleared_bytes = self._clear_conversation_messages(conversation_dir)
                reflection = self._clear_reflection_artifacts(conversation_id)
                if conversation_id == previous_active:
                    self.last_usage = None
                    self._schedule_status_refresh()
                # Letta always starts on the configured group conversation;
                # restore the previously active conversation after readiness.
                self._backend_conversation_id = self.config.conversation_id
                self.backend.start()
                ready = await self._wait_backend_ready(20.0)
                if not ready:
                    raise RpcError(
                        ErrorCode.RESET_FAILED,
                        "conversation cleared but backend did not become ready",
                        {
                            "backup_path": str(backup_path),
                            "cleared_messages_bytes": cleared_bytes,
                            "backend_ready": False,
                            "backend_error": getattr(self.backend, "last_error", None),
                        },
                    )
                if previous_active != self.config.conversation_id:
                    restored = await self._switch_backend_conversation(
                        previous_active
                    )
                    if not restored:
                        raise RpcError(
                            ErrorCode.RESET_FAILED,
                            "conversation cleared but the active conversation "
                            "could not be restored",
                            {
                                "backup_path": str(backup_path),
                                "cleared_messages_bytes": cleared_bytes,
                                "active_conversation_id": previous_active,
                                "backend_ready": True,
                            },
                        )
                LOGGER.info(
                    "conversation soft reset id=%s solo=%s backup=%s cleared_bytes=%s",
                    conversation_id,
                    solo,
                    backup_path,
                    cleared_bytes,
                )
                return {
                    "conversation_id": conversation_id,
                    "mode": "solo" if solo else "group",
                    "agent_id": self.config.agent_id,
                    "backup_path": str(backup_path),
                    "cleared_messages_bytes": cleared_bytes,
                    "reflection": reflection,
                    "interrupted_turn_id": interrupted_turn_id,
                    "dropped_turn_ids": dropped_turn_ids,
                    "active_conversation_id": self._active_conversation_id(),
                    "preserved": ["channel_history", "solo_memory"],
                    "backend_ready": True,
                }
            except RpcError:
                raise
            except Exception as exc:
                LOGGER.exception("conversation soft reset failed")
                raise RpcError(
                    ErrorCode.RESET_FAILED, f"conversation soft reset failed: {exc}"
                ) from exc
            finally:
                self._resetting = False
                if not self._shutting_down and not bool(
                    getattr(self.backend, "ready", False)
                ):
                    with contextlib.suppress(Exception):
                        self.backend.start()
                        await self._wait_backend_ready(20.0)
                if not self._shutting_down:
                    await self._maybe_start_next()
