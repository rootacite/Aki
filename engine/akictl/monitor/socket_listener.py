"""Asyncio Unix Debug Socket listener for proxy-debug-*.sock."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .state import ApiCallRecord, MonitorState

LOGGER = logging.getLogger("akid.monitor.listener")
SOCKET_DIR = Path("/tmp/akid")


class SocketListener:
    """Listens to multiple proxy-debug Unix sockets and streams records into MonitorState."""

    def __init__(self, state: MonitorState) -> None:
        self.state = state
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        self._accumulators: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        self._running = True
        asyncio.create_task(self._discovery_loop())

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        self._tasks.clear()

    async def _discovery_loop(self) -> None:
        """Periodically scan /tmp/akid for new proxy-debug sockets."""
        while self._running:
            try:
                if SOCKET_DIR.is_dir():
                    for sock_path in SOCKET_DIR.glob("proxy-debug-*.sock"):
                        profile = sock_path.stem.replace("proxy-debug-", "")
                        if profile not in self._tasks or self._tasks[profile].done():
                            self._tasks[profile] = asyncio.create_task(
                                self._listen_profile(sock_path, profile)
                            )
            except Exception as exc:
                LOGGER.debug("socket discovery error: %s", exc)
            await asyncio.sleep(2.0)

    async def _listen_profile(self, sock_path: Path, profile: str) -> None:
        """Connect to one debug socket and stream records."""
        while self._running:
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                LOGGER.debug("connected to debug socket: %s", sock_path)
                try:
                    while self._running:
                        line = await reader.readline()
                        if not line:
                            break
                        self._handle_event_line(line, profile)
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
            except (ConnectionRefusedError, FileNotFoundError, OSError):
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOGGER.debug("debug stream exception profile=%s: %s", profile, exc)
                await asyncio.sleep(2.0)

    def _handle_event_line(self, line_bytes: bytes, profile: str) -> None:
        try:
            line_str = line_bytes.decode("utf-8").strip()
            if not line_str:
                return
            ev = json.loads(line_str)
        except Exception:
            return

        event_type = ev.get("event")
        request_id = ev.get("request_id")

        if event_type == "hello":
            return

        if not request_id:
            return

        acc = self._accumulators.setdefault(request_id, {
            "request_id": request_id,
            "profile": profile,
            "chunks_count": 0,
            "total_chunk_bytes": 0,
            "chunks_text": [],
            "first_chunk_at": 0.0,
            "tool_calls": [],
            "usage": {},
            "timings": {},
        })

        if event_type == "request":
            record = ApiCallRecord(
                request_id=request_id,
                profile=profile,
                seq=ev.get("seq", 0),
                timestamp=ev.get("ts", ""),
                path=ev.get("path", ""),
                requested_model=ev.get("requested_model", ""),
                upstream_model=ev.get("upstream_model", ""),
                upstream_url=ev.get("upstream_url", ""),
                body_bytes=ev.get("body_bytes", 0),
                body_sha256=ev.get("body_sha256", ""),
                tool_names=ev.get("tool_names", []),
                payload=ev.get("payload") if isinstance(ev.get("payload"), dict) else {},
            )
            self.state.add_or_update_call(record)

        elif event_type == "response_start":
            pass

        elif event_type == "response_chunk":
            acc["chunks_count"] += 1
            if acc["first_chunk_at"] == 0.0:
                acc["first_chunk_at"] = asyncio.get_event_loop().time()
            data = ev.get("data", "")
            encoding = ev.get("encoding", "utf-8")
            if encoding == "base64" and isinstance(data, str):
                try:
                    data = base64.b64decode(data).decode("utf-8", "replace")
                except Exception:
                    data = ""
            if isinstance(data, str):
                acc["total_chunk_bytes"] += len(data.encode("utf-8"))
                self._parse_sse_data(data, acc)

        elif event_type == "response_end":
            status = ev.get("status", 200)
            duration_ms = ev.get("duration_ms", 0)
            record = self.state.api_calls_by_id.get(request_id)
            if record is None:
                record = ApiCallRecord(
                    request_id=request_id,
                    profile=profile,
                    seq=ev.get("seq", 0),
                    timestamp=ev.get("ts", ""),
                )
            record.status = status
            record.duration_ms = duration_ms
            record.completed = True
            record.chunks_count = acc["chunks_count"]
            record.total_chunk_bytes = acc["total_chunk_bytes"]
            record.first_chunk_at = acc["first_chunk_at"]
            record.usage = acc["usage"]
            record.timings = acc["timings"]
            record.response_text = "".join(acc["chunks_text"])
            record.response_tool_calls = acc["tool_calls"]
            self.state.add_or_update_call(record)
            self._accumulators.pop(request_id, None)

        elif event_type == "error":
            record = self.state.api_calls_by_id.get(request_id)
            if record:
                record.status = ev.get("status", 500)
                record.error = str(ev.get("error", "Unknown error"))
                record.completed = True
                self.state.add_or_update_call(record)
            self._accumulators.pop(request_id, None)

    def _parse_sse_data(self, chunk_text: str, acc: dict[str, Any]) -> None:
        """Parse SSE data lines to capture streamed content, tool calls, and usage/timings."""
        for line in chunk_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if payload_str == "[DONE]":
                continue
            try:
                data = json.loads(payload_str)
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            # Extract usage / timings
            if "usage" in data and isinstance(data["usage"], dict):
                acc["usage"].update(data["usage"])
            if "timings" in data and isinstance(data["timings"], dict):
                acc["timings"].update(data["timings"])

            # Extract text choices
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                c0 = choices[0]
                if isinstance(c0, dict):
                    delta = c0.get("delta")
                    if isinstance(delta, dict):
                        content = delta.get("content")
                        if isinstance(content, str):
                            acc["chunks_text"].append(content)
                        # Tool calls
                        tc = delta.get("tool_calls")
                        if isinstance(tc, list):
                            for call in tc:
                                if isinstance(call, dict):
                                    acc["tool_calls"].append(call)
