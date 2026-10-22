"""Trigger Letta's /compact command on the local backend.

akid's long-lived stream-json backend does not accept slash commands.  This
module starts a short-lived Letta app server, sends the official
``execute_command`` requests for ``compact`` and ``reflect``, and waits for
the memory reflection task to finish before shutting it down.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

COMPACT_MODES = (
    "all",
    "sliding_window",
    "self_compact_all",
    "self_compact_sliding_window",
)
DEFAULT_TIMEOUT = 900.0
SERVER_START_TIMEOUT = 30.0
REFLECTION_GRACE = 30.0
REFLECTION_FINALIZE_TIMEOUT = 30.0
MAX_WS_MESSAGE = 64 * 1024 * 1024


class CompactError(RuntimeError):
    """Raised when the Letta app-server compaction path fails."""


class _WebSocket:
    """Minimal RFC 6455 text client used for the loopback app server."""

    def __init__(self, sock: socket.socket, *, initial: bytes = b"") -> None:
        self.sock = sock
        self._buffer = initial
        self._closed = False

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            try:
                chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            except TimeoutError:
                raise
            except OSError as exc:
                raise CompactError(f"app server socket failed: {exc}") from exc
            if not chunk:
                raise CompactError("app server websocket closed unexpectedly")
            self._buffer += chunk
        data = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError as exc:
            raise CompactError(f"app server socket failed: {exc}") from exc

    def _recv_message(self) -> str:
        fragments: list[bytes] = []
        expected = 0
        while True:
            first, second = self._recv_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._recv_exact(8))
            if length > MAX_WS_MESSAGE:
                raise CompactError("app server websocket frame is too large")
            mask_bytes = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(
                    byte ^ mask_bytes[index % 4] for index, byte in enumerate(payload)
                )
            if opcode == 0x8:
                self._closed = True
                raise CompactError("app server websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                fragments = [payload]
                expected = length
            elif opcode == 0x0:
                fragments.append(payload)
                expected += length
            else:
                continue
            if not final:
                continue
            if expected > MAX_WS_MESSAGE:
                raise CompactError("app server websocket message is too large")
            try:
                return b"".join(fragments).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CompactError("app server sent invalid UTF-8") from exc

    def send_json(self, message: Mapping[str, Any]) -> None:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_frame(0x1, payload)

    def recv_json(self, timeout: float) -> dict[str, Any]:
        if not self._buffer:
            try:
                ready, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
            except (OSError, ValueError) as exc:
                raise CompactError(f"app server socket failed: {exc}") from exc
            if not ready:
                raise TimeoutError("timed out waiting for the app server")
        self.sock.settimeout(max(30.0, timeout))
        try:
            payload = self._recv_message()
        except TimeoutError as exc:
            raise CompactError("timed out reading an app server message") from exc
        try:
            message = json.loads(payload)
        except ValueError as exc:
            raise CompactError("app server sent invalid JSON") from exc
        if not isinstance(message, dict):
            raise CompactError("app server sent an unexpected message")
        return message

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(0x8, b"")
        except CompactError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _websocket_connect(host: str, port: int, *, timeout: float) -> _WebSocket:
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET /ws HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise CompactError("app server closed during websocket handshake")
            response += chunk
            if len(response) > 64 * 1024:
                raise CompactError("app server websocket handshake is too large")
    except OSError as exc:
        sock.close()
        raise CompactError(f"app server websocket handshake failed: {exc}") from exc
    header, _, initial = response.partition(b"\r\n\r\n")
    lines = header.split(b"\r\n")
    status_line = lines[0].decode("latin-1") if lines else ""
    if " 101 " not in status_line:
        sock.close()
        raise CompactError(f"app server rejected websocket upgrade: {status_line}")
    expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    ).decode("ascii")
    accepted = ""
    for line in lines[1:]:
        name, separator, value = line.decode("latin-1").partition(":")
        if separator and name.strip().lower() == "sec-websocket-accept":
            accepted = value.strip()
            break
    if accepted and accepted != expected:
        sock.close()
        raise CompactError("app server websocket handshake was not verified")
    return _WebSocket(sock, initial=initial)


def _drain_stderr(stream: Any, tail: deque[str]) -> None:
    try:
        for line in stream:
            text = str(line).strip()
            if text:
                tail.append(text[:400])
    except (OSError, ValueError):
        return


def _stop_server(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return


def _server_env(
    base: Mapping[str, str] | None,
    *,
    backend_dir: Path | str | None,
    letta_home: Path | str | None,
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["LETTA_LOCAL_BACKEND_EXPERIMENTAL"] = "1"
    if backend_dir is not None:
        env["LETTA_LOCAL_BACKEND_DIR"] = str(Path(backend_dir).expanduser())
    if letta_home is not None:
        env["LETTA_HOME"] = str(Path(letta_home).expanduser())
    return env


def _wait_for_server(
    proc: subprocess.Popen[Any],
    host: str,
    port: int,
    tail: deque[str],
    *,
    timeout: float,
) -> _WebSocket:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            detail = tail[-1] if tail else f"exit code {proc.returncode}"
            raise CompactError(f"letta app server exited early: {detail}")
        try:
            return _websocket_connect(host, port, timeout=1.0)
        except (OSError, CompactError) as exc:
            last_error = str(exc)
            time.sleep(0.1)
    detail = tail[-1] if tail else last_error or "timeout"
    raise CompactError(f"letta app server did not become ready: {detail}")


def _runtime_scope(agent_id: str, conversation_id: str) -> dict[str, str]:
    return {"agent_id": agent_id, "conversation_id": conversation_id}


def _touch_agent_record(
    backend_dir: str | Path | None, agent_id: str
) -> bool:
    if backend_dir is None:
        return False
    agents = Path(backend_dir).expanduser() / "agents"
    try:
        paths = list(agents.glob("*.json"))
    except OSError:
        return False
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("id") != agent_id:
            continue
        try:
            stat_result = path.stat()
            os.utime(
                path,
                ns=(
                    stat_result.st_atime_ns,
                    max(stat_result.st_mtime_ns + 1_000_000_000, time.time_ns()),
                ),
            )
        except OSError:
            return False
        return True
    return False


def _wait_runtime_start(
    ws: _WebSocket,
    *,
    agent_id: str,
    conversation_id: str,
    timeout: float,
) -> dict[str, str]:
    request_id = f"runtime-start-{os.getpid()}-{time.time_ns()}"
    scope = _runtime_scope(agent_id, conversation_id)
    ws.send_json(
        {
            "type": "runtime_start",
            "request_id": request_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "wait_for_replay": True,
            "recover_approvals": False,
            "force_device_status": False,
        }
    )
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompactError("timed out starting the Letta app-server runtime")
        try:
            message = ws.recv_json(min(remaining, 5.0))
        except TimeoutError:
            continue
        if (
            message.get("type") != "runtime_start_response"
            or message.get("request_id") != request_id
        ):
            continue
        if not message.get("success"):
            raise CompactError(
                str(message.get("error") or "failed to start app-server runtime")
            )
        runtime = message.get("runtime")
        if not isinstance(runtime, dict):
            return scope
        resolved = {
            "agent_id": str(runtime.get("agent_id") or agent_id),
            "conversation_id": str(runtime.get("conversation_id") or conversation_id),
        }
        return resolved


def _run_command(
    ws: _WebSocket,
    runtime: Mapping[str, str],
    command_id: str,
    *,
    args: str | None,
    timeout: float,
) -> tuple[bool, str]:
    request_id = f"{command_id}-{os.getpid()}-{time.time_ns()}"
    request: dict[str, Any] = {
        "type": "execute_command",
        "request_id": request_id,
        "command_id": command_id,
        "runtime": dict(runtime),
    }
    if args:
        request["args"] = args
    ws.send_json(request)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompactError(f"timed out waiting for Letta command: {command_id}")
        try:
            message = ws.recv_json(min(remaining, 5.0))
        except TimeoutError:
            continue
        if message.get("type") != "execute_command_response":
            continue
        if message.get("request_id") != request_id:
            continue
        return bool(message.get("success")), str(message.get("output") or "")


def _reflection_running(device_status: Any) -> bool:
    if not isinstance(device_status, dict):
        return False
    processes = device_status.get("background_processes")
    if not isinstance(processes, list):
        return False
    for process in processes:
        if not isinstance(process, dict) or process.get("kind") != "agent_task":
            continue
        task_type = str(process.get("task_type") or "").lower()
        description = str(process.get("description") or "").lower()
        if "reflection" in task_type or "reflection" in description:
            return True
    return False


def _reflection_state_path(
    env: Mapping[str, str], agent_id: str, conversation_id: str
) -> Path:
    configured = str(env.get("LETTA_TRANSCRIPT_ROOT") or "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(env.get("HOME") or str(Path.home())).expanduser()
        / ".letta"
        / "transcripts"
    )

    def safe(segment: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", segment).strip()
        return cleaned or "unknown"

    return root / safe(agent_id) / safe(conversation_id) / "state.json"


def _read_reflection_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _reflection_state_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _sync_after_compaction(
    ws: _WebSocket,
    runtime: Mapping[str, str],
    *,
    timeout: float,
) -> tuple[dict[str, Any] | None, bool]:
    request_id = f"sync-{os.getpid()}-{time.time_ns()}"
    ws.send_json(
        {
            "type": "sync",
            "request_id": request_id,
            "runtime": dict(runtime),
            "recover_approvals": False,
            "force_device_status": True,
        }
    )
    deadline = time.monotonic() + timeout
    status: dict[str, Any] | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status, False
        try:
            message = ws.recv_json(min(remaining, 2.0))
        except TimeoutError:
            continue
        if message.get("type") == "update_device_status":
            candidate = message.get("device_status")
            if isinstance(candidate, dict):
                status = candidate
        if message.get("type") == "sync_response":
            return status, bool(message.get("success"))


def _wait_reflection(
    ws: _WebSocket,
    runtime: Mapping[str, str],
    *,
    state_path: Path,
    state_before: Mapping[str, Any],
    grace: float,
    timeout: float,
) -> dict[str, Any]:
    before_succeeded = str(state_before.get("last_reflection_succeeded_at") or "")
    before_started = str(state_before.get("last_reflection_started_at") or "")
    seen = False
    seen_mtime: int | None = None
    gone_at: float | None = None
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        status, _ok = _sync_after_compaction(ws, runtime, timeout=5.0)
        if status is not None and _reflection_running(status):
            if not seen:
                seen = True
                seen_mtime = _reflection_state_mtime(state_path)
            gone_at = None
        else:
            state_after = _read_reflection_state(state_path)
            if not seen and str(
                state_after.get("last_reflection_started_at") or ""
            ) != before_started:
                seen = True
                seen_mtime = _reflection_state_mtime(state_path)
            if not seen:
                if time.monotonic() - started >= grace:
                    break
                time.sleep(1.0)
                continue
            succeeded = str(state_after.get("last_reflection_succeeded_at") or "")
            if succeeded != before_succeeded:
                return {
                    "status": "completed",
                    "launched": True,
                    "state_changed": True,
                }
            current_mtime = _reflection_state_mtime(state_path)
            if current_mtime != seen_mtime:
                return {
                    "status": "finished",
                    "launched": True,
                    "state_changed": True,
                }
            if gone_at is None:
                gone_at = time.monotonic()
            elif time.monotonic() - gone_at >= REFLECTION_FINALIZE_TIMEOUT:
                return {
                    "status": "finished",
                    "launched": True,
                    "state_changed": False,
                }
        time.sleep(1.0)
    state_after = _read_reflection_state(state_path)
    succeeded = str(state_after.get("last_reflection_succeeded_at") or "")
    if succeeded != before_succeeded:
        return {"status": "completed", "launched": seen, "state_changed": True}
    if seen:
        return {"status": "timeout", "launched": True, "state_changed": False}
    return {"status": "not_launched", "launched": False, "state_changed": False}


def _parse_compact_output(output: str) -> dict[str, Any]:
    text = str(output or "")
    compacted = "compaction completed" in text.lower()
    if "number of messages is the same" in text.lower():
        compacted = False
    before = after = None
    match = re.search(
        r"reduced from\s+(\d+)\s+to\s+(\d+)", text, flags=re.IGNORECASE
    )
    if match:
        before = int(match.group(1))
        after = int(match.group(2))
    summary = None
    summary_match = re.search(r"\nSummary:\s*(.*)\Z", text, flags=re.DOTALL)
    if summary_match:
        summary = summary_match.group(1).strip()
    return {
        "compacted": compacted,
        "num_messages_before": before,
        "num_messages_after": after,
        "summary": summary,
    }


def run_letta_compact(
    *,
    letta_bin: str,
    agent_id: str,
    conversation_id: str,
    cwd: str | Path,
    mode: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    backend_dir: str | Path | None = None,
    letta_home: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run Letta's compact slash command and wait for memory reflection."""

    if mode is not None and mode not in COMPACT_MODES:
        raise CompactError(f"invalid compaction mode: {mode}")
    timeout = max(30.0, float(timeout))
    host = "127.0.0.1"
    port = _free_loopback_port()
    base_env = _server_env(env, backend_dir=backend_dir, letta_home=letta_home)
    command = [
        letta_bin,
        "server",
        "--listen",
        f"ws://{host}:{port}",
    ]
    tail: deque[str] = deque(maxlen=40)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=base_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise CompactError(f"cannot start letta app server: {exc}") from exc
    if proc.stderr is not None:
        threading.Thread(
            target=_drain_stderr, args=(proc.stderr, tail), daemon=True
        ).start()

    ws: _WebSocket | None = None
    try:
        ws = _wait_for_server(
            proc, host, port, tail, timeout=min(SERVER_START_TIMEOUT, timeout)
        )
        runtime = _wait_runtime_start(
            ws,
            agent_id=agent_id,
            conversation_id=conversation_id,
            timeout=min(60.0, timeout),
        )
        # The app server can read the agent record before global provider mods
        # are registered, which makes normalizeAgentRecord() double-prefix a
        # custom model handle (aki/aki/model). Reload the mods, bump the agent
        # file mtime without changing its content, and re-read the runtime so
        # the provider is known when the handle is normalized.
        _run_command(ws, runtime, "reload", args=None, timeout=30.0)
        _touch_agent_record(backend_dir, agent_id)
        runtime = _wait_runtime_start(
            ws,
            agent_id=agent_id,
            conversation_id=conversation_id,
            timeout=min(60.0, timeout),
        )
        state_path = _reflection_state_path(base_env, agent_id, conversation_id)
        state_before = _read_reflection_state(state_path)
        deadline = time.monotonic() + timeout
        success, output = _run_command(
            ws,
            runtime,
            "compact",
            args=mode,
            timeout=max(30.0, deadline - time.monotonic()),
        )
        if not success:
            raise CompactError(output or "Letta compaction failed")
        parsed = _parse_compact_output(output)
        remaining = max(30.0, deadline - time.monotonic())
        reflect_ok, reflect_output = _run_command(
            ws,
            runtime,
            "reflect",
            args=None,
            timeout=min(60.0, remaining),
        )
        reflection_status: str | None = None
        if not reflect_ok:
            reflection_status = "failed"
        elif "No new transcript content" in reflect_output:
            reflection_status = "up_to_date"
        elif "needs the memory filesystem" in reflect_output:
            reflection_status = "unavailable"
        elif reflect_output.startswith("Failed to start reflection"):
            reflection_status = "failed"
        if reflection_status is not None:
            reflection = {
                "status": reflection_status,
                "launched": False,
                "state_changed": False,
            }
        else:
            reflection = _wait_reflection(
                ws,
                runtime,
                state_path=state_path,
                state_before=state_before,
                grace=min(REFLECTION_GRACE, remaining),
                timeout=remaining,
            )
        return {
            "compacted": parsed["compacted"],
            "mode": mode or "default",
            "num_messages_before": parsed["num_messages_before"],
            "num_messages_after": parsed["num_messages_after"],
            "summary": parsed["summary"],
            "output": output,
            "reflection": reflection["status"],
            "reflection_launched": reflection["launched"],
            "reflection_state_changed": reflection["state_changed"],
            "reflection_output": reflect_output,
        }
    finally:
        if ws is not None:
            ws.close()
        _stop_server(proc)
