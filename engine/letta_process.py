"""Supervision of the long-lived Letta stream-json child process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections import deque
from typing import Any, Awaitable, Callable, Mapping, Sequence

from engine.protocol import ErrorCode, RpcError

BACKEND_LINE_LIMIT = 16 * 1024 * 1024
RESTART_BACKOFF = (1, 2, 4, 8, 16, 30)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
ReadyHandler = Callable[[dict[str, Any]], Awaitable[None]]
ExitHandler = Callable[[int | None, str | None], Awaitable[None]]


class BackendUnavailable(Exception):
    pass


class BackendProtocolError(Exception):
    pass


class LettaProcess:
    """Run and supervise one ``letta --input-format stream-json`` process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        on_message: MessageHandler,
        on_ready: ReadyHandler,
        on_exit: ExitHandler,
        logger: logging.Logger | None = None,
        backoff: Sequence[int] = RESTART_BACKOFF,
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env) if env is not None else dict(os.environ)
        self.on_message = on_message
        self.on_ready = on_ready
        self.on_exit = on_exit
        self.logger = logger or logging.getLogger("akid.letta")
        self.backoff = tuple(backoff)

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._stopping = False
        self._wake = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._state = "stopped"
        self._ready = False
        self._restart_count = 0
        self._last_error: str | None = None
        self._init_payload: dict[str, Any] | None = None
        self._last_ready_at: float | None = None
        self._ready_generation = 0

    @property
    def ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.returncode is None

    @property
    def state(self) -> str:
        return self._state

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def init_payload(self) -> dict[str, Any]:
        return dict(self._init_payload or {})

    @property
    def aki_bridge(self) -> dict[str, Any]:
        payload = self._init_payload or {}
        bridge = payload.get("aki_bridge")
        return dict(bridge) if isinstance(bridge, dict) else {}

    @property
    def supports_aki_bridge(self) -> bool:
        return bool(self.aki_bridge.get("version"))

    @property
    def last_ready_at(self) -> float | None:
        return self._last_ready_at

    def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stopping = False
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name="letta-supervisor"
        )

    async def wait_ready(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.ready:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            await asyncio.sleep(0.05)
        return True

    async def _supervise(self) -> None:
        backoff_index = 0
        while not self._stopping:
            self._state = "starting" if backoff_index == 0 else "restarting"
            self._ready = False
            ready_generation = self._ready_generation
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command,
                    cwd=self.cwd,
                    env=self.env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=BACKEND_LINE_LIMIT,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self._last_error = f"spawn failed: {exc}"
                self.logger.error("backend spawn failed: %s", exc)
                if not await self._sleep_backoff(backoff_index):
                    break
                backoff_index = min(backoff_index + 1, len(self.backoff) - 1)
                continue

            self._state = "running"
            self._stderr_tail.clear()
            self._reader_task = asyncio.create_task(
                self._read_loop(self._proc), name="letta-reader"
            )
            stderr_task = asyncio.create_task(
                self._read_stderr(self._proc), name="letta-stderr"
            )
            returncode = await self._proc.wait()
            self._ready = False
            self._reject_pending("backend process exited")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task

            if self._stopping:
                break

            self._restart_count += 1
            detail = self._stderr_tail[-1] if self._stderr_tail else None
            self._last_error = (
                f"backend exited with code {returncode}"
                + (f": {detail}" if detail else "")
            )
            self.logger.warning(
                "backend exited code=%s restart=%s", returncode, self._restart_count
            )
            self._state = "restarting"
            with contextlib.suppress(Exception):
                await self.on_exit(returncode, detail)

            if self._ready_generation != ready_generation:
                backoff_index = 0
            if not await self._sleep_backoff(backoff_index):
                break
            backoff_index = min(backoff_index + 1, len(self.backoff) - 1)

        self._state = "stopped"
        self._ready = False
        self._proc = None

    async def _sleep_backoff(self, index: int) -> bool:
        delay = self.backoff[min(index, len(self.backoff) - 1)] if self.backoff else 0
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        return not self._stopping

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            self._stderr_tail.append(text[:400])
            self.logger.debug("letta stderr: %s", text[:400])

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                self.logger.error("backend emitted an oversized line; stopping it")
                self._last_error = "backend line exceeded limit"
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                self.logger.warning("backend emitted invalid JSON; ignoring line")
                continue
            if not isinstance(message, dict):
                continue

            if message.get("type") == "system" and message.get("subtype") == "init":
                self._init_payload = message
                self._ready = True
                self._state = "ready"
                self._last_ready_at = time.time()
                self._ready_generation += 1
                self._last_error = None
                self.logger.info(
                    "backend ready pid=%s model=%s", proc.pid, message.get("model")
                )
                with contextlib.suppress(Exception):
                    await self.on_ready(message)
                continue

            if message.get("type") == "control_response":
                response = message.get("response")
                request_id = response.get("request_id") if isinstance(response, dict) else None
                future = self._pending.pop(str(request_id), None) if request_id else None
                if future is not None and not future.done():
                    future.set_result(message)
                    continue

            try:
                await self.on_message(message)
            except Exception:
                self.logger.exception("backend message handler failed")

    def _reject_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BackendUnavailable(reason))
        self._pending.clear()

    async def _write_json(self, message: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise BackendUnavailable("backend process is not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                proc.stdin.write(payload.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                raise BackendUnavailable("backend stdin is closed") from exc

    async def send_user(self, content: str | Sequence[Mapping[str, Any]]) -> None:
        if not self.ready:
            raise BackendUnavailable("backend is not ready")
        await self._write_json(
            {"type": "user", "message": {"role": "user", "content": content}}
        )

    async def send_interrupt(self) -> str:
        request_id = f"interrupt-{time.monotonic_ns()}"
        await self._write_json(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "interrupt"},
            }
        )
        return request_id

    async def send_control_response(
        self, request_id: str, body: Mapping[str, Any]
    ) -> None:
        await self._write_json(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": dict(body),
                },
            }
        )

    async def approve_tool(
        self, request_id: str, *, allow: bool, message: str | None = None
    ) -> None:
        if allow:
            body: dict[str, Any] = {"behavior": "allow"}
        else:
            body = {"behavior": "deny", "message": message or "Denied by user"}
        await self.send_control_response(request_id, body)

    async def send_external_tool_result(
        self,
        tool_call_id: str,
        content: list[dict[str, Any]],
        *,
        is_error: bool = False,
    ) -> None:
        await self._write_json(
            {
                "type": "control_response",
                "response": {
                    "subtype": "external_tool_result",
                    "request_id": f"ext-{tool_call_id}",
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "is_error": is_error,
                },
            }
        )

    async def control_request(
        self,
        subtype: str,
        request: Mapping[str, Any] | None = None,
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        if not self.ready:
            raise BackendUnavailable("backend is not ready")
        request_id = f"{subtype}-{time.monotonic_ns()}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_json(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": subtype, **dict(request or {})},
                }
            )
            message = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RpcError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"backend {subtype} request timed out",
            ) from exc
        finally:
            self._pending.pop(request_id, None)

        response = message.get("response")
        if not isinstance(response, dict) or response.get("subtype") != "success":
            error = response.get("error") if isinstance(response, dict) else None
            raise BackendProtocolError(str(error or f"{subtype} failed"))
        payload = response.get("response")
        return payload if isinstance(payload, dict) else {}

    async def list_messages(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        after: str | None = None,
        order: str = "desc",
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"limit": limit, "order": order}
        if before:
            request["before"] = before
        if after:
            request["after"] = after
        return await self.control_request("list_messages", request, timeout=timeout)

    async def begin_aki_turn(
        self, turn_id: str, overlay: Mapping[str, Any], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        return await self.control_request(
            "aki_begin_turn",
            {"turn_id": turn_id, "overlay": dict(overlay)},
            timeout=timeout,
        )

    async def update_aki_turn(
        self, turn_id: str, overlay: Mapping[str, Any], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        return await self.control_request(
            "aki_update_turn",
            {"turn_id": turn_id, "overlay": dict(overlay)},
            timeout=timeout,
        )

    async def end_aki_turn(
        self, turn_id: str, *, timeout: float = 10.0
    ) -> dict[str, Any]:
        return await self.control_request(
            "aki_end_turn", {"turn_id": turn_id}, timeout=timeout
        )

    async def reload_conversation(
        self,
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
            )
            if value is not None
        }
        return await self.control_request(
            "aki_reload_conversation", request, timeout=timeout
        )

    async def snapshot_conversation(
        self,
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
            )
            if value is not None
        }
        return await self.control_request(
            "aki_conversation_snapshot", request, timeout=timeout
        )

    async def restore_conversation(
        self, snapshot_id: str, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        return await self.control_request(
            "aki_conversation_restore",
            {"snapshot_id": snapshot_id},
            timeout=timeout,
        )

    async def compact_conversation(
        self,
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
        mode: str | None = None,
        timeout: float = 900.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
                ("mode", mode),
            )
            if value is not None
        }
        return await self.control_request("aki_compact", request, timeout=timeout)

    async def reflect_conversation(
        self,
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
            )
            if value is not None
        }
        return await self.control_request("aki_reflect", request, timeout=timeout)

    async def reflection_status(
        self,
        *,
        conversation_id: str | None = None,
        agent_id: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
            )
            if value is not None
        }
        return await self.control_request(
            "aki_reflection_status", request, timeout=timeout
        )

    async def memory_status(
        self, *, agent_id: str | None = None, timeout: float = 20.0
    ) -> dict[str, Any]:
        request = {"agent_id": agent_id} if agent_id else {}
        return await self.control_request("aki_memory_status", request, timeout=timeout)

    async def memory_tokens(
        self,
        *,
        agent_id: str | None = None,
        memory_dir: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        request = {
            key: value
            for key, value in (
                ("agent_id", agent_id),
                ("memory_dir", memory_dir),
            )
            if value is not None
        }
        return await self.control_request("aki_memory_tokens", request, timeout=timeout)

    async def mcp_list(
        self, *, agent_id: str | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        request = {"agent_id": agent_id} if agent_id else {}
        return await self.control_request("aki_mcp_list", request, timeout=timeout)

    async def mcp_tools(
        self, *, agent_id: str | None = None, timeout: float = 60.0
    ) -> dict[str, Any]:
        request = {"agent_id": agent_id} if agent_id else {}
        return await self.control_request("aki_mcp_tools", request, timeout=timeout)

    async def mcp_call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        agent_id: str | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "tool_name": tool_name,
            "args": dict(arguments),
        }
        if agent_id:
            request["agent_id"] = agent_id
        return await self.control_request("aki_mcp_call", request, timeout=timeout)

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=3)
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor_task
            self._supervisor_task = None
        self._proc = None
        self._ready = False
        self._state = "stopped"
