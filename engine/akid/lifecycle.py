"""Start and stop the gateway, the desktop layer, and socket clients."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import uuid
from pathlib import Path
from engine.layer_process import (
    LayerProcess,
    discover_wayland_display,
)
from engine.layer_notify import (
    LayerNotifier,
    character_for_names,
    character_for_profile,
)
from engine.solo_memory import (
    FALLBACK_SUMMARY_STATUS,
    SOLO_SUMMARY_TIMEOUT_SECONDS,
)
from engine.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    FrameError,
    RpcError,
    decode_message,
    make_error,
    make_response,
    parse_request,
    read_message,
)
from .connections import Connection
from .constants import (
    LAYER_DISPLAY_RETRY_SECONDS,
    LOGGER,
)
from .no_reply import contains_no_reply_marker

class LifecycleMixin:

    # ------------------------------------------------------------------ server
    async def _abort_startup(self, message: str) -> None:
        LOGGER.error("refusing to start: %s", message)
        with contextlib.suppress(Exception):
            await self.backend.stop()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()
        raise RuntimeError(message)


    async def start(self) -> None:
        path = self.config.socket_path
        parent = path.parent
        existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = parent.stat()
        if existing.st_uid != os.getuid():
            raise RuntimeError(f"socket directory is owned by uid {existing.st_uid}")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        dedicated_dirs = {Path(f"/tmp/aki-{os.getuid()}")}
        if runtime:
            dedicated_dirs.add(Path(runtime) / "aki")
        if not existed or parent in dedicated_dirs:
            os.chmod(parent, 0o700)
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError(f"refusing to replace non-socket path {path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
            except OSError as exc:
                raise RuntimeError(f"another akid may be running on {path}: {exc}") from exc
            else:
                raise RuntimeError(f"another akid is already listening on {path}")
            finally:
                probe.close()
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(path),
            limit=MAX_LINE_BYTES + 2,
        )
        os.chmod(path, 0o600)
        # Runtime prompt blocks are turn overlays. Never mutate the Letta
        # agent record or its compiled prompt during startup.
        self._backfill_public_memory()
        await self._refresh_channel_transcript()
        if self.config.aki is not None:
            try:
                self._prepare_letta_provider()
            except Exception:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                raise
        sandbox_available = True
        if self.config.sandbox_enabled:
            self.writable.ensure_runtime_dirs()
            available, bwrap_path, error = self.writable.probe(refresh=True)
            sandbox_available = available
            if not available:
                self._sandbox_blocked_reason = error or "sandbox unavailable"
                LOGGER.error("sandbox unavailable; Letta backend blocked: %s", self._sandbox_blocked_reason)
            else:
                LOGGER.info("sandbox ready backend=bwrap path=%s", bwrap_path)
        if sandbox_available:
            self.backend.start()
            if not await self._wait_backend_ready(30.0):
                await self._abort_startup(
                    "Letta backend did not become ready; refusing to run"
                )
            if not self._aki_bridge_available():
                await self._abort_startup(
                    "Aki requires the customized Letta runtime with aki_bridge; "
                    "the configured backend does not advertise it"
                )
        if self.channel is not None:
            await self.channel.start()
            self._start_channel_status_loop()
        self._start_layer()
        self._start_layer_notifier()
        self._start_proactive_services()
        memory_config = self._solo_memory_llm_config()
        if memory_config is not None and any(
            session.summary_status == FALLBACK_SUMMARY_STATUS
            for session in self.solo_memory.sessions
        ):
            task = asyncio.create_task(
                self.solo_memory.retry_fallback_summaries(
                    **memory_config,
                    timeout=SOLO_SUMMARY_TIMEOUT_SECONDS,
                ),
                name="akid-solo-summary-retry",
            )
            self._channel_tasks.add(task)
            task.add_done_callback(self._channel_tasks.discard)
        LOGGER.info("akid listening on %s", path)


    async def stop(self) -> None:
        self._shutting_down = True
        await self._stop_proactive_services()
        server = self._server
        if server is not None:
            server.close()
        for conn in list(self.connection_set):
            conn.closed = True
            with contextlib.suppress(Exception):
                conn.writer.close()
        all_tasks = (
            list(self._client_tasks)
            + list(self._approval_tasks)
            + list(self._turn_tasks)
            + list(self._channel_tasks)
            + list(self._external_tool_tasks)
        )
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.active is not None:
            self._release_turn_lock(self.active)
        if server is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await server.wait_closed()
            self._server = None
        self.connection_set.clear()
        self.channel_conn.closed = True
        await self.channel_conn.stop()
        self._status_stop.set()
        self._status_event.set()
        status_task = self._status_task
        self._status_task = None
        if status_task is not None:
            status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await status_task
        if self.channel is not None:
            if self.channel.connected:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._publish_channel_offline(), timeout=2.0)
            with contextlib.suppress(Exception):
                await self.channel.stop()
        notifier = self._layer_notifier
        self._layer_notifier = None
        if notifier is not None:
            with contextlib.suppress(Exception):
                await notifier.stop()
        layer_start_task = self._layer_start_task
        self._layer_start_task = None
        if layer_start_task is not None:
            layer_start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await layer_start_task
        layer = self._layer
        self._layer = None
        if layer is not None:
            with contextlib.suppress(Exception):
                await layer.stop()
        with contextlib.suppress(Exception):
            await self.backend.stop()
        self.execution_lock.close()
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()
        LOGGER.info("akid stopped")


    def _start_layer(self) -> None:
        self._layer_skip_reason = None
        if not self.config.layer_enabled:
            self._layer_skip_reason = "disabled"
            LOGGER.info("aki-layer disabled")
            return
        if self._layer is not None or self._layer_start_task is not None:
            return
        binary = self.config.layer_bin
        if not binary.is_file() or not os.access(binary, os.X_OK):
            self._layer_skip_reason = f"binary not executable: {binary}"
            LOGGER.warning("aki-layer unavailable: %s", self._layer_skip_reason)
            return
        self._layer_start_task = asyncio.create_task(
            self._bootstrap_layer(binary), name="aki-layer-bootstrap"
        )


    async def _bootstrap_layer(self, binary: Path) -> None:
        try:
            while not self._shutting_down and self._layer is None:
                display = discover_wayland_display(
                    configured=os.environ.get("WAYLAND_DISPLAY")
                )
                if display:
                    self._launch_layer(binary, display)
                    return
                if self._layer_skip_reason != "waiting for Wayland display":
                    self._layer_skip_reason = "waiting for Wayland display"
                    LOGGER.info(
                        "aki-layer waiting for a Wayland display runtime=%s",
                        os.environ.get("XDG_RUNTIME_DIR", ""),
                    )
                await asyncio.sleep(LAYER_DISPLAY_RETRY_SECONDS)
        finally:
            self._layer_start_task = None


    def _launch_layer(self, binary: Path, display: str) -> None:
        env = dict(os.environ)
        env["WAYLAND_DISPLAY"] = display
        env.setdefault("XDG_SESSION_TYPE", "wayland")
        env.setdefault("QT_QPA_PLATFORM", "wayland")
        layer = LayerProcess(
            [str(binary)],
            cwd=str(self.config.cwd),
            env=env,
            logger=logging.getLogger("akid.layer"),
        )
        layer.start()
        self._layer = layer
        self._layer_display_name = display
        self._layer_skip_reason = None
        LOGGER.info("aki-layer starting binary=%s display=%s", binary, display)


    def _start_layer_notifier(self) -> None:
        if not self.config.layer_notify_enabled or self.config.layer_socket is None:
            return
        notifier = LayerNotifier(
            self.config.layer_socket,
            preview_chars=self.config.layer_preview_chars,
            logger=logging.getLogger("akid.layer"),
        )
        notifier.start()
        self._layer_notifier = notifier
        LOGGER.info("aki-layer notifier socket=%s", self.config.layer_socket)


    def _notify_layer(self, character: str | None, text: str) -> None:
        notifier = self._layer_notifier
        if notifier is None or not character:
            return
        if contains_no_reply_marker(text):
            return
        notifier.notify(character, text)


    async def _publish_layer_presence(self) -> None:
        notifier = self._layer_notifier
        if notifier is None:
            return
        notifier.notify_presence(self.presence_monitor.layer_status())


    def _layer_own_character(self) -> str:
        return character_for_profile(self.config.profile, self.config.channel_name)


    def _layer_character_for_member(self, member_id: str, name: str) -> str | None:
        member = self._channel_members.get(member_id) if member_id else None
        aliases = tuple(member.get("aliases") or ()) if member is not None else ()
        member_name = str(member.get("name") or "") if member is not None else ""
        return character_for_names(name, member_name, member_id, *aliases)


    def _layer_health(self) -> dict[str, Any]:
        status = (
            self._layer.status()
            if self._layer is not None
            else {
                "state": "stopped",
                "pid": None,
                "command": [str(self.config.layer_bin)],
                "cwd": str(self.config.cwd),
                "restart_count": 0,
                "last_error": None,
                "started_at": None,
                "stderr_tail": [],
            }
        )
        status.update(
            {
                "enabled": bool(self.config.layer_enabled),
                "available": self._layer is not None,
                "display": self._layer_display_name
                or os.environ.get("WAYLAND_DISPLAY"),
                "skip_reason": self._layer_skip_reason,
            }
        )
        if self._layer_notifier is not None:
            status["notifier"] = self._layer_notifier.status()
        return status


    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        if sock is None:
            return None
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        except OSError:
            return None


    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        uid = self._peer_uid(writer)
        if uid is not None and uid != os.getuid():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            LOGGER.warning("rejected peer uid=%s", uid)
            return
        conn = Connection(
            reader=reader,
            writer=writer,
            connection_id=f"conn-{uuid.uuid4().hex[:12]}",
            uid=uid,
        )
        self.connection_set.add(conn)
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            while not conn.closed:
                try:
                    raw = await read_message(reader)
                except FrameError as exc:
                    await conn.send(make_error(None, ErrorCode.PARSE_ERROR, str(exc)))
                    break
                if raw is None:
                    break
                request_id: Any = None
                try:
                    value = decode_message(raw)
                    if isinstance(value, dict):
                        request_id = value.get("id")
                    request = parse_request(value)
                except FrameError as exc:
                    await conn.send(make_error(request_id, ErrorCode.PARSE_ERROR, str(exc)))
                    continue
                except RpcError as exc:
                    await conn.send(make_error(request_id, exc.code, exc.message, exc.data))
                    continue

                try:
                    result, post = await self._dispatch(conn, request)
                except RpcError as exc:
                    if not request.is_notification:
                        await conn.send(
                            make_error(request.id, exc.code, exc.message, exc.data)
                        )
                    continue
                except Exception:
                    LOGGER.exception("request handler failed method=%s", request.method)
                    if not request.is_notification:
                        await conn.send(
                            make_error(
                                request.id,
                                ErrorCode.INTERNAL_ERROR,
                                "internal gateway error",
                            )
                        )
                    continue

                if not request.is_notification:
                    try:
                        await conn.send(make_response(request.id, result))
                    except FrameError:
                        LOGGER.error(
                            "response exceeds frame limit method=%s", request.method
                        )
                        await conn.send(
                            make_error(
                                request.id,
                                ErrorCode.INTERNAL_ERROR,
                                "response exceeds the 1 MiB frame limit",
                            )
                        )
                if post is not None:
                    maybe = post()
                    if asyncio.iscoroutine(maybe):
                        await maybe
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            LOGGER.exception("connection loop failed")
        finally:
            conn.closed = True
            self.connection_set.discard(conn)
            if task is not None:
                self._client_tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            await self._on_connection_closed(conn)


    async def _on_connection_closed(self, conn: Connection) -> None:
        if self._shutting_down:
            return
        removed = [t for t in self.queue if t.conn is conn]
        if removed:
            self.queue = [t for t in self.queue if t.conn is not conn]
            for turn in removed:
                turn.state = "cancelled"
                turn.end_sent = True
                turn.done_event.set()
        if self.active is not None and self.active.conn is conn:
            await self._cancel_turn(self.active, disconnect=True)
