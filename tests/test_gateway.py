from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from engine.akid import GatewayConfig, GatewayServer, _b64url
from engine.letta_process import LettaProcess
from engine.protocol import ErrorCode, MAX_LINE_BYTES, RpcError
from tests.fake_mqtt_broker import FakeMqttBroker

ROOT = Path(__file__).resolve().parent.parent
FAKE_LETTA = ROOT / "tests" / "fake_letta.py"
PNG_1X1 = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
PNG_1X1_BLACK = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


class FakeChannel:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def publish_text(
        self, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        if metadata:
            kind = str(metadata.get("kind") or "event")
            self.published.append((kind, text))
            self.events.append({"text": text, **metadata})
        else:
            self.published.append(("text", text))

    async def publish_notice(self, text: str) -> None:
        self.published.append(("notice", text))

    async def publish_approval(self, data: dict) -> None:
        self.published.append(("approval", data))

    async def publish_image(
        self, data: bytes, *, caption: str | None = None, mime: str | None = None
    ) -> dict:
        result = {
            "transfer_id": "fake-transfer",
            "bytes": len(data),
            "chunks": 1,
            "mime": mime or "image/png",
            "topic": "tokifactor/picture",
        }
        self.images.append({"caption": caption, **result})
        return result

    def health(self) -> dict:
        return {
            "enabled": True,
            "state": "connected",
            "connected": True,
            "messages_received": 0,
            "replies_sent": len([item for item in self.published if item[0] == "text"]),
            "images_sent": len(self.images),
        }

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("kind") == kind]

    def final_texts(self) -> list[str]:
        return [
            str(event.get("text") or "")
            for event in self.events_of("turn")
            if event.get("phase") == "final"
        ]


class AsyncClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.events: list[dict[str, Any]] = []
        self._next_id = 0

    @classmethod
    async def connect(cls, path: Path) -> "AsyncClient":
        reader, writer = await asyncio.open_unix_connection(
            str(path), limit=MAX_LINE_BYTES + 2
        )
        return cls(reader, writer)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> str:
        self._next_id += 1
        request_id = f"t{self._next_id}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self.writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.writer.drain()
        return request_id

    async def _read(self, timeout: float = 5.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        if not line:
            raise EOFError("gateway closed the connection")
        return json.loads(line)

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = await self.send(method, params)
        while True:
            message = await self._read()
            if message.get("method") == "aki.event":
                self.events.append(message["params"])
                continue
            if str(message.get("id")) != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise RpcError(
                    int(error.get("code", -1)),
                    str(error.get("message") or "error"),
                    error.get("data"),
                )
            return message.get("result")

    async def hello(self) -> dict[str, Any]:
        return await self.call(
            "aki.hello",
            {
                "client_name": "test-client",
                "client_version": "1.0",
                "protocol_version": 1,
            },
        )

    async def next_event(
        self, *, turn_id: str | None = None, timeout: float = 5.0
    ) -> dict[str, Any] | None:
        for index, event in enumerate(self.events):
            if turn_id is None or event.get("turn_id") == turn_id:
                return self.events.pop(index)
        try:
            while True:
                message = await self._read(timeout=timeout)
                if message.get("method") != "aki.event":
                    continue
                event = message["params"]
                if turn_id is None or event.get("turn_id") == turn_id:
                    return event
                self.events.append(event)
        except asyncio.TimeoutError:
            return None

    async def collect_turn(
        self, turn_id: str, *, timeout: float = 8.0
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        while True:
            event = await self.next_event(turn_id=turn_id, timeout=timeout)
            if event is None:
                raise AssertionError(f"timed out waiting for turn {turn_id}: {collected}")
            collected.append(event)
            if event.get("type") == "turn_end":
                return collected

    async def wait_event(
        self, turn_id: str, event_type: str, *, timeout: float = 8.0
    ) -> dict[str, Any]:
        while True:
            event = await self.next_event(turn_id=turn_id, timeout=timeout)
            if event is None:
                raise AssertionError(f"event {event_type} not received for {turn_id}")
            if event.get("type") == event_type:
                return event

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.memory = self.root / "memory"
        self.memory.mkdir(mode=0o700)
        (self.memory / "MEMORY.md").write_text("# Aki memory root\nindex\n", encoding="utf-8")
        (self.memory / "sub").mkdir()
        (self.memory / "sub" / "note.md").write_text("beta AKI line\n", encoding="utf-8")
        (self.memory / ".hidden").write_text("hidden\n", encoding="utf-8")
        self.outside = self.root / "outside-secret.txt"
        self.outside.write_text("outside\n", encoding="utf-8")
        os.symlink(self.outside, self.memory / "escape.md")
        self.server: GatewayServer | None = None
        self.clients: list[AsyncClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        if self.server is not None:
            await self.server.stop()
        self.tempdir.cleanup()

    async def start_server(
        self,
        *,
        queue_max: int = 32,
        approval_timeout: float = 2.0,
        sandbox_enabled: bool = False,
        bwrap_bin: str = "bwrap",
        writable_state: Path | None = None,
        wait_backend: bool = True,
        channel: Any | None = None,
        image_require_approval: bool = False,
        mqtt_enabled: bool = False,
        mqtt_host: str = "192.168.3.1",
        mqtt_port: int = 1883,
        mqtt_state: Path | None = None,
        camera_enabled: bool = True,
        camera_require_approval: bool = True,
        camera_device: str = "/dev/video0",
        camera_width: int = 64,
        camera_height: int = 48,
        camera_output_dir: Path | None = None,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str | None = None,
        screenshot_enabled: bool = True,
        screenshot_require_approval: bool = False,
        grim_bin: str = "grim",
        spectacle_bin: str = "spectacle",
        screenshot_output_dir: Path | None = None,
        screenshot_wayland_display: str | None = None,
        memory_dir: Path | None = None,
        cwd: Path | None = None,
        backup_dir: Path | None = None,
    ) -> GatewayServer:
        config = GatewayConfig(
            socket_path=self.root / "akid.sock",
            agent_id="test-agent",
            conversation_id="default",
            letta_bin="letta",
            cwd=cwd or ROOT,
            memory_dir=memory_dir or self.memory,
            backup_dir=backup_dir,
            backend_dir=self.root / "backend",
            queue_max=queue_max,
            approval_timeout=approval_timeout,
            sandbox_enabled=sandbox_enabled,
            bwrap_bin=bwrap_bin,
            writable_state=writable_state,
            tokifactor_image_require_approval=image_require_approval,
            mqtt_enabled=mqtt_enabled,
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            mqtt_state=mqtt_state,
            camera_enabled=camera_enabled,
            camera_require_approval=camera_require_approval,
            camera_device=camera_device,
            camera_width=camera_width,
            camera_height=camera_height,
            camera_output_dir=camera_output_dir or (self.root / "camera"),
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            screenshot_enabled=screenshot_enabled,
            screenshot_require_approval=screenshot_require_approval,
            grim_bin=grim_bin,
            spectacle_bin=spectacle_bin,
            screenshot_output_dir=screenshot_output_dir or (self.root / "screenshots"),
            screenshot_wayland_display=screenshot_wayland_display,
        )

        def factory(on_message, on_ready, on_exit):
            return LettaProcess(
                [sys.executable, str(FAKE_LETTA)],
                cwd=str(ROOT),
                env=os.environ.copy(),
                on_message=on_message,
                on_ready=on_ready,
                on_exit=on_exit,
                backoff=(0.1, 0.1),
            )

        server = GatewayServer(config, backend_factory=factory)
        if channel is not None:
            server.channel = channel
        await server.start()
        if wait_backend:
            self.assertTrue(await server.backend.wait_ready(5.0))
        self.server = server
        return server

    async def client(self) -> AsyncClient:
        assert self.server is not None
        client = await AsyncClient.connect(self.server.config.socket_path)
        self.clients.append(client)
        return client

    async def test_streaming_events_and_sequence(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        result = await client.call("aki.chat.send", {"message": "hello"})
        self.assertEqual(result["state"], "started")
        events = await client.collect_turn(result["turn_id"])
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "turn_started")
        self.assertIn("assistant_delta", types)
        self.assertIn("usage", types)
        self.assertEqual(types[-1], "turn_end")
        seqs = [event["seq"] for event in events]
        self.assertEqual(seqs, list(range(1, len(events) + 1)))
        final = next(event for event in events if event["type"] == "assistant_message")
        self.assertIn("echo: hello", final["data"]["content"])
        end = events[-1]
        self.assertEqual(end["data"]["reason"], "completed")
        self.assertEqual(end["data"]["stop_reason"], "end_turn")

        history = await client.call("aki.chat.history", {"limit": 10})
        self.assertEqual(history["messages"][0]["id"], "m1")

    async def test_username_get_and_set(self) -> None:
        repo = self.root / "repo"
        shutil.copytree(ROOT / "userdata", repo / "userdata")
        (repo / "userdata" / "username.txt").write_text("先生\n", encoding="utf-8")
        (repo / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "sync-persona.py", repo / "scripts" / "sync-persona.py")
        memfs = self.root / "memfs"
        memfs.mkdir()
        subprocess.run(["git", "init", "-q", str(memfs)], check=True)
        subprocess.run(["git", "-C", str(memfs), "config", "user.name", "test"], check=True)
        subprocess.run(
            ["git", "-C", str(memfs), "config", "user.email", "test@example.com"],
            check=True,
        )
        await self.start_server(
            memory_dir=memfs,
            cwd=repo,
            backup_dir=self.root / "backups",
        )
        client = await self.client()
        await client.hello()
        before = await client.call("aki.username.get", {})
        self.assertEqual(before["username"], "先生")
        result = await client.call("aki.username.set", {"username": "Master"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["username"], "Master")
        after = await client.call("aki.username.get", {})
        self.assertEqual(after["username"], "Master")
        self.assertIn("Master", (memfs / "persona.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (repo / "userdata" / "username.txt").read_text(encoding="utf-8"),
            "Master\n",
        )
        log = subprocess.check_output(
            ["git", "-C", str(memfs), "log", "-1", "--pretty=%s"], text=True
        ).strip()
        self.assertEqual(log, "username: set to Master")
        again = await client.call("aki.username.set", {"username": "Master"})
        self.assertFalse(again["changed"])
        with self.assertRaises(RpcError):
            await client.call("aki.username.set", {"username": ""})

    async def test_conversation_reset_backs_up_and_clears(self) -> None:
        await self.start_server()
        assert self.server is not None
        conversation_dir = (
            self.server.config.backend_dir
            / "conversations"
            / _b64url("default:test-agent")
        )
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "manifest.json").write_text(
            json.dumps({"schema_version": 2}), encoding="utf-8"
        )
        (conversation_dir / "conversation.json").write_text(
            json.dumps(
                {
                    "id": "default",
                    "agent_id": "test-agent",
                    "archived": False,
                    "archived_at": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "last_message_at": "2026-01-01T00:00:00Z",
                    "summary": None,
                    "in_context_message_ids": ["m1"],
                }
            ),
            encoding="utf-8",
        )
        (conversation_dir / "messages.jsonl").write_text(
            '{"message":"old"}\n', encoding="utf-8"
        )
        client = await self.client()
        await client.hello()
        result = await client.call("aki.conversation.reset", {})
        self.assertTrue(result["backend_ready"])
        self.assertEqual(result["conversation_id"], "default")
        self.assertGreater(result["cleared_messages_bytes"], 0)
        backup = Path(result["backup_path"])
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "messages.jsonl").read_text(), '{"message":"old"}\n')
        self.assertEqual((conversation_dir / "messages.jsonl").read_bytes(), b"")
        record = json.loads((conversation_dir / "conversation.json").read_text())
        self.assertEqual(record["in_context_message_ids"], [])
        self.assertIsNone(record["last_message_at"])
        self.assertIsNone(record["summary"])

    async def test_conversation_reset_interrupts_active_and_drops_queue(self) -> None:
        await self.start_server()
        assert self.server is not None
        conversation_dir = (
            self.server.config.backend_dir
            / "conversations"
            / _b64url("default:test-agent")
        )
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "conversation.json").write_text(
            json.dumps(
                {
                    "id": "default",
                    "agent_id": "test-agent",
                    "archived": False,
                    "archived_at": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "last_message_at": None,
                    "summary": None,
                    "in_context_message_ids": [],
                }
            ),
            encoding="utf-8",
        )
        (conversation_dir / "messages.jsonl").write_text("", encoding="utf-8")
        client = await self.client()
        await client.hello()
        active = await client.call("aki.chat.send", {"message": "slow"})
        await client.wait_event(active["turn_id"], "turn_started")
        queued = await client.call("aki.chat.send", {"message": "queued"})
        self.assertEqual(queued["state"], "queued")
        result = await client.call("aki.conversation.reset", {})
        self.assertEqual(result["interrupted_turn_id"], active["turn_id"])
        self.assertEqual(result["dropped_turn_ids"], [queued["turn_id"]])
        active_events = await client.collect_turn(active["turn_id"])
        self.assertIn(
            "CONVERSATION_RESET",
            [event["data"].get("code") for event in active_events if event["type"] == "error"],
        )
        self.assertEqual(active_events[-1]["data"]["reason"], "conversation_reset")
        queued_events = await client.collect_turn(queued["turn_id"])
        self.assertEqual(queued_events[-1]["data"]["reason"], "conversation_reset")
        self.assertTrue((conversation_dir / "messages.jsonl").read_bytes() == b"")
        self.assertTrue(Path(result["backup_path"]).is_dir())

    async def test_history_omits_large_image_payloads(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_LETTA_HISTORY_IMAGE_BYTES": str(2 * 1024 * 1024)}
        ):
            await self.start_server()
            client = await self.client()
            await client.hello()
            history = await client.call("aki.chat.history", {"limit": 10})
        dumped = json.dumps(history)
        self.assertNotIn("A" * 1000, dumped)
        self.assertIn("[image data omitted from history]", dumped)
        self.assertEqual(history["messages"][0]["id"], "m-image")

    async def test_multi_client_isolation_queue_and_disconnect(self) -> None:
        await self.start_server()
        a = await self.client()
        await a.hello()
        a_turn = await a.call("aki.chat.send", {"message": "slow"})
        await a.wait_event(a_turn["turn_id"], "turn_started")
        a_queued = await a.call(
            "aki.chat.send", {"message": "queued from a", "client_message_id": "a-q"}
        )
        self.assertEqual(a_queued["state"], "queued")

        b = await self.client()
        await b.hello()
        status = await b.call("aki.status", {})
        self.assertEqual(status["active_turn"]["turn_id"], a_turn["turn_id"])
        b_turn = await b.call("aki.chat.send", {"message": "from b"})
        self.assertEqual(b_turn["state"], "queued")
        self.assertEqual(b_turn["queue_position"], 2)
        queued_event = await b.wait_event(b_turn["turn_id"], "turn_queued")
        self.assertEqual(queued_event["data"]["queue_position"], 2)
        self.assertIsNone(await b.next_event(timeout=0.3))

        await a.close()
        await b.wait_event(b_turn["turn_id"], "turn_started")
        events = await b.collect_turn(b_turn["turn_id"])
        self.assertEqual(events[-1]["type"], "turn_end")
        self.assertEqual(events[-1]["data"]["reason"], "completed")
        self.assertTrue(all(event["turn_id"] == b_turn["turn_id"] for event in events))

    async def test_queue_limit_and_idempotency(self) -> None:
        await self.start_server(queue_max=2)
        client = await self.client()
        await client.hello()
        active = await client.call("aki.chat.send", {"message": "slow"})
        await client.wait_event(active["turn_id"], "turn_started")
        first = await client.call(
            "aki.chat.send", {"message": "q1", "client_message_id": "same"}
        )
        duplicate = await client.call(
            "aki.chat.send", {"message": "q1", "client_message_id": "same"}
        )
        self.assertEqual(first, duplicate)
        second = await client.call("aki.chat.send", {"message": "q2"})
        self.assertEqual(second["queue_position"], 2)
        with self.assertRaises(RpcError) as ctx:
            await client.call("aki.chat.send", {"message": "q3"})
        self.assertEqual(ctx.exception.code, ErrorCode.QUEUE_FULL)

    async def test_approval_allow_deny_and_safe_tool(self) -> None:
        await self.start_server(approval_timeout=2.0)
        client = await self.client()
        await client.hello()

        allowed_turn = await client.call("aki.chat.send", {"message": "dangerous"})
        approval = await client.wait_event(allowed_turn["turn_id"], "approval_request")
        await client.call(
            "aki.approval.respond",
            {"approval_id": approval["data"]["approval_id"], "decision": "allow"},
        )
        events = await client.collect_turn(allowed_turn["turn_id"])
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "success")

        denied_turn = await client.call("aki.chat.send", {"message": "dangerous"})
        approval = await client.wait_event(denied_turn["turn_id"], "approval_request")
        await client.call(
            "aki.approval.respond",
            {
                "approval_id": approval["data"]["approval_id"],
                "decision": "deny",
                "message": "nope",
            },
        )
        events = await client.collect_turn(denied_turn["turn_id"])
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "error")

        safe_turn = await client.call("aki.chat.send", {"message": "safe"})
        events = await client.collect_turn(safe_turn["turn_id"])
        self.assertNotIn("approval_request", [event["type"] for event in events])
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "success")

    async def test_approval_timeout_denies_and_turn_continues(self) -> None:
        await self.start_server(approval_timeout=0.25)
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "timeout"})
        events = await client.collect_turn(turn["turn_id"])
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "error")
        self.assertEqual(events[-1]["data"]["reason"], "completed")

    async def test_disconnect_during_approval_recovers(self) -> None:
        await self.start_server(approval_timeout=5.0)
        owner = await self.client()
        await owner.hello()
        turn = await owner.call("aki.chat.send", {"message": "dangerous"})
        await owner.wait_event(turn["turn_id"], "approval_request")
        await owner.close()

        replacement = await self.client()
        await replacement.hello()
        next_turn = await replacement.call("aki.chat.send", {"message": "after disconnect"})
        events = await replacement.collect_turn(next_turn["turn_id"])
        self.assertEqual(events[-1]["data"]["reason"], "completed")

    async def test_compaction_events_are_normalized(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "compact"})
        events = await client.collect_turn(turn["turn_id"])
        compactions = [event for event in events if event["type"] == "compaction"]
        self.assertGreaterEqual(len(compactions), 2)
        self.assertEqual(compactions[0]["data"]["trigger"], "context_pressure")
        self.assertIn("condensed", compactions[1]["data"]["summary"])

    async def test_non_initiator_cannot_approve(self) -> None:
        await self.start_server(approval_timeout=3.0)
        owner = await self.client()
        other = await self.client()
        await owner.hello()
        await other.hello()
        turn = await owner.call("aki.chat.send", {"message": "dangerous"})
        approval = await owner.wait_event(turn["turn_id"], "approval_request")
        with self.assertRaises(RpcError) as ctx:
            await other.call(
                "aki.approval.respond",
                {"approval_id": approval["data"]["approval_id"], "decision": "allow"},
            )
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_INITIATOR)
        await owner.call(
            "aki.approval.respond",
            {"approval_id": approval["data"]["approval_id"], "decision": "deny"},
        )
        await owner.collect_turn(turn["turn_id"])

    async def test_memory_read_only_and_path_safety(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        tree = await client.call("aki.memory.tree", {"depth": 3})
        paths = {entry["path"] for entry in tree["entries"]}
        self.assertIn("MEMORY.md", paths)
        self.assertIn("sub/note.md", paths)
        self.assertNotIn(".hidden", paths)

        read = await client.call("aki.memory.read", {"path": "sub/note.md"})
        self.assertIn("beta", read["content"])
        search = await client.call("aki.memory.search", {"query": "aki"})
        self.assertGreaterEqual(len(search["matches"]), 2)

        for path in ("../outside-secret.txt", "/etc/passwd", ".hidden", "escape.md"):
            with self.assertRaises(RpcError) as ctx:
                await client.call("aki.memory.read", {"path": path})
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PATH, path)

    async def test_backend_crash_marks_turn_and_recovers(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "crash"})
        events = await client.collect_turn(turn["turn_id"])
        types = [event["type"] for event in events]
        self.assertIn("error", types)
        self.assertEqual(events[-1]["data"]["reason"], "backend_error")

        assert self.server is not None
        self.assertTrue(await self.server.backend.wait_ready(5.0))
        next_turn = await client.call("aki.chat.send", {"message": "after crash"})
        events = await client.collect_turn(next_turn["turn_id"])
        self.assertEqual(events[-1]["data"]["reason"], "completed")
        final = next(event for event in events if event["type"] == "assistant_message")
        self.assertIn("after crash", final["data"]["content"])

    async def test_handshake_required_and_protocol_mismatch(self) -> None:
        await self.start_server()
        client = await self.client()
        with self.assertRaises(RpcError) as ctx:
            await client.call("aki.status", {})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(RpcError) as ctx:
            await client.call(
                "aki.hello",
                {
                    "client_name": "test",
                    "client_version": "1",
                    "protocol_version": 99,
                },
            )
        self.assertEqual(ctx.exception.code, ErrorCode.PROTOCOL_VERSION)

    async def test_custom_socket_parent_mode_is_not_changed(self) -> None:
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        os.chmod(shared, 0o755)
        config = GatewayConfig(
            socket_path=shared / "akid.sock",
            agent_id="test-agent",
            memory_dir=self.memory,
            backend_dir=self.root / "backend",
        )

        def factory(on_message, on_ready, on_exit):
            return LettaProcess(
                [sys.executable, str(FAKE_LETTA)],
                cwd=str(ROOT),
                env=os.environ.copy(),
                on_message=on_message,
                on_ready=on_ready,
                on_exit=on_exit,
                backoff=(0.1, 0.1),
            )

        server = GatewayServer(config, backend_factory=factory)
        await server.start()
        try:
            self.assertEqual(stat_mode(shared), 0o755)
            self.assertEqual(stat_mode(shared / "akid.sock"), 0o600)
        finally:
            await server.stop()

    async def test_channel_message_gets_agent_reply(self) -> None:
        server = await self.start_server()
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]
        await server._on_channel_text("alice", "alice-1", "hello channel")
        await wait_until(
            lambda: any("channel reply" in text for text in fake_channel.final_texts())
        )
        self.assertIn("channel reply", fake_channel.final_texts())
        phases = [
            event.get("phase")
            for event in fake_channel.events_of("turn")
        ]
        self.assertEqual(phases[0], "started")
        self.assertIn("progress", phases)
        self.assertIn("final", phases)
        self.assertEqual(phases[-1], "ended")
        turn_ids = {event.get("turnId") for event in fake_channel.events_of("turn")}
        self.assertEqual(len(turn_ids), 1)

    async def test_channel_no_reply_marker_is_suppressed(self) -> None:
        server = await self.start_server()
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]
        await server._on_channel_text("alice", "alice-1", "please no-reply")
        await wait_until(lambda: server.active is None and not server.queue)
        await asyncio.sleep(0.1)
        self.assertEqual(fake_channel.final_texts(), [])
        self.assertTrue(
            any(
                event.get("phase") == "no_reply"
                for event in fake_channel.events_of("turn")
            )
        )

    async def test_channel_approval_commands_allow_and_deny(self) -> None:
        server = await self.start_server(approval_timeout=5.0)
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]

        await server._on_channel_text("alice", "alice-1", "run a dangerous tool")
        await wait_until(lambda: bool(fake_channel.events_of("approval")))
        approval = fake_channel.events_of("approval")[0]
        await server._on_channel_text("alice", "alice-1", "akid-approve")
        await wait_until(
            lambda: any("channel tool allowed" in text for text in fake_channel.final_texts())
        )
        self.assertTrue(
            any(
                event.get("toolStatus") == "running"
                for event in fake_channel.events_of("tool")
            )
        )
        self.assertTrue(
            any(
                event.get("toolStatus") == "success"
                for event in fake_channel.events_of("tool")
            )
        )
        self.assertTrue(
            any(
                event.get("decision") == "allow"
                for event in fake_channel.events_of("approval_resolved")
            )
        )

        await server._on_channel_text("alice", "alice-1", "dangerous again")
        await wait_until(lambda: len(fake_channel.events_of("approval")) >= 2)
        second = fake_channel.events_of("approval")[1]
        await server._on_channel_text(
            "alice", "alice-1", f"akid-deny {second['approvalId']}"
        )
        await wait_until(
            lambda: any("channel tool denied" in text for text in fake_channel.final_texts())
        )
        self.assertTrue(
            any(
                event.get("decision") == "deny"
                for event in fake_channel.events_of("approval_resolved")
            )
        )

    async def test_channel_only_original_sender_can_approve(self) -> None:
        server = await self.start_server(approval_timeout=5.0)
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]

        await server._on_channel_text("alice", "alice-1", "run a dangerous tool")
        await wait_until(lambda: bool(fake_channel.events_of("approval")))
        approval = fake_channel.events_of("approval")[0]
        self.assertEqual(approval.get("requesterId"), "alice-1")

        await server._on_channel_text(
            "bob", "bob-1", f"akid-approve {approval['approvalId']}"
        )
        await wait_until(
            lambda: any(
                kind == "notice" and "only the message sender" in text
                for kind, text in fake_channel.published
            )
        )
        self.assertIn(approval["approvalId"], server.approvals)

        await server._on_channel_text(
            "alice", "alice-1", f"akid-deny {approval['approvalId']}"
        )
        await wait_until(
            lambda: any(
                event.get("decision") == "deny"
                for event in fake_channel.events_of("approval_resolved")
            )
        )

    async def test_channel_cannot_approve_local_turn(self) -> None:
        server = await self.start_server(approval_timeout=5.0)
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "dangerous"})
        approval = await client.wait_event(turn["turn_id"], "approval_request")
        await server._on_channel_text("alice", "alice-1", "akid-approve")
        await wait_until(
            lambda: any(kind == "notice" and "local client" in text for kind, text in fake_channel.published)
        )
        await client.call(
            "aki.approval.respond",
            {"approval_id": approval["data"]["approval_id"], "decision": "deny"},
        )
        await client.collect_turn(turn["turn_id"])

    async def test_writable_api_restarts_backend_and_persists(self) -> None:
        state = self.root / "writable.json"
        allowed = Path(tempfile.mkdtemp(prefix="akid-gw-allow-", dir=str(Path.home() / ".local" / "share")))
        try:
            server = await self.start_server(
                sandbox_enabled=True, writable_state=state
            )
            client = await self.client()
            await client.hello()
            added = await client.call("aki.fs.writable.add", {"path": str(allowed)})
            self.assertTrue(added["added"])
            self.assertTrue(added["backend_restarted"])
            self.assertTrue(added["backend_ready"])
            listing = await client.call("aki.fs.writable.list", {})
            self.assertIn(str(allowed.resolve()), [entry["path"] for entry in listing["registered"]])
            self.assertTrue(state.exists())

            removed = await client.call("aki.fs.writable.remove", {"path": str(allowed)})
            self.assertTrue(removed["removed"])
            self.assertTrue(removed["backend_restarted"])
            listing = await client.call("aki.fs.writable.list", {})
            self.assertEqual(listing["registered"], [])
            self.assertTrue(server.backend.ready)
        finally:
            await asyncio.to_thread(__import__("shutil").rmtree, allowed, True)

    async def test_writable_change_interrupts_active_and_preserves_queue(self) -> None:
        state = self.root / "writable.json"
        allowed = Path(tempfile.mkdtemp(prefix="akid-gw-allow-", dir=str(Path.home() / ".local" / "share")))
        try:
            await self.start_server(sandbox_enabled=True, writable_state=state)
            client = await self.client()
            await client.hello()
            slow = await client.call("aki.chat.send", {"message": "slow"})
            await client.wait_event(slow["turn_id"], "turn_started")
            queued = await client.call("aki.chat.send", {"message": "after policy"})
            self.assertEqual(queued["state"], "queued")

            added = await client.call("aki.fs.writable.add", {"path": str(allowed)})
            self.assertEqual(added["interrupted_turn_id"], slow["turn_id"])

            slow_events = await client.collect_turn(slow["turn_id"])
            errors = [event for event in slow_events if event["type"] == "error"]
            self.assertTrue(errors)
            self.assertEqual(errors[0]["data"]["code"], "SANDBOX_POLICY_CHANGED")
            self.assertEqual(slow_events[-1]["data"]["reason"], "sandbox_restart")

            queued_events = await client.collect_turn(queued["turn_id"], timeout=10.0)
            self.assertEqual(queued_events[-1]["data"]["reason"], "completed")
            final = next(
                event for event in queued_events if event["type"] == "assistant_message"
            )
            self.assertIn("after policy", final["data"]["content"])
        finally:
            await asyncio.to_thread(__import__("shutil").rmtree, allowed, True)

    async def test_sandbox_unavailable_blocks_backend(self) -> None:
        await self.start_server(
            sandbox_enabled=True,
            bwrap_bin="/definitely/missing/bwrap",
            writable_state=self.root / "writable.json",
            wait_backend=False,
        )
        client = await self.client()
        await client.hello()
        health = await client.call("aki.health", {})
        self.assertFalse(health["sandbox"]["available"])
        self.assertFalse(health["ready"])
        with self.assertRaises(RpcError) as ctx:
            await client.call("aki.chat.send", {"message": "hello"})
        self.assertEqual(ctx.exception.code, ErrorCode.BACKEND_UNAVAILABLE)

    async def test_channel_text_cannot_manage_writable_dirs(self) -> None:
        server = await self.start_server(
            sandbox_enabled=True,
            writable_state=self.root / "writable.json",
        )
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]
        target = Path.home() / ".local" / "share" / "should-not-be-registered"
        await server._on_channel_text("alice", "alice-1", f"akid-register {target}")
        await wait_until(
            lambda: any("channel reply" in text for text in fake_channel.final_texts())
        )
        self.assertEqual(server.writable.revision, 0)
        self.assertEqual(server.writable.status()["registered"], [])

    async def test_tokifactor_image_external_tool_auto_approves_and_sends(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        await wait_until(lambda: server._external_tools_registered)
        client = await self.client()
        await client.hello()
        image_path = self.root / "pixel.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        turn = await client.call(
            "aki.chat.send", {"message": f"EXTERNAL_IMAGE:{image_path}"}
        )
        events = await client.collect_turn(turn["turn_id"])
        self.assertNotIn("approval_request", [event["type"] for event in events])
        self.assertEqual(len(fake_channel.images), 1)
        self.assertEqual(fake_channel.images[0]["caption"], "fake caption")
        self.assertEqual(fake_channel.images[0]["mime"], "image/png")
        final = next(event for event in events if event["type"] == "assistant_message")
        self.assertIn("external tool result: sent tokifactor image", final["data"]["content"])

    async def test_tokifactor_image_external_tool_rejects_unregistered_source(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        await wait_until(lambda: server._external_tools_registered)
        client = await self.client()
        await client.hello()
        outside = Path(tempfile.mkdtemp(prefix="akid-no-image-", dir=str(Path.home() / ".local" / "share")))
        image_path = outside / "pixel.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        try:
            turn = await client.call(
                "aki.chat.send", {"message": f"EXTERNAL_IMAGE:{image_path}"}
            )
            events = await client.collect_turn(turn["turn_id"])
            self.assertEqual(fake_channel.images, [])
            final = next(event for event in events if event["type"] == "assistant_message")
            self.assertIn("registered writable directory", final["data"]["content"])
        finally:
            await asyncio.to_thread(__import__("shutil").rmtree, outside, True)

    async def test_tokifactor_image_external_tool_can_require_approval(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel, image_require_approval=True)
        await wait_until(lambda: server._external_tools_registered)
        client = await self.client()
        await client.hello()
        image_path = self.root / "pixel-approval.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        turn = await client.call(
            "aki.chat.send", {"message": f"EXTERNAL_IMAGE:{image_path}"}
        )
        approval = await client.wait_event(turn["turn_id"], "approval_request")
        self.assertEqual(approval["data"]["tool_name"], "tokifactor_send_image")
        await client.call(
            "aki.approval.respond",
            {"approval_id": approval["data"]["approval_id"], "decision": "allow"},
        )
        await client.collect_turn(turn["turn_id"])
        self.assertEqual(len(fake_channel.images), 1)

    def _fake_ffmpeg(self) -> Path:
        path = self.root / "fake-ffmpeg"
        path.write_text(
            "#!/bin/sh\nfor last; do :; done\n"
            "printf '\\377\\330\\377\\340fake-jpeg-data' > \"$last\"\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _fake_grim(self) -> Path:
        path = self.root / "fake-grim"
        encoded = __import__("base64").b64encode(PNG_1X1).decode("ascii")
        path.write_text(
            "#!/usr/bin/python3\n"
            "import base64, os, sys\n"
            "if os.environ.get('WAYLAND_DISPLAY') == 'wayland-1':\n"
            "    print(\"compositor doesn't support the screen capture protocol\", file=sys.stderr)\n"
            "    sys.exit(1)\n"
            f"open(sys.argv[-1], 'wb').write(base64.b64decode('{encoded}'))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _fake_spectacle(self, png: bytes) -> Path:
        path = self.root / f"fake-spectacle-{__import__('uuid').uuid4().hex[:6]}"
        encoded = __import__("base64").b64encode(png).decode("ascii")
        path.write_text(
            "#!/usr/bin/python3\n"
            "import base64, sys\n"
            f"open(sys.argv[-1], 'wb').write(base64.b64decode('{encoded}'))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _wayland_runtime(self) -> tuple[Path, list[socket.socket]]:
        runtime = self.root / "wayland-runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        sockets: list[socket.socket] = []
        now = time.time()
        for index in (1, 2):
            path = runtime / f"wayland-{index}"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
            sockets.append(sock)
            os.utime(path, (now + index, now + index))
        return runtime, sockets

    async def test_camera_external_tool_is_registered_without_mqtt(self) -> None:
        server = await self.start_server(
            camera_output_dir=self.root / "camera",
            ffmpeg_bin=str(self._fake_ffmpeg()),
            ffprobe_bin=None,
        )
        await wait_until(lambda: server._external_tools_registered)
        names = [tool["name"] for tool in server._external_tools()]
        self.assertIn("camera_take_photo", names)
        self.assertIn("desktop_take_screenshot", names)
        self.assertNotIn("tokifactor_send_image", names)

    async def test_screenshot_external_tool_auto_approves_and_falls_back(self) -> None:
        runtime, sockets = self._wayland_runtime()
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_DESKTOP": "GNOME",
                    "DESKTOP_SESSION": "gnome",
                    "KDE_FULL_SESSION": "",
                },
                clear=False,
            ):
                output_dir = self.root / "screenshots"
                server = await self.start_server(
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin="/definitely/missing/spectacle",
                    screenshot_output_dir=output_dir,
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                client = await self.client()
                await client.hello()
                turn = await client.call(
                    "aki.chat.send", {"message": "EXTERNAL_SCREENSHOT"}
                )
                events = await client.collect_turn(turn["turn_id"])
                self.assertNotIn(
                    "approval_request", [event["type"] for event in events]
                )
                final = next(
                    event for event in events if event["type"] == "assistant_message"
                )
                self.assertIn("screenshot captured path=", final["data"]["content"])
                self.assertIn("display=wayland-2", final["data"]["content"])
                self.assertIn("backend=grim", final["data"]["content"])
                self.assertIn("width=1 height=1", final["data"]["content"])
            files = list(output_dir.glob("*.png"))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        finally:
            for sock in sockets:
                sock.close()

    async def test_screenshot_external_tool_can_require_approval(self) -> None:
        runtime, sockets = self._wayland_runtime()
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_DESKTOP": "GNOME",
                    "DESKTOP_SESSION": "gnome",
                    "KDE_FULL_SESSION": "",
                },
                clear=False,
            ):
                server = await self.start_server(
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin="/definitely/missing/spectacle",
                    screenshot_output_dir=self.root / "screenshots-approval",
                    screenshot_wayland_display="wayland-1",
                    screenshot_require_approval=True,
                )
                await wait_until(lambda: server._external_tools_registered)
                client = await self.client()
                await client.hello()
                turn = await client.call(
                    "aki.chat.send", {"message": "EXTERNAL_SCREENSHOT"}
                )
                approval = await client.wait_event(turn["turn_id"], "approval_request")
                self.assertEqual(
                    approval["data"]["tool_name"], "desktop_take_screenshot"
                )
                await client.call(
                    "aki.approval.respond",
                    {
                        "approval_id": approval["data"]["approval_id"],
                        "decision": "allow",
                    },
                )
                events = await client.collect_turn(turn["turn_id"])
                final = next(
                    event for event in events if event["type"] == "assistant_message"
                )
                self.assertIn("screenshot captured path=", final["data"]["content"])
        finally:
            for sock in sockets:
                sock.close()

    async def test_screenshot_on_kde_uses_spectacle(self) -> None:
        runtime, sockets = self._wayland_runtime()
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_DESKTOP": "KDE",
                },
                clear=False,
            ):
                server = await self.start_server(
                    grim_bin="/definitely/missing/grim",
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "screenshots-kde",
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                client = await self.client()
                await client.hello()
                turn = await client.call(
                    "aki.chat.send", {"message": "EXTERNAL_SCREENSHOT"}
                )
                events = await client.collect_turn(turn["turn_id"])
                final = next(
                    event for event in events if event["type"] == "assistant_message"
                )
                self.assertIn("backend=spectacle", final["data"]["content"])
                self.assertIn("display=wayland-1", final["data"]["content"])
        finally:
            for sock in sockets:
                sock.close()

    async def test_screenshot_on_kde_black_spectacle_fails_without_grim_fallback(self) -> None:
        runtime, sockets = self._wayland_runtime()
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_DESKTOP": "KDE",
                },
                clear=False,
            ):
                server = await self.start_server(
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1_BLACK)),
                    screenshot_output_dir=self.root / "screenshots-kde-fallback",
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                client = await self.client()
                await client.hello()
                turn = await client.call(
                    "aki.chat.send", {"message": "EXTERNAL_SCREENSHOT"}
                )
                events = await client.collect_turn(turn["turn_id"])
                final = next(
                    event for event in events if event["type"] == "assistant_message"
                )
                self.assertIn("black screenshot", final["data"]["content"])
                self.assertNotIn("backend=grim", final["data"]["content"])
                self.assertEqual(
                    list((self.root / "screenshots-kde-fallback").glob("*.png")), []
                )
        finally:
            for sock in sockets:
                sock.close()

    async def test_camera_external_tool_requires_approval_and_captures(self) -> None:
        output_dir = self.root / "camera"
        server = await self.start_server(
            camera_output_dir=output_dir,
            ffmpeg_bin=str(self._fake_ffmpeg()),
            ffprobe_bin=None,
        )
        with mock.patch("engine.akid._is_video_device", return_value=True):
            await wait_until(lambda: server._external_tools_registered)
            client = await self.client()
            await client.hello()
            turn = await client.call("aki.chat.send", {"message": "EXTERNAL_CAMERA"})
            approval = await client.wait_event(turn["turn_id"], "approval_request")
            self.assertEqual(approval["data"]["tool_name"], "camera_take_photo")
            self.assertEqual(approval["data"]["channel"], "camera")
            await client.call(
                "aki.approval.respond",
                {"approval_id": approval["data"]["approval_id"], "decision": "allow"},
            )
            events = await client.collect_turn(turn["turn_id"])
            final = next(event for event in events if event["type"] == "assistant_message")
            self.assertIn("photo captured path=", final["data"]["content"])
        files = list(output_dir.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].read_bytes().startswith(b"\xff\xd8\xff"))
        self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    async def test_camera_external_tool_deny_does_not_capture(self) -> None:
        output_dir = self.root / "camera-denied"
        server = await self.start_server(
            camera_output_dir=output_dir,
            ffmpeg_bin=str(self._fake_ffmpeg()),
            ffprobe_bin=None,
        )
        with mock.patch("engine.akid._is_video_device", return_value=True):
            await wait_until(lambda: server._external_tools_registered)
            client = await self.client()
            await client.hello()
            turn = await client.call("aki.chat.send", {"message": "EXTERNAL_CAMERA"})
            approval = await client.wait_event(turn["turn_id"], "approval_request")
            await client.call(
                "aki.approval.respond",
                {
                    "approval_id": approval["data"]["approval_id"],
                    "decision": "deny",
                    "message": "not now",
                },
            )
            events = await client.collect_turn(turn["turn_id"])
            final = next(event for event in events if event["type"] == "assistant_message")
            self.assertIn("camera photo not taken", final["data"]["content"])
        self.assertEqual(list(output_dir.glob("*.jpg")), [])

    async def test_mqtt_set_persists_and_reconnects(self) -> None:
        broker1 = FakeMqttBroker()
        broker2 = FakeMqttBroker()
        await broker1.start()
        await broker2.start()
        state = self.root / "mqtt.json"
        server = await self.start_server(
            mqtt_enabled=True,
            mqtt_host="127.0.0.1",
            mqtt_port=broker1.port,
            mqtt_state=state,
        )
        try:
            await wait_until(lambda: server.channel is not None and server.channel.connected)
            client = await self.client()
            await client.hello()
            before = await client.call("aki.mqtt.status", {})
            self.assertEqual(before["port"], broker1.port)
            changed = await client.call(
                "aki.mqtt.set", {"host": "127.0.0.1", "port": broker2.port}
            )
            self.assertEqual(changed["host"], "127.0.0.1")
            self.assertEqual(changed["port"], broker2.port)
            self.assertTrue(changed["connected"])
            self.assertTrue(changed["persisted"])
            saved = json.loads(state.read_text())
            self.assertEqual(saved["host"], "127.0.0.1")
            self.assertEqual(saved["port"], broker2.port)
            await wait_until(lambda: server.channel is not None and server.channel.connected)
        finally:
            await server.stop()
            self.server = None
            await broker1.stop()
            await broker2.stop()


if __name__ == "__main__":
    unittest.main()
