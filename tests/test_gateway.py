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

from engine.akid import (
    Approval,
    ChannelTurnState,
    GatewayConfig,
    GatewayServer,
    Turn,
    _b64url,
    _format_channel_message_time,
    _normalize_mcp_result,
    unique_capture_path,
)
from engine.aki_config import ImageGeneration
from engine.letta_process import LettaProcess
from engine.execution_policy import WAIT_OTHER_TOOL
from engine.live_context import (
    ChannelImageAttachment,
    WindowInfo,
    estimate_tokens,
    render_channel_policy,
    render_channel_turn,
)
from engine.protocol import ErrorCode, MAX_LINE_BYTES, RpcError, make_event
from engine.tokifactor import ImageEnvelope, TextEnvelope
from engine.member_identity import legacy_member_id
from tests.fake_mqtt_broker import FakeMqttBroker
from tests.test_image_generation import FakeMiniMaxImageAPI

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
        self.control: list[tuple[str, dict[str, str]]] = []
        self.connected = True

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def publish_text(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        if metadata:
            kind = str(metadata.get("kind") or "event")
            self.published.append((kind, text))
            self.events.append({"text": text, **metadata})
        else:
            self.published.append(("text", text))

    async def publish_message(
        self,
        text: str,
        images: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = f"fake-message-{len(self.images) + 1}"
        results = []
        for index, image in enumerate(images):
            result = await self.publish_image(
                bytes(image["data"]),
                caption=str(image.get("name") or ""),
                mime=str(image.get("mime") or ""),
            )
            result.update(
                {
                    "transfer_id": f"fake-transfer-{len(self.images)}",
                    "message_id": message_id,
                    "name": str(image.get("name") or ""),
                    "attachment_index": index,
                    "attachment_count": len(images),
                }
            )
            results.append(result)
        if text.strip() or not results:
            await self.publish_text(text, metadata, message_id=message_id)
        return {"message_id": message_id, "images": results}

    async def publish_notice(self, text: str) -> None:
        self.published.append(("notice", text))

    async def publish_control(self, text: str, metadata: dict[str, str], **_: object) -> None:
        payload = {str(key): str(value) for key, value in metadata.items()}
        self.control.append((text, payload))

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


class FakeLayerNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, character: str, text: str) -> bool:
        self.messages.append((character, text))
        return True

    async def stop(self) -> None:
        return


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
        self._window_patch = mock.patch(
            "engine.live_context._collect_window", new=self._fake_window
        )
        self._window_patch.start()

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        if self.server is not None:
            await self.server.stop()
        self._window_patch.stop()
        self.tempdir.cleanup()

    async def _fake_window(self, _options) -> WindowInfo:
        return WindowInfo(title="Fake Window <test>", app="firefox", pid=102)

    async def start_server(
        self,
        *,
        profile: str = "aki",
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
        channel_wait_for: tuple[str, ...] = (),
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
        image_generation: ImageGeneration | None = None,
        live_context_enabled: bool = False,
        live_context_budget_tokens: int = 65536,
        zsh_history_path: Path | None = None,
        live_context_timeout: float = 2.0,
        ps_bin: str = "ps",
        qdbus_bin: str | None = None,
        memory_dir: Path | None = None,
        cwd: Path | None = None,
        backup_dir: Path | None = None,
        mcp_native_tools_enabled: bool = False,
        mcp_native_excluded_servers: tuple[str, ...] = ("local",),
        mcp_native_state: Path | None = None,
        username: str = "Master",
        username_source: str = "default",
        username_path: Path | None = None,
        profile_config_path: Path | None = None,
    ) -> GatewayServer:
        config = GatewayConfig(
            socket_path=self.root / "akid.sock",
            profile=profile,
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
            channel_wait_for=channel_wait_for,
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
            image_generation=image_generation,
            live_context_enabled=live_context_enabled,
            live_context_budget_tokens=live_context_budget_tokens,
            zsh_history_path=zsh_history_path,
            live_context_timeout=live_context_timeout,
            ps_bin=ps_bin,
            qdbus_bin=qdbus_bin,
            mcp_native_tools_enabled=mcp_native_tools_enabled,
            mcp_native_excluded_servers=mcp_native_excluded_servers,
            mcp_native_state=mcp_native_state,
            username=username,
            username_source=username_source,
            username_path=username_path,
            profile_config_path=profile_config_path or (self.root / "aki.conf"),
            execution_lock_path=self.root / "execution.lock",
            execution_lock_status_path=self.root / "execution.lock.json",
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

    async def test_local_turn_notifies_layer_bubble(self) -> None:
        await self.start_server()
        assert self.server is not None
        notifier = FakeLayerNotifier()
        self.server._layer_notifier = notifier
        client = await self.client()
        await client.hello()
        result = await client.call("aki.chat.send", {"message": "hello"})
        events = await client.collect_turn(result["turn_id"])
        final = next(event for event in events if event["type"] == "assistant_message")
        self.assertEqual(notifier.messages, [("Aki", final["data"]["content"])])

    async def test_channel_agent_final_notifies_layer_bubble(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        notifier = FakeLayerNotifier()
        server._layer_notifier = notifier
        member_id = legacy_member_id("Fuyu")
        server._channel_members[member_id] = {
            "id": member_id,
            "name": "Fuyu",
            "aliases": ["Fuyu", "冬"],
            "kind": "agent",
            "last_seen": time.monotonic(),
            "sources": {},
        }
        await server._on_channel_text(
            "Fuyu",
            member_id,
            "代码我看过了。",
            {
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "final",
                "turnId": "turn-fuyu",
                "wake": "user",
            },
            message_id="message-fuyu",
        )
        self.assertEqual(notifier.messages, [("Fuyu", "代码我看过了。")])

    async def test_channel_final_publish_notifies_layer_bubble(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        notifier = FakeLayerNotifier()
        server._layer_notifier = notifier
        server.channel_conn._ensure_worker()
        server.channel_conn._enqueue_turn("turn-final", 1, "final", "final reply")
        await wait_until(lambda: bool(notifier.messages))
        self.assertEqual(notifier.messages, [("Aki", "final reply")])
        self.assertEqual(fake_channel.final_texts(), ["final reply"])

    async def test_internal_task_notification_result_does_not_end_turn(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        result = await client.call("aki.chat.send", {"message": "INTERNAL_RESULT_RACE"})
        events = await client.collect_turn(result["turn_id"])
        self.assertEqual([event["type"] for event in events].count("turn_end"), 1)
        self.assertEqual(events[-1]["data"]["reason"], "completed")
        assistant = next(
            event for event in events if event["type"] == "assistant_message"
        )
        self.assertIn("real reply", assistant["data"]["content"])
        self.assertNotIn("internal notification reply", assistant["data"]["content"])

    async def test_mcp_tool_discovery_builds_native_specs(self) -> None:
        await self.start_server(mcp_native_tools_enabled=False)
        assert self.server is not None

        async def fake_letta_json(args, timeout=20.0, *, label="letta command"):
            if args[:2] == ["mcp", "list"]:
                return [{"name": "tavily", "transport": "stdio"}]
            if args[:3] == ["mcp", "tools", "tavily"]:
                return [
                    {
                        "name": "mcp__tavily__tavily_search",
                        "description": "Search the web",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ]
            raise AssertionError(f"unexpected letta args: {args}")

        with mock.patch.object(
            self.server, "_run_letta_json_value", new=fake_letta_json
        ):
            await self.server._discover_mcp_tools()

        names = [tool["name"] for tool in self.server._external_tools()]
        self.assertIn("mcp__tavily__tavily_search", names)
        tool = next(
            tool
            for tool in self.server._mcp_tools
            if tool["name"] == "mcp__tavily__tavily_search"
        )
        self.assertEqual(tool["parameters"]["required"], ["query"])
        self.assertIsNone(self.server._mcp_tool_spec({"name": "bad name"}, "tavily"))

    async def test_mcp_native_excludes_local_by_default(self) -> None:
        await self.start_server(
            mcp_native_tools_enabled=False,
            mcp_native_excluded_servers=("local",),
        )
        assert self.server is not None

        async def fake_letta_json(args, timeout=20.0, *, label="letta command"):
            if args[:2] == ["mcp", "list"]:
                return [{"name": "tavily"}, {"name": "local"}]
            if args[:3] == ["mcp", "tools", "tavily"]:
                return [
                    {
                        "name": "mcp__tavily__tavily_search",
                        "description": "Search the web",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            if args[:3] == ["mcp", "tools", "local"]:
                return [
                    {
                        "name": "mcp__local__nmap_scan",
                        "description": "Scan ports",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            raise AssertionError(f"unexpected letta args: {args}")

        with mock.patch.object(
            self.server, "_run_letta_json_value", new=fake_letta_json
        ):
            await self.server._discover_mcp_tools()

        self.assertEqual(self.server._mcp_native_active_servers, ["tavily"])
        self.assertEqual(
            self.server._mcp_tool_names, {"mcp__tavily__tavily_search"}
        )

    async def test_model_set_switches_among_enabled_models(self) -> None:
        from types import SimpleNamespace

        agents = self.root / "backend" / "agents"
        agents.mkdir(parents=True)
        agent_path = agents / "test-agent.json"
        agent_path.write_text(
            json.dumps(
                {
                    "id": "test-agent",
                    "model": "aki/gemini-3.8-flash",
                    "model_settings": {"provider_type": "aki"},
                }
            ),
            encoding="utf-8",
        )
        server = await self.start_server()
        server.config.aki = SimpleNamespace(
            enabled_models=(
                SimpleNamespace(
                    model_id="gemini-3.8-flash",
                    upstream_name="openrouter",
                    upstream_model="google/gemini-3.8-flash",
                    context_window=1_000_000,
                    max_tokens=65_536,
                ),
                SimpleNamespace(
                    model_id="deepseek-flash",
                    upstream_name="deepseek",
                    upstream_model="deepseek-flash",
                    context_window=256_000,
                    max_tokens=32_768,
                ),
            )
        )
        client = await self.client()
        await client.hello()
        listed = await client.call("aki.model.get", {})
        self.assertEqual(listed["model"], "aki/gemini-3.8-flash")
        self.assertEqual(
            [item["id"] for item in listed["models"]],
            ["gemini-3.8-flash", "deepseek-flash"],
        )
        changed = await client.call("aki.model.set", {"model": "deepseek-flash"})
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["model"], "aki/deepseek-flash")
        self.assertTrue(changed["backend_restarted"])
        saved = json.loads(agent_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["model"], "aki/deepseek-flash")
        self.assertEqual(saved["model_settings"]["provider_type"], "aki")
        self.assertEqual(saved["model_settings"]["context_window_limit"], 256_000)
        self.assertEqual(saved["model_settings"]["max_tokens"], 32_768)
        with self.assertRaises(RpcError):
            await client.call("aki.model.set", {"model": "grok-4.7"})

    async def test_reasoning_set_persists_and_restarts(self) -> None:
        agents = self.root / "backend" / "agents"
        agents.mkdir(parents=True)
        agent_path = agents / "test-agent.json"
        agent_path.write_text(
            json.dumps(
                {
                    "id": "test-agent",
                    "model": "aki/gemini-3.8-flash",
                    "model_settings": {"reasoning_effort": "high"},
                }
            ),
            encoding="utf-8",
        )
        await self.start_server()
        client = await self.client()
        await client.hello()
        current = await client.call("aki.reasoning.get", {})
        self.assertEqual(current["effort"], "high")
        self.assertEqual(current["default"], "low")
        self.assertIn("low", current["allowed"])
        changed = await client.call("aki.reasoning.set", {"effort": "low"})
        self.assertTrue(changed["changed"])
        self.assertTrue(changed["backend_restarted"])
        self.assertEqual(changed["effort"], "low")
        saved = json.loads(agent_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["model_settings"]["reasoning_effort"], "low")
        self.assertEqual(saved["model_settings"]["reasoning"]["reasoning_effort"], "low")
        with self.assertRaises(RpcError):
            await client.call("aki.reasoning.set", {"effort": "max"})

    async def test_mcp_native_local_toggle_persists_and_restarts(self) -> None:
        state_path = self.root / "mcp-native.json"
        server = await self.start_server(
            mcp_native_tools_enabled=False,
            mcp_native_state=state_path,
        )
        client = await self.client()
        await client.hello()

        status = await client.call("aki.mcp.status", {})
        self.assertIn("local", status["excluded_servers"])

        server.config.mcp_native_tools_enabled = True
        restart_result = {
            "backend_restarted": True,
            "interrupted_turn_id": None,
            "backend_ready": True,
            "external_tools_pending": False,
        }
        with mock.patch.object(
            server,
            "_restart_backend_for_mcp_change",
            new=mock.AsyncMock(return_value=restart_result),
        ) as restart:
            enabled = await client.call(
                "aki.mcp.set", {"server": "local", "enabled": True}
            )
        restart.assert_awaited_once()
        self.assertTrue(enabled["changed"])
        self.assertNotIn("local", enabled["status"]["excluded_servers"])
        from engine.aki_config import read_mcp_excluded

        self.assertEqual(read_mcp_excluded(state_path), ())

        with mock.patch.object(
            server,
            "_restart_backend_for_mcp_change",
            new=mock.AsyncMock(return_value=restart_result),
        ):
            disabled = await client.call(
                "aki.mcp.set", {"server": "local", "enabled": False}
            )
        self.assertTrue(disabled["changed"])
        self.assertIn("local", disabled["status"]["excluded_servers"])
        self.assertEqual(read_mcp_excluded(state_path), ("local",))

    async def test_native_mcp_tool_call_uses_external_tool_path(self) -> None:
        server = await self.start_server(mcp_native_tools_enabled=False)
        server._mcp_tools = [
            {
                "name": "mcp__tavily__tavily_search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        server._mcp_tool_names = {"mcp__tavily__tavily_search"}
        client = await self.client()
        await client.hello()

        with mock.patch.object(
            server,
            "_call_mcp_tool",
            new=mock.AsyncMock(return_value=("native search result", False)),
        ) as call_mcp:
            result = await client.call("aki.chat.send", {"message": "NATIVE_MCP_TOOL"})
            events = await client.collect_turn(result["turn_id"])

        call_mcp.assert_awaited_once_with(
            "mcp__tavily__tavily_search", {"query": "native search"}
        )
        tool_calls = [event for event in events if event["type"] == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(
            tool_calls[0]["data"]["name"], "mcp__tavily__tavily_search"
        )
        tool_result = next(
            event for event in events if event["type"] == "tool_result"
        )
        self.assertEqual(tool_result["data"]["status"], "success")
        self.assertEqual(tool_result["data"]["tool_return"], "native search result")
        assistant = next(
            event for event in events if event["type"] == "assistant_message"
        )
        self.assertIn("native search result", assistant["data"]["content"])

    def test_mcp_result_normalization(self) -> None:
        text, is_error = _normalize_mcp_result(
            json.dumps({"content": [{"type": "text", "text": "hello"}]})
        )
        self.assertEqual((text, is_error), ("hello", False))
        text, is_error = _normalize_mcp_result(
            json.dumps({"content": {"type": "text", "text": "object form"}})
        )
        self.assertEqual((text, is_error), ("object form", False))
        text, is_error = _normalize_mcp_result(
            json.dumps({"error": {"message": "boom"}})
        )
        self.assertEqual((text, is_error), ("boom", True))
        text, is_error = _normalize_mcp_result("plain text")
        self.assertEqual((text, is_error), ("plain text", False))

    async def test_conversation_compact_uses_official_runner(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        fake_result = {
            "compacted": True,
            "mode": "all",
            "num_messages_before": 12,
            "num_messages_after": 3,
            "summary": "condensed",
            "output": "Compaction completed (mode: all).",
            "reflection": "completed",
            "reflection_launched": True,
            "reflection_state_changed": True,
        }
        with mock.patch(
            "engine.akid.run_letta_compact", return_value=fake_result
        ) as runner:
            result = await client.call(
                "aki.conversation.compact", {"mode": "all", "timeout": 120}
            )
        self.assertTrue(result["compacted"])
        self.assertTrue(result["backend_ready"])
        self.assertEqual(result["conversation_id"], "default")
        self.assertEqual(result["reflection"], "completed")
        self.assertEqual(result["mode"], "all")
        runner.assert_called_once()
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["agent_id"], "test-agent")
        self.assertEqual(kwargs["conversation_id"], "default")
        self.assertEqual(kwargs["mode"], "all")
        self.assertEqual(kwargs["timeout"], 120.0)

    async def test_conversation_compact_rejects_invalid_mode_and_busy_turn(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        with self.assertRaises(RpcError) as invalid:
            await client.call("aki.conversation.compact", {"mode": "bad"})
        self.assertEqual(invalid.exception.code, ErrorCode.INVALID_PARAMS)

        active = await client.call("aki.chat.send", {"message": "slow"})
        await client.wait_event(active["turn_id"], "turn_started")
        with self.assertRaises(RpcError) as busy:
            await client.call("aki.conversation.compact", {})
        self.assertEqual(busy.exception.code, ErrorCode.COMPACT_FAILED)
        self.assertIn("active or queued", busy.exception.message)
        await client.call("aki.chat.cancel", {"turn_id": active["turn_id"]})

    async def test_username_get_and_set(self) -> None:
        repo = self.root / "repo"
        shutil.copytree(ROOT / "userdata", repo / "userdata")
        (repo / "userdata" / "username.txt").write_text("OldName\n", encoding="utf-8")
        (repo / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "sync-persona.py", repo / "scripts" / "sync-persona.py")
        memfs = self.root / "memfs"
        memfs.mkdir()
        (memfs / "human.md").write_text(
            "---\nname: Human\ndescription: test human\n---\nHuman profile.\n",
            encoding="utf-8",
        )
        conversation_dir = (
            self.root / "backend" / "conversations" / _b64url("default:test-agent")
        )
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "system-prompt.json").write_text(
            json.dumps(
                {
                    "content": (
                        "<human>\n<description>old human</description>\nold\n</human>\n\n"
                        "<memory-policy>\n<description>old policy</description>\nold OldName\n</memory-policy>\n\n"
                        "<persona>\n<description>old persona</description>\nold OldName\n</persona>\n\n"
                        "<memory>\n# Memory\nold\n</memory>"
                    ),
                    "coreMemory": (
                        "<human>\n<description>old human</description>\nold\n</human>\n\n"
                        "<memory-policy>\n<description>old policy</description>\nold OldName\n</memory-policy>\n\n"
                        "<persona>\n<description>old persona</description>\nold OldName\n</persona>\n"
                    ),
                    "compiledAt": "2026-01-01T00:00:00Z",
                    "rawSystemHash": "test",
                    "memfsRevision": "old",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
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
            username="OldName",
            username_source="legacy-userdata",
            username_path=repo / "userdata" / "username.txt",
        )
        client = await self.client()
        await client.hello()
        before = await client.call("aki.username.get", {})
        self.assertEqual(before["username"], "OldName")
        self.assertEqual(before["source"], "legacy-userdata")
        result = await client.call("aki.username.set", {"username": "Master"})
        self.assertTrue(result["changed"])
        self.assertTrue(result["recompiled"])
        self.assertEqual(result["username"], "Master")
        prompt = json.loads((conversation_dir / "system-prompt.json").read_text())
        self.assertIn("Master", prompt["content"])
        self.assertNotIn("OldName", prompt["content"])
        self.assertNotEqual(prompt["compiledAt"], "2026-01-01T00:00:00Z")
        after = await client.call("aki.username.get", {})
        self.assertEqual(after["username"], "Master")
        self.assertIn("Master", (memfs / "persona.md").read_text(encoding="utf-8"))
        config_text = (self.root / "aki.conf").read_text(encoding="utf-8")
        self.assertIn("[gateway]", config_text)
        self.assertIn("username = Master", config_text)
        self.assertEqual(
            (repo / "userdata" / "username.txt").read_text(encoding="utf-8"),
            "OldName\n",
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
        self.server.last_usage = {"context_tokens": 83_364, "prompt_tokens": 76_903}
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
        self.assertIsNone(self.server.last_usage)
        self.assertEqual(self.server._channel_control_metadata()["contextTokens"], "0")

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

    async def test_bash_auto_allowed_when_sandbox_enabled(self) -> None:
        await self.start_server(approval_timeout=2.0)
        assert self.server is not None
        self.server.config.sandbox_enabled = True
        self.server._sandbox_blocked_reason = None
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "dangerous"})
        events = await client.collect_turn(turn["turn_id"])
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
            mcp_native_tools_enabled=False,
            execution_lock_path=self.root / "execution.lock",
            execution_lock_status_path=self.root / "execution.lock.json",
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

    async def test_channel_prompt_is_minimal_and_policy_lives_in_system_prompt(self) -> None:
        agent_path = self._write_test_agent("BASE PROMPT\n")
        server = await self.start_server()
        sent_at = 1_792_942_663_759
        prompt = server._channel_prompt(
            "Acite", "sender-1", "你好", sent_at=sent_at
        )
        self.assertIn("sender: Acite", prompt)
        self.assertIn("message:\n你好", prompt)
        self.assertIn(
            f"message-time: {_format_channel_message_time(sent_at)}", prompt
        )
        self.assertNotIn("automatically published", prompt)
        self.assertNotIn("<channel-policy", prompt)
        self.assertNotIn("<channel-turn", prompt)
        system = self._agent_system(agent_path)
        self.assertIn("<channel-policy", system)
        self.assertIn(
            "你的回复会被自动发布，不要通过工具发送文字消息。若判断本轮无需回复，直接输出 [NO_REPLY]。",
            system,
        )
        self.assertIn(
            "wake-reminder: 当前正在和你说话的是 Master。"
            "用^排除不需要被提醒的成员",
            prompt,
        )
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用", prompt
        )

    async def test_channel_turn_is_written_to_real_system_prompt(self) -> None:
        agent_path = self._write_test_agent("BASE PROMPT\n")
        server = await self.start_server(channel=FakeChannel())
        turn = Turn(
            turn_id="turn-context",
            conn=server.channel_conn,
            message="",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="sender-1",
            channel_message_seq=7,
            channel_sender="Acite",
            channel_text="hello",
        )
        await server._refresh_channel_turn(turn)
        system = self._agent_system(agent_path)
        self.assertIn('<channel-turn version="1" from_agent="false" new="true">', system)
        self.assertIn(
            '<new-message seq="7" sender="Acite" sender_id="sender-1" source="user"',
            system,
        )
        self.assertIn("<text>hello</text>", system)
        self.assertIn("<wake-reminder>用^排除不需要被提醒的成员", system)
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用", system
        )
        self.assertIn("<guidance>", system)
        self.assertIn('remaining="3"', system)
        self.assertIn("resets=\"user-message\"", system)
        prompt = server._channel_prompt("Acite", "sender-1", "hello")
        self.assertNotIn("<channel-turn", prompt)
        await server._refresh_channel_turn(None)
        self.assertNotIn("<channel-turn", self._agent_system(agent_path))

    async def test_wake_routing_is_written_to_real_system_prompt(self) -> None:
        agent_path = self._write_test_agent("BASE PROMPT\n")
        conversation = (
            self.root
            / "backend"
            / "conversations"
            / _b64url("default:test-agent")
        )
        conversation.mkdir(parents=True)
        prompt_path = conversation / "system-prompt.json"
        prompt_path.write_text(
            json.dumps({"content": "BASE SYSTEM PROMPT\n"}) + "\n",
            encoding="utf-8",
        )
        server = await self.start_server()

        agent_system = self._agent_system(agent_path)
        self.assertIn("<channel-wake-routing", agent_system)
        self.assertIn("有 `@成员名 `：只唤醒被 @ 的成员。", agent_system)
        self.assertIn("强制只唤醒Master，不唤醒任何其他成员。", agent_system)
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用。", agent_system
        )
        self.assertIn("无 `@`、有 `^成员名`：不唤醒被 `^` 的成员", agent_system)

        compiled = json.loads(prompt_path.read_text(encoding="utf-8"))["content"]
        self.assertIn("<channel-wake-routing", compiled)
        self.assertIn("有 `@成员名 `：只唤醒被 @ 的成员。", compiled)
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用。", compiled
        )
        self.assertIn("无 `@`、有 `^成员名`：不唤醒被 `^` 的成员", compiled)

        channel_prompt = server._channel_prompt("Acite", "sender-1", "你好")
        self.assertNotIn("<wake-routing>", channel_prompt)
        self.assertNotIn("不希望其他成员注意到这条消息", channel_prompt)
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用", channel_prompt
        )

    async def test_agent_channel_message_uses_transcript_not_user_role(self) -> None:
        agent_path = self._write_test_agent()
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        body = "UNIQUE_AGENT_BODY_4821"
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            body,
            {
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "final",
                "turnId": "foreign-turn",
                "seq": "1",
            },
        )
        await wait_until(lambda: bool(fake_channel.final_texts()))
        published = "\n".join(fake_channel.final_texts())
        self.assertNotIn(body, published)
        self.assertEqual(server._channel_history[0].text, body)
        self.assertTrue(server._channel_history[0].from_agent)
        self.assertEqual(server._channel_history[-1].sender_id, legacy_member_id("Aki"))
        self.assertTrue(server._channel_history[-1].from_agent)
        system = self._agent_system(agent_path)
        self.assertIn("<channel-transcript", system)
        self.assertIn(body, system)
        self.assertIn('new="true"', system)
        self.assertIn('source="agent"', system)

    async def test_agent_progress_and_tool_events_do_not_create_turns(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        for kind, phase in (
            ("turn", "started"),
            ("turn", "progress"),
            ("tool", "running"),
            ("approval", "pending"),
        ):
            await server._on_channel_text(
                "Fuyu",
                "fuyu",
                f"ignored-{kind}-{phase}",
                {"schema": "akid.v1", "kind": kind, "phase": phase},
            )
        await asyncio.sleep(0.1)
        self.assertEqual(len(server._channel_history), 1)
        self.assertEqual(server._channel_history[0].phase, "progress")
        self.assertTrue(server._channel_history[0].from_agent)
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(fake_channel.events, [])

    async def test_final_reply_metadata_declares_wake_scope(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }

        state = ChannelTurnState(text="@Master\n提醒所有人。")
        await server.channel_conn._flush_progress(
            "turn-user-reminder", 1, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 1)
        reminder_event = fake_channel.events_of("turn")[-1]
        self.assertEqual(reminder_event["text"], "提醒所有人。")
        self.assertEqual(reminder_event["wake"], "user")
        self.assertNotIn("wakeIds", reminder_event)

        state = ChannelTurnState(text="@Master\n只提醒你。^Fuyu")
        await server.channel_conn._flush_progress(
            "turn-user-reminder-suppressed", 2, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 2)
        suppressed_event = fake_channel.events_of("turn")[-1]
        self.assertEqual(suppressed_event["text"], "只提醒你。")
        self.assertEqual(suppressed_event["wake"], "user")
        self.assertNotIn("wakeIds", suppressed_event)

        state = ChannelTurnState(text="@Fuyu 只提醒 Fuyu。")
        await server.channel_conn._flush_progress(
            "turn-mentions", 3, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 3)
        mention_event = fake_channel.events_of("turn")[-1]
        self.assertEqual(mention_event["wake"], "mentions")
        self.assertEqual(mention_event["wakeIds"], "fuyu")

        state = ChannelTurnState(text="提醒所有人。")
        await server.channel_conn._flush_progress(
            "turn-all", 4, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 4)
        self.assertEqual(fake_channel.events_of("turn")[-1]["wake"], "all")

    async def test_agent_reply_wake_scope_controls_turns(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        base = {"schema": "akid.v1", "kind": "turn", "phase": "final"}

        await server._on_channel_text(
            "Fuyu", "fuyu", "只提醒用户", {**base, "wake": "user"}
        )
        await asyncio.sleep(0.05)
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(server._channel_history[-1].text, "只提醒用户")

        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "只提醒别人",
            {**base, "wake": "mentions", "wakeIds": "other"},
        )
        await asyncio.sleep(0.05)
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(server._channel_history[-1].text, "只提醒别人")

        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "@Aki 这条提醒你",
            {**base, "wake": "mentions", "wakeIds": "aki"},
        )
        await wait_until(lambda: bool(fake_channel.final_texts()))
        self.assertEqual(
            [entry.text for entry in server._channel_history[:3]],
            ["只提醒用户", "只提醒别人", "@Aki 这条提醒你"],
        )

    async def test_wake_ids_accept_old_name_keys_and_normalize_to_member_id(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        base = {"schema": "akid.v1", "kind": "turn", "phase": "final"}

        await server._on_channel_text(
            "Fuyu",
            "device-fuyu",
            "这条没有 @ 提及，只靠 wakeIds。",
            {**base, "wake": "mentions", "wakeIds": "Aki"},
        )
        await wait_until(lambda: bool(fake_channel.final_texts()))
        self.assertEqual(
            server._channel_history[0].sender_id,
            legacy_member_id("Fuyu"),
        )

    async def test_busy_user_only_reply_is_recorded_without_deferral(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-user-only",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "只提醒用户",
            {"schema": "akid.v1", "kind": "turn", "phase": "final", "wake": "user"},
        )
        self.assertEqual(busy_turn.busy_messages, [])
        self.assertEqual(server._channel_history[-1].text, "只提醒用户")
        server.active = None

    async def test_messages_while_busy_update_transcript_and_queue_only_users(self) -> None:
        agent_path = self._write_test_agent()
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        agent_body = "AGENT_MESSAGE_WHILE_BUSY"
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            agent_body,
            {"schema": "akid.v1", "kind": "turn", "phase": "final"},
        )
        await asyncio.sleep(0.05)
        self.assertEqual(server.queue, [])
        self.assertTrue(server._channel_history[-1].arrived_while_busy)
        system = self._agent_system(agent_path)
        self.assertIn(agent_body, system)
        self.assertIn('during-work="true"', system)

        await server._on_channel_text("alice", "alice-1", "USER_MESSAGE_WHILE_BUSY")
        self.assertEqual(server.queue, [])
        self.assertEqual(len(busy_turn.busy_messages), 1)
        self.assertTrue(server._channel_history[-1].arrived_while_busy)
        self.assertEqual(fake_channel.events, [])
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(len(server.queue), 1)
        self.assertIn("USER_MESSAGE_WHILE_BUSY", server.queue[0].message)
        server.active = None
        server.queue.clear()

    async def test_busy_user_message_is_consumed_by_later_tool_call(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-tool-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        await server._on_channel_text("alice", "alice-1", "seen during tool loop")
        self.assertEqual(len(busy_turn.busy_messages), 1)
        busy_turn.tool_result_events += 1
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_busy_user_message_before_next_api_call_is_consumed(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-pending-call",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        busy_turn.pending_tool_results = 1
        server.active = busy_turn
        await server._on_channel_text("alice", "alice-1", "queued before next call")
        self.assertEqual(
            busy_turn.busy_messages[0]["pending_tool_results_at_arrival"], 1
        )
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_busy_agent_mention_is_deferred_like_user_message(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-mention-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        body = "@Aki 这条必须处理"
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            body,
            {"schema": "akid.v1", "kind": "turn", "phase": "final"},
        )
        self.assertEqual(server.queue, [])
        self.assertEqual(len(busy_turn.busy_messages), 1)
        self.assertTrue(busy_turn.busy_messages[0]["origin_is_agent"])
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(len(server.queue), 1)
        queued = server.queue[0]
        self.assertTrue(queued.origin_is_agent)
        self.assertEqual(queued.channel_text, body)
        self.assertIn(body, queued.message)
        self.assertIn("new-message: true", queued.message)
        self.assertIn('<new-message seq="1"', queued.message)
        self.assertIn('<text>@Aki 这条必须处理</text>', queued.message)
        self.assertNotIn("<channel-turn", queued.message)
        server.active = None
        server.queue.clear()

    async def test_busy_agent_mention_is_consumed_by_later_tool_call(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        busy_turn = Turn(
            turn_id="busy-mention-tool-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "@Aki seen in tool loop",
            {"schema": "akid.v1", "kind": "turn", "phase": "final"},
        )
        busy_turn.tool_result_events += 1
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_wait_other_is_registered_only_when_configured(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        names = {str(tool.get("name") or "") for tool in server._external_tools()}
        self.assertIn(WAIT_OTHER_TOOL, names)

        await server.stop()
        self.server = None
        plain = await self.start_server(channel=FakeChannel())
        plain_names = {str(tool.get("name") or "") for tool in plain._external_tools()}
        self.assertNotIn(WAIT_OTHER_TOOL, plain_names)

    async def test_wait_other_tool_events_are_hidden(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        turn = Turn(
            turn_id="hidden-wait",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"aki"}),
            state="running",
        )
        server.active = turn
        await server._emit_tool_call_once(turn, "call-1", WAIT_OTHER_TOOL, {})
        await server._emit_tool_result_once(
            turn, "call-1", {"status": "success", "tool_return": "hidden"}
        )
        await asyncio.sleep(0.05)
        self.assertEqual(fake_channel.events, [])
        self.assertEqual(turn.tool_names["call-1"], WAIT_OTHER_TOOL)
        server.active = None

    async def test_wait_other_returns_target_final_after_turn_end(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        server._channel_members["aki"] = {
            "id": "aki",
            "name": "Aki",
            "aliases": ("Aki", "aki"),
            "description": "",
            "kind": "agent",
            "last_seen": time.monotonic(),
            "sources": {"aki": time.monotonic()},
        }
        turn = Turn(
            turn_id="wait-final",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"aki"}),
            state="running",
        )
        server.active = turn
        now = int(time.time() * 1000)
        server._observe_channel_turn_event(
            "aki",
            {"schema": "akid.v1", "kind": "turn", "phase": "started", "turnId": "a1"},
            "Aki is working…",
            sent_at=now,
        )
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ), mock.patch("engine.akid.WAIT_OTHER_POLL_INTERVAL", 0.01):
            task = asyncio.create_task(
                server._handle_wait_other_tool(turn, "wait-call")
            )
            await asyncio.sleep(0.02)
            server._observe_channel_turn_event(
                "aki",
                {"schema": "akid.v1", "kind": "turn", "phase": "final", "turnId": "a1"},
                "Aki 的完整回复",
                sent_at=now + 1,
            )
            server._observe_channel_turn_event(
                "aki",
                {"schema": "akid.v1", "kind": "turn", "phase": "ended", "turnId": "a1"},
                "",
                sent_at=now + 2,
            )
            await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(len(captured), 1)
        self.assertIn("[Aki] status=completed", captured[0])
        self.assertIn("Aki 的完整回复", captured[0])
        self.assertIn("aki", turn.waited_agent_ids)
        server.active = None

    async def test_wait_other_reports_offline_target(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        server._channel_members["aki"] = {
            "id": "aki",
            "name": "Aki",
            "aliases": ("Aki", "aki"),
            "description": "",
            "kind": "agent",
            "last_seen": time.monotonic(),
            "sources": {"aki": time.monotonic()},
        }
        turn = Turn(
            turn_id="wait-offline",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"aki"}),
            state="running",
        )
        server.active = turn
        now = int(time.time() * 1000)
        server._observe_channel_turn_event(
            "aki",
            {"schema": "akid.v1", "kind": "turn", "phase": "started", "turnId": "a1"},
            "Aki is working…",
            sent_at=now,
        )
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ), mock.patch("engine.akid.WAIT_OTHER_POLL_INTERVAL", 0.01):
            task = asyncio.create_task(
                server._handle_wait_other_tool(turn, "wait-call")
            )
            await asyncio.sleep(0.02)
            server._channel_members.pop("aki", None)
            await asyncio.wait_for(task, timeout=2.0)
        self.assertIn("[Aki] status=offline", captured[0])
        server.active = None

    async def test_wait_other_idle_target_returns_after_start_grace(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        server._channel_members["aki"] = {
            "id": "aki",
            "name": "Aki",
            "aliases": ("Aki", "aki"),
            "description": "",
            "kind": "agent",
            "last_seen": time.monotonic(),
            "sources": {"aki": time.monotonic()},
        }
        turn = Turn(
            turn_id="wait-idle",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"aki"}),
            state="running",
        )
        server.active = turn
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ), mock.patch("engine.akid.WAIT_OTHER_START_GRACE", 0.05), mock.patch(
            "engine.akid.WAIT_OTHER_POLL_INTERVAL", 0.01
        ):
            await asyncio.wait_for(
                server._handle_wait_other_tool(turn, "wait-call"), timeout=2.0
            )
        self.assertIn("[Aki] status=idle", captured[0])
        server.active = None

    async def test_wait_other_skips_targets_not_in_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        server._channel_members["aki"] = {
            "id": "aki",
            "name": "Aki",
            "aliases": ("Aki", "aki"),
            "description": "",
            "kind": "agent",
            "last_seen": time.monotonic(),
            "sources": {"aki": time.monotonic()},
        }
        turn = Turn(
            turn_id="wait-skip",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"fuyu"}),
            state="running",
        )
        server.active = turn
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            await server._handle_wait_other_tool(turn, "wait-call")
        self.assertIn("WAIT_OTHER_SKIPPED", captured[0])
        server.active = None

    async def test_waited_agent_message_is_not_requeued(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        turn = Turn(
            turn_id="waited-no-requeue",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            state="running",
            waited_agent_ids={"aki"},
        )
        server.active = turn
        turn.busy_messages.append(
            {
                "sender": "Aki",
                "sender_id": "aki",
                "text": "已由 wait_other 返回",
                "seq": 1,
                "origin_is_agent": True,
                "tool_results_at_arrival": 0,
                "pending_tool_results_at_arrival": 0,
                "participants": frozenset({"aki"}),
            }
        )
        server._queue_unhandled_busy_messages(turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_empty_turn_end_events_update_wait_completion_state(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        now = int(time.time() * 1000)
        member_id = server._member_key({}, "Fuyu", "fuyu")
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "Aki final",
            {
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "final",
                "turnId": "fuyu-turn",
            },
            sent_at=now,
        )
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "",
            {
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "ended",
                "turnId": "fuyu-turn",
            },
            sent_at=now + 1,
        )
        state = server._channel_agent_states[member_id]
        self.assertEqual(state.last_completion_phase, "ended")
        self.assertEqual(state.last_final_text, "Aki final")
        self.assertGreater(state.last_completion_seq, 0)

        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "",
            {
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "no_reply",
                "turnId": "fuyu-turn-2",
            },
            sent_at=now + 2,
        )
        state = server._channel_agent_states[member_id]
        self.assertEqual(state.last_completion_phase, "no_reply")
        self.assertEqual(state.last_final_text, "")

    async def test_non_wake_marker_controls_final_wake_scope(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "kind": "agent",
            "last_seen": time.monotonic(),
        }
        state = ChannelTurnState(text="大家都看一下。^Aki")
        await server.channel_conn._flush_progress(
            "turn-non-wake", 1, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 1)
        event = fake_channel.events_of("turn")[-1]
        self.assertEqual(event["text"], "大家都看一下。")
        self.assertEqual(event["wake"], "mentions")
        self.assertEqual(event["wakeIds"], "fuyu")

        state = ChannelTurnState(text="@Fuyu 这条给你。^Fuyu")
        await server.channel_conn._flush_progress(
            "turn-non-wake-empty", 2, state, force=True, phase="final"
        )
        await wait_until(lambda: len(fake_channel.final_texts()) == 2)
        event = fake_channel.events_of("turn")[-1]
        self.assertEqual(event["text"], "@Fuyu 这条给你。")
        self.assertEqual(event["wake"], "mentions")
        self.assertNotIn("wakeIds", event)

    async def test_channel_prompt_includes_agent_body_and_new_message_identity(self) -> None:
        await self.start_server()
        assert self.server is not None
        body = "AGENT_BODY_SHOULD_STAY_IN_TRANSCRIPT"
        prompt = self.server._channel_prompt(
            "Fuyu", "fuyu", body, from_agent=True, seq=9
        )
        self.assertIn(body, prompt)
        self.assertIn("new-message: true", prompt)
        self.assertIn('<new-message seq="9"', prompt)
        self.assertIn('source="agent"', prompt)
        self.assertIn(
            "wake-reminder: 当前正在和你说话的是 Fuyu。"
            "用^排除不需要被提醒的成员",
            prompt,
        )
        self.assertIn(
            "@Master仅在你只对Master说话，无需打扰其他人时使用", prompt
        )
        self.assertNotIn("<channel-turn", prompt)

    async def test_legacy_dynamic_execution_lock_block_is_removed(self) -> None:
        agent_path = self._write_test_agent(
            "BASE PROMPT\n\n<execution-lock authorized=\"true\">old</execution-lock>\n"
        )
        server = await self.start_server()
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "hello"})
        await client.collect_turn(turn["turn_id"])
        system = self._agent_system(agent_path)
        self.assertIn("BASE PROMPT", system)
        self.assertNotIn("<execution-lock", system)

    async def test_channel_execution_lock_authorization_matrix(self) -> None:
        agent_path = self._write_test_agent()
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu", "冬"),
            "description": "",
            "last_seen": time.monotonic(),
        }
        self.assertEqual(
            server._channel_lock_authorization("do this", from_agent=False),
            (False, "user-must-name-executor"),
        )
        self.assertEqual(
            server._channel_lock_authorization("@Aki do this", from_agent=False),
            (True, "user-mentioned"),
        )
        self.assertEqual(
            server._channel_lock_authorization("@Fuyu do this", from_agent=False),
            (False, "mentioned-other-members"),
        )
        self.assertEqual(
            server._channel_lock_authorization("@Aki do this", from_agent=True),
            (False, "agent-message"),
        )
        await server._on_channel_text("alice", "alice-1", "do this")
        await wait_until(lambda: bool(fake_channel.final_texts()))
        system = self._agent_system(agent_path)
        self.assertNotIn("<execution-lock", system)
        prompt = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message="do this",
            user_name=server.config.username,
        )
        self.assertIn(
            f'<execution-ownership responder="{legacy_member_id("Aki")}" self="true">',
            prompt,
        )
        self.assertIn("负责确认执行者的唯一成员", prompt)

    async def test_single_channel_member_may_acquire_lock_without_mention(self) -> None:
        await self.start_server()
        assert self.server is not None
        self.assertEqual(
            self.server._channel_lock_authorization("do this", from_agent=False),
            (True, "single-online-member"),
        )

    async def test_solo_mode_authorizes_execution_lock_without_mention(self) -> None:
        await self.start_server()
        assert self.server is not None
        from engine.solo_mode import SoloSession

        self.server._solo_session = SoloSession(
            session_id="solo-lock",
            target_id=self.server._channel_self_key(),
            target_name="Aki",
            profile="aki",
            started_at="2026-10-25T00:00:00Z",
            group_magic="group-magic",
        )
        self.assertEqual(
            self.server._channel_lock_authorization("do this", from_agent=False),
            (True, "solo-mode"),
        )
        self.assertEqual(
            self.server._channel_lock_authorization("do this", from_agent=True),
            (True, "solo-mode"),
        )

    async def test_execution_lock_tools_acquire_and_auto_release(self) -> None:
        await self.start_server()
        assert self.server is not None
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "LOCK_ACQUIRE"})
        events = await client.collect_turn(turn["turn_id"])
        text = "".join(
            str(event["data"].get("text") or "")
            for event in events
            if event["type"] == "assistant_message"
        )
        self.assertIn("EXECUTION_LOCK_ACQUIRED", text)
        await wait_until(lambda: not self.server.execution_lock.status()["held"])

        turn = await client.call("aki.chat.send", {"message": "LOCK_ACQUIRE_RELEASE"})
        events = await client.collect_turn(turn["turn_id"])
        text = "".join(
            str(event["data"].get("text") or "")
            for event in events
            if event["type"] == "assistant_message"
        )
        self.assertIn("EXECUTION_LOCK_ACQUIRED", text)
        self.assertIn("EXECUTION_LOCK_RELEASED", text)
        self.assertFalse(self.server.execution_lock.status()["held"])

    async def test_execution_lock_contention_returns_owner_and_ends_turn(self) -> None:
        await self.start_server()
        assert self.server is not None
        holder = self.server.execution_lock
        acquired, _ = holder.acquire(
            profile="fuyu", agent_id="fuyu-agent", turn_id="foreign-turn"
        )
        self.assertTrue(acquired)
        try:
            client = await self.client()
            await client.hello()
            turn = await client.call("aki.chat.send", {"message": "LOCK_ACQUIRE"})
            events = await client.collect_turn(turn["turn_id"])
            text = "".join(
                str(event["data"].get("text") or "")
                for event in events
                if event["type"] == "assistant_message"
            )
            self.assertIn("EXECUTION_LOCK_UNAVAILABLE", text)
            self.assertIn("fuyu", text)
            self.assertTrue(holder.status()["held"])
        finally:
            holder.release(profile="fuyu", turn_id="foreign-turn")

    async def test_execution_lock_is_released_when_turn_is_cancelled(self) -> None:
        await self.start_server()
        assert self.server is not None
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "LOCK_ACQUIRE_SLOW"})
        await wait_until(lambda: self.server.execution_lock.status()["held"])
        await client.call("aki.chat.cancel", {"turn_id": turn["turn_id"]})
        await wait_until(lambda: not self.server.execution_lock.status()["held"])

    async def test_unlocked_non_read_only_tool_is_denied(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "unlocked-bash"})
        events = await client.collect_turn(turn["turn_id"])
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "error")
        self.assertIn("EXECUTION_LOCK_REQUIRED", str(tool_result["data"]["tool_return"]))

    async def test_release_tool_is_auto_denied_without_approval(self) -> None:
        await self.start_server()
        client = await self.client()
        await client.hello()
        turn = await client.call("aki.chat.send", {"message": "release-tool"})
        events = await client.collect_turn(turn["turn_id"])
        self.assertNotIn(
            "approval_request", [event["type"] for event in events]
        )
        tool_result = next(event for event in events if event["type"] == "tool_result")
        self.assertEqual(tool_result["data"]["status"], "error")
        self.assertIn(
            "manual lock release is not available",
            str(tool_result["data"]["tool_return"]),
        )

    async def test_user_channel_message_stays_user_and_enters_transcript(self) -> None:
        agent_path = self._write_test_agent()
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        body = "USER_BODY_STAYS_IN_USER_ROLE"
        await server._on_channel_text("alice", "alice-1", body)
        await wait_until(lambda: bool(fake_channel.final_texts()))
        self.assertIn(body, server._channel_prompt("alice", "alice-1", body))
        self.assertEqual(server._channel_history[0].text, body)
        self.assertFalse(server._channel_history[0].from_agent)
        system = self._agent_system(agent_path)
        self.assertIn('source="user"', system)
        self.assertIn('new="true"', system)
        client = await self.client()
        await client.hello()
        local = await client.call("aki.chat.send", {"message": "local hello"})
        await client.collect_turn(local["turn_id"])
        local_system = self._agent_system(agent_path)
        self.assertNotIn("<channel-transcript", local_system)
        self.assertIn("<channel-identity", local_system)
        self.assertIn('kind="agent"', local_system)

    async def test_human_member_kind_does_not_count_as_agent(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["master"] = {
            "id": "master",
            "name": "Master",
            "aliases": (),
            "description": "human bio",
            "kind": "user",
            "last_seen": time.monotonic(),
        }
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "agent bio",
            "kind": "agent",
            "last_seen": time.monotonic(),
        }
        self.assertFalse(server._channel_sender_is_agent("master"))
        self.assertTrue(server._channel_sender_is_agent("fuyu"))
        self.assertEqual(
            server._channel_lock_authorization("do this", from_agent=False),
            (False, "user-must-name-executor"),
        )
        await server._on_channel_text("Master", "master", "hello from a human")
        self.assertFalse(server._channel_history[-1].from_agent)
        prompt = server._channel_prompt("Master", "master", "hello from a human")
        self.assertIn('kind="user"', prompt)
        self.assertIn("human bio", prompt)
        turn = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message="hello from a human",
            user_name=server.config.username,
        )
        self.assertIn(f'responder="{legacy_member_id("Aki")}"', turn)

    async def test_control_profile_updates_are_sparse_and_bounded(self) -> None:
        server = await self.start_server()
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "existing bio",
            "kind": "agent",
            "last_seen": time.monotonic(),
        }
        metadata = {
            "schema": "akid.v1",
            "kind": "status",
            "protocolVersion": "1",
            "agentId": "fuyu",
            "agent": "fuyu",
            "agentName": "Fuyu",
        }
        await server._on_channel_control(
            TextEnvelope(sender="Fuyu", text="status", sender_id="fuyu", metadata=metadata)
        )
        self.assertEqual(server._channel_members["fuyu"]["description"], "existing bio")
        self.assertEqual(server._channel_members["fuyu"]["kind"], "agent")

        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="status",
                sender_id="fuyu",
                metadata={**metadata, "description": "中" * 300, "memberKind": "user"},
            )
        )
        self.assertEqual(server._channel_members["fuyu"]["description"], "中" * 256)
        self.assertEqual(server._channel_members["fuyu"]["kind"], "user")

    async def test_channel_transcript_persists_across_server_restart(self) -> None:
        server = await self.start_server()
        first = server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="first persisted message",
            from_agent=False,
        )
        second = server._record_channel_message(
            sender="Fuyu",
            sender_id="fuyu",
            text="second persisted message",
            from_agent=True,
        )
        path = server.channel_history_path
        self.assertTrue(path.is_file())
        self.assertEqual((first.seq, second.seq), (1, 2))

        await server.stop()
        if self.server is server:
            self.server = None

        restarted = await self.start_server()
        self.assertEqual(
            [entry.text for entry in restarted._channel_history],
            ["first persisted message", "second persisted message"],
        )
        self.assertEqual(restarted._channel_history_seq, 2)
        third = restarted._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="third persisted message",
            from_agent=False,
        )
        self.assertEqual(third.seq, 3)

    async def test_channel_transcript_is_isolated_by_mqtt_channel(self) -> None:
        server = await self.start_server()
        original_topic = server.config.mqtt_topic
        first_path = server.channel_history_path
        server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="topic one message",
            from_agent=False,
        )

        server.config.mqtt_topic = f"{original_topic}-other"
        server._load_channel_history()
        self.assertNotEqual(server.channel_history_path, first_path)
        self.assertEqual(server._channel_history, [])
        server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="topic two message",
            from_agent=False,
        )

        server.config.mqtt_topic = original_topic
        server._load_channel_history()
        self.assertEqual(
            [entry.text for entry in server._channel_history],
            ["topic one message"],
        )

    async def test_channel_conversation_snapshot_rolls_back_turn_messages(self) -> None:
        await self.start_server()
        assert self.server is not None
        directory = self.server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text('{"type":"session"}\n', encoding="utf-8")
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        original_messages = messages.read_bytes()
        original_conversation = conversation.read_bytes()
        snapshot = self.server._snapshot_conversation()
        self.assertIsNotNone(snapshot)
        with messages.open("ab") as handle:
            handle.write(b'{"type":"message","message":{"content":"wake"}}\n')
        conversation.write_text('{"id":"default","last_message_at":"now"}\n', encoding="utf-8")
        await self.server._restore_conversation_snapshot(snapshot)
        self.assertEqual(messages.read_bytes(), original_messages)
        self.assertEqual(conversation.read_bytes(), original_conversation)
        self.assertTrue(await self.server.backend.wait_ready(5.0))
        self.assertEqual(
            self.server._channel_transcript_status.get("ephemeral_turns"), 1
        )

    async def test_user_channel_turn_persists_conversation_messages(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        directory = server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text('{"type":"session"}\n', encoding="utf-8")
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        marker = b'{"type":"message","message":{"role":"user","content":"persisted"}}\n'
        original_send_user = server.backend.send_user

        async def append_and_send(content: str) -> None:
            with messages.open("ab") as handle:
                handle.write(marker)
            await original_send_user(content)

        with mock.patch.object(
            server.backend,
            "send_user",
            side_effect=append_and_send,
        ):
            await server._on_channel_text("alice", "alice-1", "persistent channel turn")
            await wait_until(lambda: bool(fake_channel.final_texts()))
            await wait_until(lambda: server.active is None and not server.queue)

        self.assertIn(marker, messages.read_bytes())
        self.assertEqual(
            server._channel_transcript_status.get("ephemeral_turns", 0), 0
        )
        self.assertTrue(await server.backend.wait_ready(5.0))

    async def test_user_channel_turn_strips_roster_after_completion(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        directory = server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text('{"type":"session"}\n', encoding="utf-8")
        conversation.write_text(
            json.dumps(
                {
                    "id": "default",
                    "agent_id": "test-agent",
                    "updated_at": "2026-01-01T00:00:00.000Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        prompts: list[str] = []
        original_send_user = server.backend.send_user

        async def append_and_send(content: str) -> None:
            prompt = content if isinstance(content, str) else ""
            prompts.append(prompt)
            self.assertIn("<channel-context>", prompt)
            row = {
                "type": "message",
                "id": f"entry-{len(prompts)}",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "message": {
                    "id": f"ui-msg-{len(prompts)}",
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                },
            }
            with messages.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
            await original_send_user(content)

        with mock.patch.object(
            server.backend,
            "send_user",
            side_effect=append_and_send,
        ):
            await server._on_channel_text("alice", "alice-1", "first turn")
            await wait_until(lambda: len(fake_channel.final_texts()) >= 1)
            await wait_until(lambda: server.active is None and not server.queue)
            await server._on_channel_text("alice", "alice-1", "second turn")
            await wait_until(lambda: len(fake_channel.final_texts()) >= 2)
            await wait_until(lambda: server.active is None and not server.queue)

        self.assertEqual(len(prompts), 2)
        rows = [
            json.loads(line)
            for line in messages.read_text(encoding="utf-8").splitlines()[1:]
        ]
        self.assertEqual(len(rows), 2)
        for row in rows:
            text = row["message"]["content"][0]["text"]
            self.assertNotIn("<channel-context>", text)
            self.assertIn("当前正在和你说话的是 Master。", text)
        self.assertIn("message:\nfirst turn", rows[0]["message"]["content"][0]["text"])
        self.assertIn("message:\nsecond turn", rows[1]["message"]["content"][0]["text"])
        record = json.loads(conversation.read_text(encoding="utf-8"))
        self.assertNotEqual(record["updated_at"], "2026-01-01T00:00:00.000Z")
        journal = server.channel_prompt_journal_path
        self.assertTrue(journal.is_file())
        self.assertEqual(
            len(
                [
                    json.loads(line)
                    for line in journal.read_text(encoding="utf-8").splitlines()
                ]
            ),
            2,
        )

    async def test_agent_channel_turn_uses_ephemeral_conversation_snapshot(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        directory = server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text('{"type":"session"}\n', encoding="utf-8")
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        original_messages = messages.read_bytes()
        original_conversation = conversation.read_bytes()
        original_send_user = server.backend.send_user

        async def append_and_send(content: str) -> None:
            with messages.open("ab") as handle:
                handle.write(
                    b'{"type":"message","message":{"role":"user","content":"ephemeral"}}\n'
                )
            await original_send_user(content)

        with mock.patch.object(
            server.backend,
            "send_user",
            side_effect=append_and_send,
        ):
            await server._on_channel_text(
                "Fuyu",
                "fuyu",
                "ephemeral channel turn",
                {"schema": "akid.v1", "kind": "turn", "phase": "final"},
            )
            await wait_until(
                lambda: int(
                    server._channel_transcript_status.get("ephemeral_turns", 0)
                )
                >= 1
            )
            await wait_until(lambda: server.active is None and not server.queue)
        self.assertEqual(messages.read_bytes(), original_messages)
        self.assertEqual(conversation.read_bytes(), original_conversation)
        self.assertTrue(await server.backend.wait_ready(5.0))

    async def test_channel_image_and_text_share_one_history_entry_and_multimodal_turn(
        self,
    ) -> None:
        fake_channel = FakeChannel()
        last_input = self.root / "last-input.json"
        with mock.patch.dict(
            os.environ, {"FAKE_LETTA_LAST_INPUT": str(last_input)}, clear=False
        ):
            server = await self.start_server(channel=fake_channel)
            await server._on_channel_image(
                ImageEnvelope(
                    sender="alice",
                    sender_id="alice-1",
                    message_id="mixed-1",
                    name="图片1",
                    attachment_index=0,
                    attachment_count=1,
                    data=PNG_1X1,
                    mime="image/png",
                    transfer_id="transfer-1",
                )
            )
            await server._on_channel_text(
                "alice",
                "alice-1",
                "看看图片1的设计",
                {"attachmentCount": "1"},
                message_id="mixed-1",
            )
            await wait_until(lambda: bool(fake_channel.final_texts()))
            await wait_until(lambda: server.active is None and not server.queue)

        entry = next(
            entry for entry in reversed(server._channel_history)
            if entry.text == "看看图片1的设计"
        )
        self.assertEqual(len(entry.attachments), 1)
        self.assertEqual(entry.attachments[0].name, "图片1")
        self.assertTrue(Path(entry.attachments[0].path).is_file())
        raw = json.loads(last_input.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list)
        self.assertEqual(raw[0]["type"], "text")
        self.assertIn("图片1", raw[0]["text"])
        self.assertEqual(raw[1]["type"], "image")
        self.assertEqual(raw[1]["source"]["media_type"], "image/png")
        self.assertGreater(len(raw[1]["source"]["data"]), 20)
        rendered = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message=entry.text,
            message_seq=entry.seq,
            message_sender=entry.sender,
            message_sender_id=entry.sender_id,
            message_attachments=entry.attachments,
        )
        self.assertIn('"name":"图片1"', rendered)
        self.assertIn('"path":', rendered)
        self.assertNotIn(raw[1]["source"]["data"], rendered)

    async def test_delayed_text_commit_does_not_prematurely_record_image_only(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        await server._on_channel_image(
            ImageEnvelope(
                sender="alice",
                sender_id="alice-1",
                message_id="delayed-text-1",
                name="图片1",
                attachment_index=0,
                attachment_count=1,
                data=PNG_1X1,
                mime="image/png",
                transfer_id="delayed-transfer-1",
                expect_text=True,
            )
        )
        await asyncio.sleep(1.2)
        self.assertFalse(
            any(entry.message_id == "delayed-text-1" for entry in server._channel_history),
            "image-only message was finalized before the text commit arrived",
        )
        await server._on_channel_text(
            "alice",
            "alice-1",
            "带图的正文",
            {"attachmentCount": "1"},
            message_id="delayed-text-1",
        )
        await wait_until(
            lambda: any(
                entry.message_id == "delayed-text-1" for entry in server._channel_history
            )
        )
        entry = next(
            entry for entry in server._channel_history if entry.message_id == "delayed-text-1"
        )
        self.assertEqual(entry.text, "带图的正文")
        self.assertEqual(len(entry.attachments), 1)

    async def test_image_only_channel_message_is_recorded_without_turn(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        await server._on_channel_image(
            ImageEnvelope(
                sender="alice",
                sender_id="alice-1",
                message_id="image-only-1",
                name="草图",
                attachment_index=0,
                attachment_count=1,
                data=PNG_1X1,
                mime="image/png",
                transfer_id="transfer-only",
            )
        )
        await wait_until(
            lambda: any(
                entry.attachments and entry.attachments[0].name == "草图"
                for entry in server._channel_history
            ),
            timeout=3.0,
        )
        self.assertIsNone(server.active)
        self.assertFalse(server.queue)
        entry = next(
            entry for entry in server._channel_history if entry.attachments
        )
        self.assertEqual(entry.text, "")
        self.assertTrue(Path(entry.attachments[0].path).is_file())

    async def test_channel_history_persists_attachment_paths_and_names(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        image_path = self.root / "persisted.png"
        image_path.write_bytes(PNG_1X1)
        server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="看看图片1",
            from_agent=False,
            message_id="persisted-message",
            attachments=(
                ChannelImageAttachment(
                    name="图片1",
                    path=str(image_path),
                    mime="image/png",
                    size=len(PNG_1X1),
                    index=1,
                ),
            ),
        )
        server._channel_history = []
        server._load_channel_history()
        entry = server._channel_history[-1]
        self.assertEqual(entry.text, "看看图片1")
        self.assertEqual(entry.attachments[0].name, "图片1")
        self.assertEqual(entry.attachments[0].path, str(image_path))

    async def test_agent_image_directive_becomes_attachment_and_preserves_wake(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        fuyu_id = legacy_member_id("Fuyu")
        server._channel_members[fuyu_id] = {
            "id": fuyu_id,
            "name": "Fuyu",
            "aliases": ("Fuyu", "冬"),
            "kind": "agent",
            "last_seen": time.monotonic(),
        }
        image_path = self.root / "agent-image.png"
        image_path.write_bytes(PNG_1X1)
        cleaned, extracted = await server._extract_agent_channel_images(
            f"看看这张图。#[{image_path}] @Fuyu 你好"
        )
        self.assertEqual(cleaned, "看看这张图。 @Fuyu 你好")
        self.assertEqual(len(extracted), 1)
        server.channel_conn._ensure_worker()
        await server.channel_conn._flush_progress(
            "turn-agent-image",
            9,
            ChannelTurnState(text=f"看看这张图。#[{image_path}] @Fuyu 你好"),
            force=True,
            phase="final",
        )
        await wait_until(lambda: bool(fake_channel.images))
        await wait_until(lambda: bool(fake_channel.final_texts()))
        event = fake_channel.events_of("turn")[-1]
        self.assertEqual(event["text"], "看看这张图。 @Fuyu 你好")
        self.assertEqual(event["wake"], "mentions")
        self.assertEqual(event["wakeIds"], fuyu_id)
        self.assertEqual(fake_channel.images[0]["caption"], "agent-image.png")
        entry = server._channel_history[-1]
        self.assertEqual(entry.attachments[0].name, "agent-image.png")
        self.assertTrue(Path(entry.attachments[0].path).is_file())

    async def test_agent_image_message_wakes_peer_with_multimodal_turn(self) -> None:
        fake_channel = FakeChannel()
        last_input = self.root / "agent-image-input.json"
        fuyu_id = legacy_member_id("Fuyu")
        with mock.patch.dict(
            os.environ, {"FAKE_LETTA_LAST_INPUT": str(last_input)}, clear=False
        ):
            server = await self.start_server(channel=fake_channel)
            server._channel_members[fuyu_id] = {
                "id": fuyu_id,
                "name": "Fuyu",
                "aliases": ("Fuyu", "冬"),
                "kind": "agent",
                "last_seen": time.monotonic(),
            }
            self_id = server._channel_self_key()
            await server._on_channel_image(
                ImageEnvelope(
                    sender="Fuyu",
                    sender_id=fuyu_id,
                    message_id="agent-mixed-1",
                    name="方案图",
                    attachment_index=0,
                    attachment_count=1,
                    data=PNG_1X1,
                    mime="image/png",
                    transfer_id="agent-transfer-1",
                )
            )
            await server._on_channel_text(
                "Fuyu",
                fuyu_id,
                "@Aki 看方案图",
                {
                    "schema": "akid.v1",
                    "kind": "turn",
                    "phase": "final",
                    "turnId": "turn-peer-image",
                    "attachmentCount": "1",
                    "wake": "mentions",
                    "wakeIds": self_id,
                },
                message_id="agent-mixed-1",
            )
            await wait_until(lambda: bool(fake_channel.final_texts()))
            await wait_until(lambda: server.active is None and not server.queue)
        raw = json.loads(last_input.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list)
        self.assertTrue(any(part.get("type") == "image" for part in raw))
        self.assertIn("方案图", raw[0]["text"])

    async def test_ephemeral_rollback_removes_multimodal_base64_messages(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        directory = server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text('{"type":"session"}\n', encoding="utf-8")
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        original_messages = messages.read_bytes()
        original_conversation = conversation.read_bytes()
        original_send_user = server.backend.send_user

        async def append_multimodal_and_send(content: object) -> None:
            with messages.open("ab") as handle:
                handle.write(
                    b'{"type":"message","message":{"role":"user","content":['
                    b'{"type":"text","text":"look"},{"type":"image","source":'
                    b'{"type":"base64","media_type":"image/png","data":"QUJD"}}]}}\n'
                )
            await original_send_user(content)

        with mock.patch.object(
            server.backend, "send_user", side_effect=append_multimodal_and_send
        ):
            await server._on_channel_text(
                "Fuyu",
                "fuyu",
                "ephemeral multimodal",
                {"schema": "akid.v1", "kind": "turn", "phase": "final"},
            )
            await wait_until(
                lambda: int(server._channel_transcript_status.get("ephemeral_turns", 0))
                >= 1
            )
            await wait_until(lambda: server.active is None and not server.queue)
        self.assertEqual(messages.read_bytes(), original_messages)
        self.assertEqual(conversation.read_bytes(), original_conversation)

    async def test_channel_control_probe_presence_dedup_and_rate_limit(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)

        def probe(probe_id: str, sender_id: str, **extra: str) -> TextEnvelope:
            metadata = {
                "schema": "akid.v1",
                "kind": "probe",
                "protocolVersion": "1",
                "probeId": probe_id,
                **extra,
            }
            return TextEnvelope(
                sender="tokifactor",
                text="Aki probe",
                sender_id=sender_id,
                metadata=metadata,
            )

        await server._on_channel_control(probe("probe-1", "gui-1"))
        presences = [
            (text, metadata)
            for text, metadata in fake_channel.control
            if metadata.get("kind") == "presence"
        ]
        self.assertEqual(len(presences), 1)
        text, metadata = presences[0]
        self.assertEqual(text, "Aki is online")
        self.assertEqual(metadata["kind"], "presence")
        self.assertEqual(metadata["agent"], legacy_member_id("Aki"))
        self.assertEqual(metadata["agentName"], "Aki")
        self.assertEqual(metadata["probeId"], "probe-1")
        self.assertEqual(metadata["protocolVersion"], "1")
        self.assertIn("approvals", metadata["capabilities"])
        self.assertIn("model", metadata)
        self.assertIn("reasoning", metadata)
        self.assertIn(metadata["backendReady"], {"true", "false"})
        self.assertEqual(metadata["channelReady"], "true")

        await server._on_channel_control(probe("probe-1", "gui-1"))
        self.assertEqual(
            sum(1 for _text, meta in fake_channel.control if meta.get("kind") == "presence"),
            1,
        )
        await server._on_channel_control(probe("probe-2", "gui-1"))
        self.assertEqual(
            sum(1 for _text, meta in fake_channel.control if meta.get("kind") == "presence"),
            1,
        )
        await server._on_channel_control(probe("probe-3", "gui-2"))
        self.assertEqual(
            sum(1 for _text, meta in fake_channel.control if meta.get("kind") == "presence"),
            2,
        )
        await server._on_channel_control(
            probe("probe-4", "gui-3", protocolVersion="2")
        )
        await server._on_channel_control(
            TextEnvelope(
                sender="tokifactor",
                text="",
                sender_id="gui-3",
                metadata={
                    "schema": "akid.v1",
                    "kind": "probe",
                    "protocolVersion": "1",
                    "probeId": "probe-5",
                },
            )
        )
        self.assertEqual(
            sum(1 for _text, meta in fake_channel.control if meta.get("kind") == "presence"),
            2,
        )
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(fake_channel.events, [])

    async def test_channel_member_roster_and_mentions_are_contextual(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        metadata = {
            "schema": "akid.v1",
            "kind": "status",
            "protocolVersion": "1",
            "agentId": "fuyu",
            "agent": "fuyu",
            "agentName": "Fuyu",
            "description": "Fuyu <untrusted>",
            "aliases": "Fuyu,冬",
            "model": "aki-fuyu/deepseek-flash",
            "reasoning": "low",
            "backendReady": "true",
            "channelReady": "true",
            "queueDepth": "0",
            "activeTurn": "false",
            "pendingApprovals": "0",
        }
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="Fuyu status",
                sender_id="fuyu",
                metadata=metadata,
            )
        )
        await server._on_channel_control(
            TextEnvelope(
                sender="Aki",
                text="self status",
                sender_id="aki",
                metadata={
                    **metadata,
                    "agentId": "aki",
                    "agent": "aki",
                    "agentName": "Aki",
                },
            )
        )
        plain = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message="hello everyone",
            user_name=server.config.username,
        )
        self.assertIn("这是一条群聊消息；你可自行决定是否回复。", plain)
        policy = render_channel_policy(user_name=server.config.username)
        self.assertIn("Master不在成员列表中", policy)
        self.assertNotIn("<wake-routing>", plain)
        self.assertNotIn("不希望其他成员注意到这条消息", plain)
        roster = server._channel_prompt("alice", "alice-1", "hello everyone")
        self.assertIn("Fuyu", roster)
        self.assertIn("&lt;untrusted&gt;", roster)
        other = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message="@冬 hello",
            user_name=server.config.username,
        )
        self.assertIn("点名了其他成员，未点名你", other)
        own = render_channel_turn(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
            message="@秋 hello",
            user_name=server.config.username,
        )
        self.assertIn("Master在消息中点名了你，请直接回复。", own)
        self.assertIn("它会强制只唤醒Master", own)
        self.assertNotIn("未点名你", own)
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="Fuyu offline",
                sender_id="fuyu",
                metadata={**metadata, "kind": "offline"},
            )
        )
        after_offline = server._channel_prompt("alice", "alice-1", "hello")
        self.assertNotIn("Fuyu", after_offline)
        self.assertEqual(server.active, None)
        self.assertEqual(fake_channel.events, [])

    async def test_user_mention_only_starts_mentioned_agent_turns(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu", "冬"),
            "description": "",
            "last_seen": time.monotonic(),
        }
        await server._on_channel_text("alice", "alice-1", "@Fuyu please answer")
        await asyncio.sleep(0.1)
        self.assertEqual(server._channel_history[-1].text, "@Fuyu please answer")
        self.assertFalse(server._channel_history[-1].from_agent)
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(fake_channel.events, [])

        await server._on_channel_text(
            "alice", "alice-1", "@Fuyu @秋 please answer too"
        )
        await wait_until(lambda: bool(fake_channel.events))
        self.assertTrue(fake_channel.events)

    async def test_user_message_without_mentions_wakes_agent(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["qq"] = {
            "id": "qq",
            "name": "QQ",
            "aliases": ("qq",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        body = "我的邮箱是114514@qq.com"
        await server._on_channel_text("alice", "alice-1", body)
        await wait_until(lambda: bool(fake_channel.events))
        self.assertTrue(fake_channel.events)
        user_entry = next(
            entry
            for entry in server._channel_history
            if entry.sender_id == legacy_member_id("alice")
        )
        self.assertEqual(user_entry.text, body)
        self.assertFalse(user_entry.from_agent)

    async def test_user_at_without_trailing_space_does_not_filter_turns(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        await server._on_channel_text("alice", "alice-1", "@Fuyu")
        await wait_until(lambda: bool(fake_channel.events))
        self.assertTrue(fake_channel.events)

    async def test_user_mention_followed_by_punctuation_filters_turns(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        await server._on_channel_text("alice", "alice-1", "@Fuyu，请回答")
        await asyncio.sleep(0.1)
        self.assertIsNone(server.active)
        self.assertEqual(server.queue, [])
        self.assertEqual(fake_channel.events, [])

        await server._on_channel_text("alice", "alice-1", "@秋,请回答")
        await wait_until(lambda: bool(fake_channel.events))
        self.assertTrue(fake_channel.events)

    async def test_busy_user_mention_of_another_agent_is_not_deferred(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        busy_turn = Turn(
            turn_id="busy-other-mention-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            state="running",
        )
        server.active = busy_turn
        await server._on_channel_text("alice", "alice-1", "@Fuyu not for Aki")
        self.assertEqual(busy_turn.busy_messages, [])
        self.assertTrue(server._channel_history[-1].arrived_while_busy)
        server._queue_unhandled_busy_messages(busy_turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_agent_reply_budget_decrements_and_user_message_resets(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        self.assertEqual(server._agent_initiation_remaining, 3)
        await server._on_channel_text("Fuyu", "fuyu", "hello from another agent")
        await wait_until(lambda: bool(fake_channel.final_texts()))
        self.assertEqual(server._agent_initiation_remaining, 2)
        await server._on_channel_text("alice", "alice-1", "hello from user")
        self.assertEqual(server._agent_initiation_remaining, 3)
        await wait_until(lambda: len(fake_channel.final_texts()) >= 2)

    async def test_agent_reply_budget_zero_suppresses_agent_to_agent_reply(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._agent_initiation_remaining = 0
        server._channel_members["fuyu"] = {
            "id": "fuyu",
            "name": "Fuyu",
            "aliases": ("Fuyu",),
            "description": "",
            "last_seen": time.monotonic(),
        }
        await server._on_channel_text("Fuyu", "fuyu", "loop continues")
        await wait_until(lambda: server.active is None)
        await asyncio.sleep(0.1)
        self.assertEqual(fake_channel.events, [])
        self.assertEqual(fake_channel.final_texts(), [])
        self.assertEqual(server._agent_initiation_remaining, 0)

    async def test_agent_initiation_is_charged_at_most_once_per_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        self.assertEqual(server._agent_initiation_remaining, 3)
        conn = server.channel_conn
        turn_id = "turn-budget-once"
        await conn.send(
            make_event(
                turn_id,
                1,
                "turn_started",
                {"origin": "mqtt", "origin_sender_id": "fuyu", "origin_is_agent": True},
                "2026-10-21T00:00:00Z",
            )
        )
        self.assertEqual(server._agent_initiation_remaining, 2)
        await conn.send(
            make_event(
                turn_id,
                2,
                "assistant_message",
                {"content": "first visible message", "text": "first visible message"},
                "2026-10-21T00:00:01Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                3,
                "assistant_message",
                {"content": "second visible message", "text": "second visible message"},
                "2026-10-21T00:00:02Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                4,
                "turn_end",
                {"reason": "completed"},
                "2026-10-21T00:00:03Z",
            )
        )
        await wait_until(lambda: len(fake_channel.final_texts()) >= 2)
        self.assertEqual(server._agent_initiation_remaining, 2)

    async def test_last_initiation_does_not_truncate_the_current_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._agent_initiation_remaining = 1
        conn = server.channel_conn
        turn_id = "turn-last-budget"
        await conn.send(
            make_event(
                turn_id,
                1,
                "turn_started",
                {"origin": "mqtt", "origin_sender_id": "fuyu", "origin_is_agent": True},
                "2026-10-21T00:00:00Z",
            )
        )
        self.assertEqual(server._agent_initiation_remaining, 0)
        await conn.send(
            make_event(
                turn_id,
                2,
                "assistant_message",
                {"content": "first part", "text": "first part"},
                "2026-10-21T00:00:01Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                3,
                "tool_call",
                {"name": "Read", "tool_call_id": "tool-1"},
                "2026-10-21T00:00:02Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                4,
                "assistant_delta",
                {"text": "second part"},
                "2026-10-21T00:00:03Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                5,
                "assistant_message",
                {"content": "second part", "text": "second part"},
                "2026-10-21T00:00:04Z",
            )
        )
        await conn.send(
            make_event(
                turn_id,
                6,
                "turn_end",
                {"reason": "completed"},
                "2026-10-21T00:00:04Z",
            )
        )
        await wait_until(lambda: len(fake_channel.final_texts()) >= 2)
        self.assertEqual(
            fake_channel.final_texts(),
            ["first part", "second part"],
        )
        self.assertEqual(server._agent_initiation_remaining, 0)

    async def test_channel_prompt_exposes_agent_initiation_budget(self) -> None:
        await self.start_server()
        assert self.server is not None
        self.server._agent_initiation_remaining = 2
        prompt = render_channel_turn(
            self_member=self.server._channel_self_member(),
            members=self.server._channel_member_views(),
            message="agent message",
            initiation_remaining=2,
            initiation_limit=3,
            from_agent=True,
            user_name=self.server.config.username,
        )
        self.assertIn('remaining="2"', prompt)
        self.assertIn('limit="3"', prompt)
        self.assertIn("每产生一次对外可见的回复", prompt)

    async def test_channel_status_metadata_is_complete(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        metadata = server._channel_control_metadata()
        for key in (
            "schema",
            "kind",
            "agent",
            "agentId",
            "agentName",
            "memberId",
            "memberName",
            "sourceId",
            "memberKind",
            "description",
            "aliases",
            "protocolVersion",
            "capabilities",
            "model",
            "reasoning",
            "contextLimit",
            "contextTokens",
            "backendReady",
            "channelReady",
            "sandboxReady",
            "queueDepth",
            "activeTurn",
            "pendingApprovals",
            "uptimeS",
            "memoryTokens",
        ):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["kind"], "status")
        self.assertEqual(metadata["memberKind"], "agent")
        self.assertEqual(metadata["agentId"], legacy_member_id(metadata["agentName"]))
        self.assertEqual(metadata["channelReady"], "true")
        self.assertEqual(metadata["queueDepth"], "0")
        self.assertEqual(metadata["pendingApprovals"], "0")
        self.assertEqual(metadata["activeTurn"], "false")

    async def test_channel_status_refreshes_on_turn_and_approval(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, approval_timeout=5.0
        )

        def statuses() -> list[dict[str, str]]:
            return [
                metadata
                for _text, metadata in fake_channel.control
                if metadata.get("kind") == "status"
            ]

        await server._on_channel_text("alice", "alice-1", "hello channel")
        await wait_until(lambda: any(item.get("activeTurn") == "false" for item in statuses()))
        await server._on_channel_text("alice", "device-pc", "run a dangerous tool")
        await wait_until(lambda: bool(fake_channel.events_of("approval")))
        await wait_until(lambda: any(item.get("pendingApprovals") == "1" for item in statuses()))
        await server._on_channel_text("alice", "alice-1", "akid-approve")
        await wait_until(
            lambda: any(item.get("pendingApprovals") == "0" for item in statuses())
        )

    def test_default_backend_uses_standard_permission_mode(self) -> None:
        config = GatewayConfig(
            socket_path=self.root / "perm.sock",
            agent_id="test-agent",
            conversation_id="default",
            cwd=ROOT,
            memory_dir=self.memory,
            backend_dir=self.root / "backend-perm",
            mcp_native_tools_enabled=False,
        )
        server = GatewayServer(config)
        command = server.backend.command
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "standard")

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
        self.assertIn("final", phases)
        self.assertEqual(phases[-1], "ended")
        turn_ids = {event.get("turnId") for event in fake_channel.events_of("turn")}
        self.assertEqual(len(turn_ids), 1)
        progress = [
            event
            for event in fake_channel.events_of("turn")
            if event.get("phase") == "progress"
        ]
        self.assertEqual(progress, [])

    async def test_replayed_channel_backlog_does_not_start_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        server._agent_initiation_remaining = 0
        await server._on_channel_text(
            "alice",
            "alice-1",
            "@Aki backlog message",
            {"schema": "akid.v1", "kind": "message"},
            "backlog-message-1",
            1_700_000_000_123,
            True,
        )
        self.assertEqual(server.active, None)
        self.assertEqual(server.queue, [])
        self.assertEqual(fake_channel.events, [])
        self.assertEqual(server._agent_initiation_remaining, 0)
        entry = server._channel_history[-1]
        self.assertEqual(entry.message_id, "backlog-message-1")
        self.assertEqual(entry.sent_at, 1_700_000_000_123)
        self.assertIn("@Aki backlog message", entry.text)

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

    async def test_embedded_no_reply_marker_suppresses_entire_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        conn = server.channel_conn
        stamp = "2026-01-01T00:00:00Z"
        await conn.send(make_event("turn-no-reply", 1, "turn_started", {}, stamp))
        await conn.send(
            make_event(
                "turn-no-reply",
                2,
                "assistant_message",
                {"content": "等秋先说完。[NO_REPLY]"},
                stamp,
            )
        )
        await conn.send(make_event("turn-no-reply", 3, "turn_end", {}, stamp))
        await wait_until(
            lambda: any(
                event.get("phase") == "no_reply"
                for event in fake_channel.events_of("turn")
            )
        )
        await asyncio.sleep(0.05)
        self.assertEqual(fake_channel.final_texts(), [])
        self.assertFalse(
            any(
                event.get("phase") in ("progress", "final")
                for event in fake_channel.events_of("turn")
            )
        )

    async def test_embedded_no_reply_marker_in_delta_suppresses_flush(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        conn = server.channel_conn
        stamp = "2026-01-01T00:00:00Z"
        await conn.send(make_event("turn-no-reply-delta", 1, "turn_started", {}, stamp))
        await conn.send(
            make_event(
                "turn-no-reply-delta",
                2,
                "assistant_delta",
                {"text": "等秋先说完。【NO_REPLY】"},
                stamp,
            )
        )
        await conn.send(make_event("turn-no-reply-delta", 3, "turn_end", {}, stamp))
        await wait_until(
            lambda: any(
                event.get("phase") == "no_reply"
                for event in fake_channel.events_of("turn")
            )
        )
        await asyncio.sleep(0.05)
        self.assertEqual(fake_channel.final_texts(), [])
        self.assertFalse(
            any(
                event.get("phase") in ("progress", "final")
                for event in fake_channel.events_of("turn")
            )
        )

    async def test_channel_publishes_each_segment_once(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        conn = server.channel_conn
        stamp = "2026-01-01T00:00:00Z"

        await conn.send(make_event("turn-x", 1, "turn_started", {}, stamp))
        await conn.send(
            make_event(
                "turn-x",
                2,
                "assistant_delta",
                {"text": "先拍一张。"},
                stamp,
            )
        )
        await conn.send(
            make_event(
                "turn-x",
                3,
                "tool_call",
                {"name": "camera_take_photo", "tool_call_id": "call-1"},
                stamp,
            )
        )
        await conn.send(
            make_event(
                "turn-x",
                4,
                "tool_result",
                {"tool_call_id": "call-1", "status": "success"},
                stamp,
            )
        )
        await conn.send(
            make_event(
                "turn-x",
                5,
                "assistant_delta",
                {"text": "拍好了。"},
                stamp,
            )
        )
        await conn.send(
            make_event(
                "turn-x",
                6,
                "assistant_message",
                {"content": "先拍一张。拍好了。"},
                stamp,
            )
        )
        await conn.send(make_event("turn-x", 7, "turn_end", {}, stamp))
        await wait_until(
            lambda: any(
                event.get("phase") == "ended"
                for event in fake_channel.events_of("turn")
            )
        )

        progress = [
            event["text"]
            for event in fake_channel.events_of("turn")
            if event.get("phase") == "progress"
        ]
        self.assertEqual(progress, ["先拍一张。"])
        self.assertEqual(fake_channel.final_texts(), ["拍好了。"])

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
        self.assertEqual(approval.get("requesterId"), legacy_member_id("alice"))

        await server._on_channel_text(
            "bob", "device-bob", f"akid-approve {approval['approvalId']}"
        )
        await wait_until(
            lambda: any(
                kind == "notice" and "only the message sender" in text
                for kind, text in fake_channel.published
            )
        )
        self.assertIn(approval["approvalId"], server.approvals)

        await server._on_channel_text(
            # Same member id, different transport source (PC -> Android).
            "alice", "device-android", f"akid-deny {approval['approvalId']}"
        )
        await wait_until(
            lambda: any(
                event.get("decision") == "deny"
                for event in fake_channel.events_of("approval_resolved")
            )
        )

    async def test_foreign_approval_command_is_ignored_silently(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel, approval_timeout=5.0)
        handled = await server._handle_channel_command(
            "akid-approve perm-owned-by-another-agent", "alice-1"
        )
        self.assertTrue(handled)
        await asyncio.sleep(0.1)
        self.assertEqual(fake_channel.published, [])
        self.assertEqual(fake_channel.events, [])

    async def test_agent_origin_channel_approval_belongs_to_user(self) -> None:
        server = await self.start_server(approval_timeout=5.0)
        fake_channel = FakeChannel()
        server.channel = fake_channel  # type: ignore[assignment]
        now = time.monotonic()
        for member_id, name, kind in (
            ("fuyu", "Fuyu", "agent"),
            ("alice", "Alice", "user"),
        ):
            server._channel_members[member_id] = {
                "id": member_id,
                "name": name,
                "aliases": (name,),
                "description": "",
                "kind": kind,
                "last_seen": now,
                "sources": {},
            }
        turn = Turn(
            turn_id="turn-agent-approval",
            conn=server.channel_conn,
            message="",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="fuyu",
            origin_is_agent=True,
        )
        future: asyncio.Future[tuple[bool, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        server.approvals["perm-agent"] = Approval(
            approval_id="perm-agent",
            turn=turn,
            future=future,
            tool_name="generate_image",
            tool_call_id="call-agent",
        )

        await server._handle_channel_command("akid-approve perm-agent", "fuyu")
        self.assertFalse(future.done())
        await wait_until(
            lambda: any(
                kind == "notice" and "only the user" in text
                for kind, text in fake_channel.published
            )
        )

        await server._handle_channel_command("akid-approve perm-agent", "alice")
        self.assertTrue(future.done())
        self.assertEqual(future.result(), (True, "approved via MQTT channel"))

    async def test_agent_origin_channel_approval_reaches_local_client(self) -> None:
        runtime, sockets = self._wayland_runtime()
        fake_channel = FakeChannel()
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
                    channel=fake_channel,
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin="/definitely/missing/spectacle",
                    screenshot_output_dir=self.root / "screenshots-agent-approval",
                    screenshot_wayland_display="wayland-1",
                    screenshot_require_approval=True,
                )
                await wait_until(lambda: server._external_tools_registered)
                client = await self.client()
                await client.hello()

                server._enqueue_channel_turn(
                    "Fuyu",
                    "fuyu",
                    "EXTERNAL_SCREENSHOT",
                    origin_is_agent=True,
                    lock_authorized=False,
                    lock_authorization="agent-message",
                )

                approval = await client.next_event(timeout=8.0)
                while approval is not None and approval.get("type") != "approval_request":
                    approval = await client.next_event(timeout=8.0)
                self.assertIsNotNone(approval)
                self.assertEqual(
                    approval["data"]["tool_name"], "desktop_take_screenshot"
                )
                self.assertNotIn("requester_id", approval["data"])
                await client.call(
                    "aki.approval.respond",
                    {
                        "approval_id": approval["data"]["approval_id"],
                        "decision": "allow",
                    },
                )
                await wait_until(
                    lambda: any(
                        "screenshot captured path=" in text
                        for text in fake_channel.final_texts()
                    )
                )
                self.assertTrue(
                    any(
                        event.get("decision") == "allow"
                        for event in fake_channel.events_of("approval_resolved")
                    )
                )
        finally:
            for sock in sockets:
                sock.close()

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

    def _fake_ps(self) -> Path:
        path = self.root / "fake-ps"
        path.write_text(
            "#!/usr/bin/python3\n"
            "print('101 python 40.0 1.5')\n"
            "print('102 firefox 20.0 2.5')\n"
            "print('103 plasmashell 10.0 1.0')\n"
            "print('104 zsh 5.0 0.2')\n"
            "print('105 ps 99.0 0.0')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _write_test_agent(self, system: str = "BASE PROMPT\n") -> Path:
        agents = self.root / "backend" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        path = agents / "test-agent.json"
        path.write_text(
            json.dumps(
                {
                    "id": "test-agent",
                    "name": "Test",
                    "description": "test agent",
                    "system": system,
                    "tags": [],
                    "model": "test/model",
                    "model_settings": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @staticmethod
    def _agent_system(path: Path) -> str:
        return json.loads(path.read_text(encoding="utf-8"))["system"]

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
        self.assertIn("acquire_execution_lock", names)
        self.assertNotIn("release_execution_lock", names)
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

    async def test_image_generation_external_tool_requires_approval(self) -> None:
        api = FakeMiniMaxImageAPI()
        api.start()
        output_dir = self.root / "generated-images"
        try:
            image = ImageGeneration(
                enabled=True,
                upstream_name="minimax",
                model="image-01",
                path="/image_generation",
                base_url=f"http://127.0.0.1:{api.port}/v1",
                url=f"http://127.0.0.1:{api.port}/v1/image_generation",
                api_key="test-minimax-key",
                use_proxy=False,
                output_dir=str(output_dir),
                require_approval=True,
                timeout=5.0,
                max_bytes=1024 * 1024,
                aspect_ratio="1:1",
                prompt_optimizer=True,
                aigc_watermark=False,
            )
            server = await self.start_server(
                profile="haru",
                camera_enabled=False,
                screenshot_enabled=False,
                image_generation=image,
            )
            await wait_until(lambda: server._external_tools_registered)
            names = [tool["name"] for tool in server._external_tools()]
            self.assertIn("generate_image", names)
            health = server._health()
            self.assertTrue(health["external_tools"]["image_generation"]["available"])
            self.assertEqual(
                health["external_tools"]["image_generation"]["model"], "image-01"
            )

            client = await self.client()
            await client.hello()
            turn = await client.call(
                "aki.chat.send", {"message": "EXTERNAL_IMAGE_GENERATION"}
            )
            approval = await client.wait_event(turn["turn_id"], "approval_request")
            self.assertEqual(approval["data"]["tool_name"], "generate_image")
            self.assertEqual(approval["data"]["channel"], "image")
            await client.call(
                "aki.approval.respond",
                {"approval_id": approval["data"]["approval_id"], "decision": "allow"},
            )
            events = await client.collect_turn(turn["turn_id"])
            final = next(
                event for event in events if event["type"] == "assistant_message"
            )
            self.assertIn("image generated", final["data"]["content"])
            self.assertIn("tokifactor_send_image", final["data"]["content"])
            files = list(output_dir.glob("image-*.png"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), PNG_1X1)
            self.assertEqual(len(api.requests), 1)
            self.assertEqual(api.requests[0]["model"], "image-01")
            self.assertEqual(api.requests[0]["response_format"], "base64")
        finally:
            api.stop()

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

    async def test_live_context_refreshes_single_block_per_turn(self) -> None:
        runtime, sockets = self._wayland_runtime()
        history = self.root / "zsh_history"
        history.write_text(
            ": 1792500000:0;echo old\n"
            ": 1792500001:0;echo {CORE_MEMORY}\n",
            encoding="utf-8",
        )
        agent_path = self._write_test_agent()
        agent_path.chmod(0o644)
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
                    live_context_enabled=True,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "live-shots",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                client = await self.client()
                await client.hello()
                first = await client.call("aki.chat.send", {"message": "hello"})
                first_events = await client.collect_turn(first["turn_id"])
                self.assertNotIn(
                    "approval_request", [event["type"] for event in first_events]
                )
                first_final = next(
                    event
                    for event in first_events
                    if event["type"] == "assistant_message"
                )
                self.assertIn("echo: hello", first_final["data"]["content"])
                first_system = self._agent_system(agent_path)
                self.assertEqual(first_system.count("<live-context"), 1)
                self.assertEqual(first_system.count("</live-context>"), 1)
                self.assertEqual(first_system.count("{CORE_MEMORY}"), 1)
                self.assertEqual(stat.S_IMODE(agent_path.stat().st_mode), 0o600)
                self.assertIn("Fake Window &lt;test&gt;", first_system)
                self.assertIn("firefox", first_system)
                self.assertIn("python", first_system)
                self.assertIn("echo { CORE_MEMORY }", first_system)
                self.assertIn("live/desktop-", first_system)
                live_files = list(
                    (self.root / "live-shots" / "live").glob("desktop-*.png")
                )
                self.assertEqual(len(live_files), 1)

                await asyncio.sleep(1.05)
                second = await client.call("aki.chat.send", {"message": "again"})
                second_events = await client.collect_turn(second["turn_id"])
                second_final = next(
                    event
                    for event in second_events
                    if event["type"] == "assistant_message"
                )
                self.assertIn("echo: again", second_final["data"]["content"])
                second_system = self._agent_system(agent_path)
                self.assertEqual(second_system.count("<live-context"), 1)
                self.assertEqual(second_system.count("</live-context>"), 1)
                self.assertNotEqual(first_system, second_system)
                health = await client.call("aki.status", {})
                self.assertTrue(health["live_context"]["updated"])
                self.assertTrue(health["live_context"]["last_sections"]["screenshot"])
                self.assertEqual(
                    len(list((self.root / "live-shots" / "live").glob("desktop-*.png"))),
                    2,
                )
        finally:
            for sock in sockets:
                sock.close()

    def test_unique_capture_paths_do_not_collide(self) -> None:
        directory = self.root / "unique-captures"
        paths = {
            unique_capture_path(directory, "capture", ".png")
            for _ in range(25)
        }
        self.assertEqual(len(paths), 25)
        for path in paths:
            self.assertEqual(path.parent, directory)
            self.assertRegex(
                path.name,
                r"^capture-\d{8}-\d{6}-\d{6}-[0-9a-f]{8}\.png$",
            )

    async def test_live_context_screenshot_failure_omits_section(self) -> None:
        runtime, sockets = self._wayland_runtime()
        history = self.root / "zsh_history-failure"
        history.write_text(": 1792500000:0;echo hi\n", encoding="utf-8")
        agent_path = self._write_test_agent()
        failure = self.root / "fake-spectacle-failure"
        failure.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        failure.chmod(0o755)
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
                    live_context_enabled=True,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(failure),
                    screenshot_output_dir=self.root / "live-shots-failure",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                client = await self.client()
                await client.hello()
                turn = await client.call("aki.chat.send", {"message": "hello"})
                events = await client.collect_turn(turn["turn_id"])
                self.assertNotIn(
                    "approval_request", [event["type"] for event in events]
                )
                system = self._agent_system(agent_path)
                self.assertIn("<live-context", system)
                self.assertNotIn("<screenshot", system)
                self.assertIn("echo hi", system)
                status = await client.call("aki.status", {})
                self.assertFalse(status["live_context"]["last_sections"]["screenshot"])
                self.assertTrue(status["live_context"]["last_sections"]["processes"])
        finally:
            for sock in sockets:
                sock.close()

    async def test_live_context_budget_and_disable_clears_stale_block(self) -> None:
        runtime, sockets = self._wayland_runtime()
        history = self.root / "zsh_history-budget"
        history.write_text(
            "\n".join(
                f": {1792500000 + index}:0;{'x' * 800}{index}"
                for index in range(20)
            )
            + "\n",
            encoding="utf-8",
        )
        agent_path = self._write_test_agent(
            "BASE\n\n<live-context refreshed_at=\"old\">stale</live-context>\n"
        )
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
                    live_context_enabled=True,
                    live_context_budget_tokens=2048,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "live-shots-budget",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                client = await self.client()
                await client.hello()
                turn = await client.call("aki.chat.send", {"message": "hello"})
                await client.collect_turn(turn["turn_id"])
                system = self._agent_system(agent_path)
                block = system[
                    system.index("<live-context") : system.index(
                        "</live-context>"
                    )
                    + len("</live-context>")
                ]
                self.assertLessEqual(estimate_tokens(block), 2048)
                self.assertNotIn("stale", system)

                disabled_path = self._write_test_agent(
                    "BASE\n\n<live-context refreshed_at=\"old\">stale</live-context>\n"
                )
                await server.stop()
                self.server = None
                server = await self.start_server(live_context_enabled=False)
                cleared = self._agent_system(disabled_path)
                self.assertNotIn("<live-context", cleared)
                client = await self.client()
                await client.hello()
                turn = await client.call("aki.chat.send", {"message": "hello"})
                await client.collect_turn(turn["turn_id"])
                self.assertNotIn(
                    "<live-context", self._agent_system(disabled_path)
                )
        finally:
            for sock in sockets:
                sock.close()

    async def test_live_context_refreshes_for_channel_turn(self) -> None:
        runtime, sockets = self._wayland_runtime()
        history = self.root / "zsh_history-channel"
        history.write_text(": 1792500000:0;echo channel\n", encoding="utf-8")
        agent_path = self._write_test_agent()
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
                    live_context_enabled=True,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "live-shots-channel",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                server._enqueue_channel_turn("sender", "sender", "channel hello")
                await wait_until(lambda: server.active is None and not server.queue)
                system = self._agent_system(agent_path)
                self.assertEqual(system.count("<live-context"), 1)
                self.assertIn("echo channel", system)
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
            approvals = [approval] + [
                event for event in events if event["type"] == "approval_request"
            ]
            self.assertEqual(len(approvals), 1)
            self.assertTrue(
                approvals[0]["data"]["approval_id"].startswith("external-")
            )
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
            from engine.aki_config import read_mqtt

            saved_host, saved_port = read_mqtt(state)
            self.assertEqual(saved_host, "127.0.0.1")
            self.assertEqual(saved_port, broker2.port)
            await wait_until(lambda: server.channel is not None and server.channel.connected)
        finally:
            await server.stop()
            self.server = None
            await broker1.stop()
            await broker2.stop()


if __name__ == "__main__":
    unittest.main()
