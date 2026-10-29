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
    NullConnection,
    Turn,
    WAIT_GUARD_ORIGIN,
    WAIT_USER_TOOL,
    _b64url,
    _format_channel_message_time,
    _normalize_mcp_result,
    unique_capture_path,
)
from engine.aki_config import ImageGeneration
from engine.channel_history import read_channel_history
from engine.letta_process import LettaProcess
from engine.execution_policy import WAIT_OTHER_TOOL, WAIT_USER_TOOL as POLICY_WAIT_USER_TOOL
from engine.live_context import (
    ChannelImageAttachment,
    WindowInfo,
    estimate_tokens,
    render_channel_policy,
    render_channel_roster,
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


class ChannelSendResult:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("kind") == kind]

    def final_texts(self) -> list[str]:
        return [
            str(event.get("text") or "")
            for event in self.events_of("turn")
            if event.get("phase") == "final"
        ]

    @property
    def final_text(self) -> str:
        texts = self.final_texts()
        return texts[-1] if texts else ""


async def channel_send(
    server: GatewayServer,
    text: str,
    *,
    sender: str = "Master",
    sender_id: str = "master",
    metadata: dict[str, str] | None = None,
    wait: bool = True,
    timeout: float = 10.0,
    client_message_id: str | None = None,
) -> ChannelSendResult:
    if server.channel is None:
        server.channel = FakeChannel()
    fake = server.channel
    start = len(fake.events)
    values = dict(metadata or {})
    if client_message_id:
        values["clientMessageId"] = client_message_id
    await server._on_channel_text(sender, sender_id, text, values)
    if wait:
        await wait_until(
            lambda: server.active is None and not server.queue, timeout=timeout
        )
    return ChannelSendResult(fake.events[start:])


async def wait_for_approval(
    server: GatewayServer, approval_id: str | None = None, *, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for pending in server.approvals.values():
            if approval_id is None or pending.approval_id == approval_id:
                return {
                    "approval_id": pending.approval_id,
                    "tool_name": pending.tool_name,
                }
        await asyncio.sleep(0.02)
    raise AssertionError("approval request not reached before timeout")


async def respond_approval(
    server: GatewayServer, approval_id: str, *, allow: bool, message: str = ""
) -> None:
    approval = server.approvals.get(approval_id)
    if approval is None:
        raise AssertionError(f"unknown approval: {approval_id}")
    if not approval.future.done():
        approval.future.set_result(
            (allow, message or ("approved via test" if allow else "denied via test"))
        )


def overlay_text(server: GatewayServer, *, update_index: int = -1) -> str:
    overlays = getattr(server, "bridge_overlays", [])
    if not overlays:
        return ""
    overlay = overlays[update_index]
    context = overlay.get("turn_context") if isinstance(overlay, dict) else None
    if not isinstance(context, dict):
        return ""
    return str(context.get("system_append") or "")


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
        mqtt_host: str = "127.0.0.1",
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
        server.bridge_overlays = []
        server.bridge_restores = []
        original_begin = server.backend.begin_aki_turn
        original_update = server.backend.update_aki_turn
        original_restore = server.backend.restore_conversation

        async def capture_begin(
            turn_id: str, overlay: dict[str, Any], **kwargs: Any
        ) -> Any:
            server.bridge_overlays.append(dict(overlay))
            return await original_begin(turn_id, overlay, **kwargs)

        async def capture_update(
            turn_id: str, overlay: dict[str, Any], **kwargs: Any
        ) -> Any:
            server.bridge_overlays.append(dict(overlay))
            return await original_update(turn_id, overlay, **kwargs)

        async def capture_restore(snapshot_id: str, **kwargs: Any) -> Any:
            server.bridge_restores.append(snapshot_id)
            return await original_restore(snapshot_id, **kwargs)

        server.backend.begin_aki_turn = capture_begin
        server.backend.update_aki_turn = capture_update
        server.backend.restore_conversation = capture_restore
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
        server = await self.start_server(channel=FakeChannel())
        result = await channel_send(server, "hello", client_message_id="seq-1")
        turns = result.events_of("turn")
        phases = [event.get("phase") for event in turns]
        self.assertEqual(phases[0], "started")
        self.assertIn("final", phases)
        self.assertEqual(phases[-1], "ended")
        self.assertEqual(result.final_text, "channel reply")
        self.assertTrue(
            all(event.get("clientMessageId") == "seq-1" for event in turns)
        )
        history = read_channel_history(server.channel_history_path)
        self.assertTrue(any(entry.get("text") == "channel reply" for entry in history))

    async def test_mqtt_turn_notifies_layer_bubble(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        notifier = FakeLayerNotifier()
        server._layer_notifier = notifier
        result = await channel_send(server, "hello")
        self.assertEqual(result.final_text, "channel reply")
        self.assertEqual(notifier.messages, [("Aki", result.final_text)])

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
        server = await self.start_server(channel=FakeChannel())
        result = await channel_send(server, "INTERNAL_RESULT_RACE")
        ended = [
            event
            for event in result.events_of("turn")
            if event.get("phase") in ("ended", "no_reply")
        ]
        self.assertEqual(len(ended), 1)
        self.assertIn("real reply", result.final_text)
        self.assertNotIn("internal notification reply", result.final_text)

    async def test_mcp_tool_discovery_builds_native_specs(self) -> None:
        await self.start_server(mcp_native_tools_enabled=False)
        assert self.server is not None

        async def fake_mcp_list(*args, **kwargs):
            return {"servers": [{"name": "tavily"}]}

        async def fake_mcp_tools(*args, **kwargs):
            return {
                "tools": [
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
            }

        with mock.patch.object(
            self.server.backend, "mcp_list", new=fake_mcp_list
        ), mock.patch.object(self.server.backend, "mcp_tools", new=fake_mcp_tools):
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

        async def fake_mcp_list(*args, **kwargs):
            return {"servers": [{"name": "tavily"}, {"name": "local"}]}

        async def fake_mcp_tools(*args, **kwargs):
            return {
                "tools": [
                    {
                        "name": "mcp__tavily__tavily_search",
                        "description": "Search the web",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "mcp__local__nmap_scan",
                        "description": "Scan ports",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            }

        with mock.patch.object(
            self.server.backend, "mcp_list", new=fake_mcp_list
        ), mock.patch.object(self.server.backend, "mcp_tools", new=fake_mcp_tools):
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
        server = await self.start_server(
            channel=FakeChannel(), mcp_native_tools_enabled=False
        )
        server._mcp_tools = [
            {
                "name": "mcp__tavily__tavily_search",
                "description": "Search the web",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        server._mcp_tool_names = {"mcp__tavily__tavily_search"}
        with mock.patch.object(
            server,
            "_call_mcp_tool",
            new=mock.AsyncMock(return_value=("native search result", False)),
        ) as call_mcp:
            result = await channel_send(server, "NATIVE_MCP_TOOL")

        call_mcp.assert_awaited_once_with(
            "mcp__tavily__tavily_search", {"query": "native search"}
        )
        tool_calls = [
            event
            for event in result.events_of("tool")
            if event.get("toolStatus") == "running"
        ]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(
            tool_calls[0]["toolName"], "mcp__tavily__tavily_search"
        )
        tool_result = [
            event
            for event in result.events_of("tool")
            if event.get("toolStatus") == "success"
        ]
        self.assertTrue(tool_result)
        self.assertIn("native search result", result.final_text)

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

    async def test_conversation_compact_uses_bridge(self) -> None:
        server = await self.start_server()
        client = await self.client()
        await client.hello()
        with mock.patch.object(
            server.backend,
            "reflection_status",
            new=mock.AsyncMock(
                return_value={
                    "last_reflection_started_at": "",
                    "last_reflection_succeeded_at": "",
                }
            ),
        ), mock.patch.object(
            server.backend,
            "compact_conversation",
            new=mock.AsyncMock(
                return_value={
                    "compacted": True,
                    "mode": "all",
                    "num_messages_before": 12,
                    "num_messages_after": 3,
                    "summary": "condensed",
                }
            ),
        ), mock.patch.object(
            server.backend,
            "reflect_conversation",
            new=mock.AsyncMock(return_value={"launched": True}),
        ), mock.patch.object(
            server, "_wait_bridge_reflection", new=mock.AsyncMock(return_value="completed")
        ):
            result = await client.call(
                "aki.conversation.compact", {"mode": "all", "timeout": 120}
            )
        self.assertTrue(result["compacted"])
        self.assertTrue(result["backend_ready"])
        self.assertEqual(result["conversation_id"], "default")
        self.assertEqual(result["reflection"], "completed")
        self.assertEqual(result["mode"], "all")

    async def test_conversation_compact_rejects_invalid_mode_and_busy_turn(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        client = await self.client()
        await client.hello()
        with self.assertRaises(RpcError) as invalid:
            await client.call("aki.conversation.compact", {"mode": "bad"})
        self.assertEqual(invalid.exception.code, ErrorCode.INVALID_PARAMS)

        await server._on_channel_text(
            "Master",
            "master",
            "slow",
            {"clientMessageId": "compact-busy"},
        )
        await wait_until(lambda: server.active is not None)
        with self.assertRaises(RpcError) as busy:
            await client.call("aki.conversation.compact", {})
        self.assertEqual(busy.exception.code, ErrorCode.COMPACT_FAILED)
        self.assertIn("active or queued", busy.exception.message)
        await server._on_channel_text(
            "Master",
            "master",
            "",
            {
                "schema": "akid.v1",
                "kind": "cancel",
                "clientMessageId": "compact-busy",
            },
        )
        await wait_until(lambda: server.active is None)

    async def test_mqtt_cancel_only_accepts_the_original_human_sender(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        await server._on_channel_text(
            "Master",
            "master",
            "slow",
            {"clientMessageId": "cancel-owner"},
        )
        await wait_until(lambda: server.active is not None)
        await server._on_channel_text(
            "Other",
            "other",
            "",
            {
                "schema": "akid.v1",
                "kind": "cancel",
                "clientMessageId": "cancel-owner",
            },
        )
        self.assertIsNotNone(server.active)
        await wait_until(
            lambda: any(
                kind == "notice" and "only the human" in text
                for kind, text in fake_channel.published
            )
        )
        await server._on_channel_text(
            "Master",
            "master",
            "",
            {
                "schema": "akid.v1",
                "kind": "cancel",
                "clientMessageId": "cancel-owner",
            },
        )
        await wait_until(lambda: server.active is None)

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
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        conversation_dir = (
            server.config.backend_dir
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
        await server._on_channel_text("Master", "master", "slow")
        await wait_until(lambda: server.active is not None)
        active = server.active
        server._enqueue_channel_turn(
            "Fuyu",
            "fuyu",
            "queued",
            origin_is_agent=True,
        )
        await wait_until(lambda: bool(server.queue))
        queued = server.queue[0]
        result = await client.call("aki.conversation.reset", {})
        self.assertEqual(result["interrupted_turn_id"], active.turn_id)
        self.assertEqual(result["dropped_turn_ids"], [queued.turn_id])
        self.assertTrue(
            any(
                event.get("kind") == "error"
                and "conversation reset" in str(event.get("text") or "")
                for event in fake_channel.events
            )
        )
        ended = [
            event
            for event in fake_channel.events
            if event.get("kind") == "turn"
            and event.get("phase") == "ended"
            and event.get("turnId") in {active.turn_id, queued.turn_id}
        ]
        self.assertEqual(len(ended), 2)
        self.assertTrue((conversation_dir / "messages.jsonl").read_bytes() == b"")
        self.assertTrue(Path(result["backup_path"]).is_dir())

    def _assert_tool_detail(
        self,
        events: list[dict[str, Any]],
        *,
        argument_needle: str,
        result_needle: str,
        status: str,
    ) -> None:
        running = [
            json.loads(str(event.get("text") or "{}"))
            for event in events
            if event.get("toolStatus") == "running"
            and argument_needle in str(event.get("text") or "")
        ]
        self.assertTrue(running)
        self.assertNotIn("result", running[-1])
        self.assertIn(argument_needle, running[-1]["arguments"])
        finished = [
            json.loads(str(event.get("text") or "{}"))
            for event in events
            if event.get("toolStatus") == status
            and argument_needle in str(event.get("text") or "")
        ]
        self.assertTrue(finished)
        self.assertIn(argument_needle, finished[-1]["arguments"])
        self.assertIn(result_needle, finished[-1]["result"])

    async def test_approval_allow_deny_and_safe_tool(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(), approval_timeout=2.0
        )

        allowed_task = asyncio.create_task(channel_send(server, "dangerous"))
        approval = await wait_for_approval(server)
        await respond_approval(
            server, approval["approval_id"], allow=True
        )
        allowed = await allowed_task
        self.assertIn("success", [event.get("toolStatus") for event in allowed.events_of("tool")])
        self._assert_tool_detail(
            allowed.events_of("tool"),
            argument_needle="echo channel-unsafe",
            result_needle="tool output",
            status="success",
        )

        denied_task = asyncio.create_task(channel_send(server, "dangerous"))
        approval = await wait_for_approval(server)
        await respond_approval(
            server, approval["approval_id"], allow=False, message="nope"
        )
        denied = await denied_task
        self.assertIn("error", [event.get("toolStatus") for event in denied.events_of("tool")])

        safe = await channel_send(server, "safe")
        self.assertFalse(safe.events_of("approval"))
        self.assertIn("success", [event.get("toolStatus") for event in safe.events_of("tool")])
        self._assert_tool_detail(
            safe.events_of("tool"),
            argument_needle="MEMORY.md",
            result_needle="tool output",
            status="success",
        )

    async def test_bash_auto_allowed_when_sandbox_enabled(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(), approval_timeout=2.0
        )
        server.config.sandbox_enabled = True
        server._sandbox_blocked_reason = None
        result = await channel_send(server, "dangerous")
        self.assertFalse(result.events_of("approval"))
        self.assertIn("success", [event.get("toolStatus") for event in result.events_of("tool")])

    async def test_approval_timeout_denies_and_turn_continues(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(), approval_timeout=0.25
        )
        result = await channel_send(server, "timeout")
        self.assertIn("error", [event.get("toolStatus") for event in result.events_of("tool")])
        self.assertEqual(result.events_of("turn")[-1].get("phase"), "ended")

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
        server = await self.start_server(channel=FakeChannel())
        crashed = await channel_send(server, "crash")
        self.assertTrue(crashed.events_of("error"))
        self.assertEqual(crashed.events_of("turn")[-1].get("phase"), "ended")

        self.assertTrue(await server.backend.wait_ready(5.0))
        result = await channel_send(server, "after crash")
        self.assertEqual(result.final_text, "channel reply")

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

    async def test_startup_refuses_backend_without_aki_bridge(self) -> None:
        with mock.patch.dict(
            os.environ, {"FAKE_LETTA_NO_BRIDGE": "1"}, clear=False
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await self.start_server()
        self.assertIn("aki_bridge", str(ctx.exception))
        self.assertIsNone(self.server)

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

    async def test_channel_prompt_is_minimal_and_policy_lives_in_overlay(self) -> None:
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
        self.assertNotIn("<channel-policy", system)
        turn = Turn(
            turn_id="turn-policy",
            conn=NullConnection(),
            message="probe",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
        )
        overlay = "\n\n".join(await server._build_turn_overlay(turn))
        self.assertIn("<channel-policy", overlay)
        self.assertIn(
            "你的回复会被自动发布，不要通过工具发送文字消息。若判断本轮无需回复，直接输出 [NO_REPLY]。",
            overlay,
        )
        self.assertNotIn("wake-reminder:", prompt)
        self.assertNotIn("reply-target", prompt)
        self.assertIn("Use `^MemberName` to exclude members from being woken.", overlay)
        self.assertIn("use `@Master`; the marker may appear anywhere in the reply", overlay)

    async def test_channel_turn_is_bridge_overlay_only(self) -> None:
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
        block = await server._refresh_channel_turn(turn)
        self.assertIn('<channel-turn version="1" from_agent="false" new="true">', block)
        self.assertIn(
            '<new-message seq="7" sender="Acite" sender_id="sender-1" source="user"',
            block,
        )
        self.assertIn("<text>hello</text>", block)
        self.assertNotIn("<wake-reminder>", block)
        self.assertNotIn("<reply-target>", block)
        self.assertIn("<guidance>", block)
        self.assertIn('remaining="3"', block)
        self.assertIn("resets=\"user-message\"", block)
        system = self._agent_system(agent_path)
        self.assertNotIn("<channel-turn", system)
        self.assertIn(
            "Use `^MemberName` to exclude members from being woken.",
            server._channel_wake_routing_block(),
        )
        prompt = server._channel_prompt("Acite", "sender-1", "hello")
        self.assertNotIn("<channel-turn", prompt)

    async def test_turn_overlay_contains_runtime_blocks_without_persisting(self) -> None:
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
        before_agent = agent_path.read_bytes()
        before_prompt = prompt_path.read_bytes()
        server = await self.start_server(channel=FakeChannel())
        server._record_channel_message(
            sender="Acite",
            sender_id="sender-1",
            text="history",
            from_agent=False,
        )
        turn = Turn(
            turn_id="turn-runtime-overlay",
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
        text = "\n\n".join(await server._build_turn_overlay(turn))
        openings = (
            "<tool-ownership version=",
            "<channel-policy version=",
            "<channel-wake-routing version=",
            "<channel-identity version=",
            "<channel-turn version=",
            "<channel-transcript ",
            "<channel-context>",
        )
        for opening in openings:
            self.assertEqual(text.count(opening), 1, opening)
        self.assertEqual(agent_path.read_bytes(), before_agent)
        self.assertEqual(prompt_path.read_bytes(), before_prompt)

    async def test_wake_routing_is_overlay_only(self) -> None:
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
        self.assertNotIn("<channel-wake-routing", agent_system)
        wake_block = server._channel_wake_routing_block()
        self.assertIn("<channel-wake-routing", wake_block)
        self.assertIn(
            "If the reply contains `@MemberName `: wake ONLY the mentioned members.",
            wake_block,
        )
        self.assertIn(
            "If there is no `@` but the reply contains `^MemberName`", wake_block
        )
        self.assertIn("use `@Master`; the marker may appear anywhere in the reply", wake_block)

        compiled = json.loads(prompt_path.read_text(encoding="utf-8"))["content"]
        self.assertEqual(compiled, "BASE SYSTEM PROMPT\n")

        channel_prompt = server._channel_prompt("Acite", "sender-1", "你好")
        self.assertNotIn("<wake-routing>", channel_prompt)
        self.assertNotIn("不希望其他成员注意到这条消息", channel_prompt)
        self.assertNotIn("wake-reminder:", channel_prompt)

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
        overlay = overlay_text(server)
        self.assertIn("<channel-transcript", overlay)
        self.assertIn(body, overlay)
        self.assertIn('new="true"', overlay)
        self.assertIn('source="agent"', overlay)
        system = self._agent_system(agent_path)
        self.assertNotIn("<channel-transcript", system)
        self.assertNotIn(body, system)

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
            bridge_turn_started=True,
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
        self.assertEqual(overlay_text(server), "")
        self.assertEqual(busy_turn.overlay_files, {})

        await server._on_channel_text("alice", "alice-1", "USER_MESSAGE_WHILE_BUSY")
        self.assertEqual(server.queue, [])
        self.assertEqual(len(busy_turn.busy_messages), 1)
        self.assertTrue(server._channel_history[-1].arrived_while_busy)
        self.assertEqual(fake_channel.events, [])
        self.assertEqual(overlay_text(server), "")
        self.assertEqual(len(busy_turn.overlay_files), 1)
        overlay_path = next(iter(busy_turn.overlay_files.values()))
        payload = json.loads(overlay_path.read_text(encoding="utf-8"))
        self.assertIn("USER_MESSAGE_WHILE_BUSY", payload["content"])
        self.assertEqual(payload["schema"], "aki.turn-overlay.v1")
        self.assertTrue(payload["insertion_id"].startswith("ins-"))
        self.assertIn("<user-insertion ", payload["content"])
        self.assertIn("inserted-at=", payload["content"])
        self.assertNotIn("during-work-note", payload["content"])
        self.assertEqual(stat_mode(overlay_path.parent), 0o700)
        self.assertEqual(stat_mode(overlay_path), 0o600)
        await server._finalize_turn_overlays(busy_turn)
        self.assertEqual(len(server.queue), 1)
        self.assertIn("USER_MESSAGE_WHILE_BUSY", server.queue[0].message)
        self.assertFalse(overlay_path.exists())
        server.active = None
        server.queue.clear()

    async def test_bridge_busy_message_writes_overlay_file_without_backend_update(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_LETTA_AKI_BRIDGE": "1"}, clear=False):
            server = await self.start_server(channel=FakeChannel())
        updates: list[dict[str, Any]] = []
        original_update = server.backend.update_aki_turn

        async def capture_update(turn_id: str, overlay: dict[str, Any], **kwargs: Any):
            updates.append({"turn_id": turn_id, "overlay": overlay})
            return await original_update(turn_id, overlay, **kwargs)

        with mock.patch.object(server.backend, "update_aki_turn", side_effect=capture_update):
            task = asyncio.create_task(
                channel_send(
                    server,
                    "slow",
                    client_message_id="busy-bridge",
                    timeout=20.0,
                )
            )
            await wait_until(lambda: server.active is not None)
            await server._on_channel_text("Master", "master", "补充一下")
            await wait_until(lambda: bool(server.active.overlay_files))
            self.assertEqual(updates, [])
            overlay_path = next(iter(server.active.overlay_files.values()))
            payload = json.loads(overlay_path.read_text(encoding="utf-8"))
            content = payload["content"]
            self.assertIn("<user-insertion ", content)
            self.assertIn("inserted-at=", content)
            self.assertNotIn("during-work-note", content)
            self.assertIn("补充一下", content)
            self.assertEqual(payload["turn_id"], server.active.turn_id)
            self.assertFalse(payload["origin_is_agent"])
            await server._on_channel_text(
                "Master",
                "master",
                "",
                {
                    "schema": "akid.v1",
                    "kind": "cancel",
                    "clientMessageId": "busy-bridge",
                },
            )
            await task

    async def test_bridge_user_turn_keeps_transcript_out_of_system_prompt(self) -> None:
        agent_path = self._write_test_agent()
        server = await self.start_server(channel=FakeChannel())
        await channel_send(server, "bridge hello")
        self.assertNotIn("<channel-transcript", self._agent_system(agent_path))
        self.assertIn("<channel-transcript", overlay_text(server))

    def _write_channel_conversation(
        self, server: GatewayServer, rows: list[dict[str, Any]]
    ) -> Path:
        directory = server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "messages.jsonl"
        path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        (directory / "conversation.json").write_text(
            '{"id":"default","in_context_message_ids":[]}\n', encoding="utf-8"
        )
        return path

    def _conversation_user_rows(self, server: GatewayServer) -> list[dict[str, Any]]:
        path = server._conversation_dir() / "messages.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def test_unconsumed_user_messages_merge_into_one_new_turn(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        turn = Turn(
            turn_id="overlay-unconsumed",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_sender="Master",
            channel_message_seq=1,
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "第一段")
        attachment = ChannelImageAttachment(
            name="补充图.png",
            path=str(self.root / "overlay.png"),
            mime="image/png",
            size=8,
            index=1,
        )
        await server._on_channel_text(
            "Master", "master", "第二段", _attachments=(attachment,)
        )
        self.assertEqual(len(turn.overlay_files), 2)
        overlay_paths = list(turn.overlay_files.values())
        await server._finalize_turn_overlays(turn)
        self.assertEqual(len(server.queue), 1)
        queued = server.queue[0]
        self.assertIn("第一段", queued.message)
        self.assertIn("第二段", queued.message)
        self.assertEqual(queued.channel_message_seq, 2)
        self.assertEqual(queued.channel_attachments[0].name, "补充图.png")
        self.assertTrue(all(not path.exists() for path in overlay_paths))
        server.active = None
        server.queue.clear()

    async def test_consumed_user_message_merges_into_current_user_role(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        prompt = server._channel_prompt(
            "Master", "master", "原始问题", sent_at=1_792_942_663_759
        )
        self._write_channel_conversation(
            server,
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "entry-user",
                    "parentId": None,
                    "timestamp": "2026-10-28T00:00:00Z",
                    "message": {
                        "id": "ui-user",
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                        "timestamp": 1792942663000,
                    },
                },
            ],
        )
        turn = Turn(
            turn_id="overlay-consumed-user",
            conn=server.channel_conn,
            message=prompt,
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_sender="Master",
            channel_message_seq=2,
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "执行中补充")
        overlay_path = next(iter(turn.overlay_files.values()))
        overlay_path.unlink()
        await server._finalize_turn_overlays(turn)
        self.assertEqual(server.queue, [])
        self.assertEqual(turn.user_role_entry_id, "entry-user")
        rows = self._conversation_user_rows(server)
        user_rows = [row for row in rows if row.get("message", {}).get("role") == "user"]
        self.assertEqual(len(user_rows), 1)
        content = user_rows[0]["message"]["content"]
        self.assertEqual(len(content), 2)
        self.assertIn("执行中补充", content[1]["text"])
        self.assertIn("<user-insertions", content[1]["text"])
        self.assertIn('count="1"', content[1]["text"])
        self.assertIn('during-work="true"', content[1]["text"])
        self.assertIn("<user-insertion ", content[1]["text"])
        server.active = None

    async def test_multiple_consumed_insertions_merge_in_one_envelope(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        prompt = server._channel_prompt("Master", "master", "原始问题")
        self._write_channel_conversation(
            server,
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "entry-user",
                    "parentId": None,
                    "timestamp": "2026-10-28T00:00:00Z",
                    "message": {
                        "id": "ui-user",
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                        "timestamp": 1792942663000,
                    },
                },
            ],
        )
        turn = Turn(
            turn_id="overlay-two-consumed",
            conn=server.channel_conn,
            message=prompt,
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_sender="Master",
            channel_message_seq=2,
            state="running",
        )
        server.active = turn
        await server._on_channel_text(
            "Master", "master", "第一条补充", sent_at=1_792_942_664_000
        )
        await server._on_channel_text(
            "Master", "master", "第二条补充", sent_at=1_792_942_664_500
        )
        for path in list(turn.overlay_files.values()):
            path.unlink()
        await server._finalize_turn_overlays(turn)
        self.assertEqual(server.queue, [])
        rows = self._conversation_user_rows(server)
        user_row = next(
            row for row in rows if row.get("message", {}).get("role") == "user"
        )
        merged = user_row["message"]["content"][1]["text"]
        self.assertIn("<user-insertions", merged)
        self.assertIn('count="2"', merged)
        self.assertEqual(merged.count("<user-insertion "), 2)
        self.assertLess(
            merged.index("第一条补充"), merged.index("第二条补充")
        )
        self.assertEqual(merged.count("inserted-at="), 2)
        self.assertNotIn("during-work-note", merged)
        server.active = None

    async def test_agent_turn_merge_targets_latest_human_role(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        human_prompt = server._channel_prompt("Master", "master", "历史真话")
        agent_prompt = server._channel_prompt(
            "Fuyu", legacy_member_id("Fuyu"), "智能体触发", from_agent=True
        )
        self._write_channel_conversation(
            server,
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "entry-human",
                    "parentId": None,
                    "timestamp": "2026-10-28T00:00:00Z",
                    "message": {
                        "id": "ui-human",
                        "role": "user",
                        "content": [{"type": "text", "text": human_prompt}],
                        "timestamp": 1792942663000,
                    },
                },
                {
                    "type": "message",
                    "id": "entry-agent",
                    "parentId": "entry-human",
                    "timestamp": "2026-10-28T00:00:01Z",
                    "message": {
                        "id": "ui-agent",
                        "role": "user",
                        "content": [{"type": "text", "text": agent_prompt}],
                        "timestamp": 1792942664000,
                    },
                },
                {
                    "type": "message",
                    "id": "entry-assistant",
                    "parentId": "entry-agent",
                    "timestamp": "2026-10-28T00:00:02Z",
                    "message": {
                        "id": "ui-assistant",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "临时回复"}],
                        "timestamp": 1792942665000,
                    },
                },
            ],
        )
        turn = Turn(
            turn_id="overlay-consumed-agent",
            conn=server.channel_conn,
            message=agent_prompt,
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id=legacy_member_id("Fuyu"),
            origin_is_agent=True,
            channel_sender="Fuyu",
            channel_message_seq=2,
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "人类插话")
        next(iter(turn.overlay_files.values())).unlink()
        await server._finalize_turn_overlays(turn)
        rows = self._conversation_user_rows(server)
        human_row = next(row for row in rows if row.get("id") == "entry-human")
        agent_row = next(row for row in rows if row.get("id") == "entry-agent")
        self.assertEqual(len(human_row["message"]["content"]), 2)
        self.assertIn("人类插话", human_row["message"]["content"][1]["text"])
        self.assertEqual(
            agent_row["message"]["content"],
            [{"type": "text", "text": agent_prompt}],
        )
        server.active = None

    async def test_reload_failure_does_not_requeue_consumed_message(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        prompt = server._channel_prompt("Master", "master", "原始问题")
        path = self._write_channel_conversation(
            server,
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "entry-user",
                    "parentId": None,
                    "timestamp": "2026-10-28T00:00:00Z",
                    "message": {
                        "id": "ui-user",
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                        "timestamp": 1792942663000,
                    },
                },
            ],
        )
        before = path.read_bytes()
        turn = Turn(
            turn_id="overlay-reload-failure",
            conn=server.channel_conn,
            message=prompt,
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_sender="Master",
            channel_message_seq=2,
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "不能丢的消息")
        next(iter(turn.overlay_files.values())).unlink()
        with mock.patch.object(
            server.backend,
            "reload_conversation",
            side_effect=RuntimeError("reload failed"),
        ):
            await server._finalize_turn_overlays(turn)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(server.queue, [])
        server.active = None
        server.queue.clear()

    async def test_message_at_end_sent_boundary_creates_new_turn(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        turn = Turn(
            turn_id="overlay-end-sent",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_sender="Master",
            state="running",
            end_sent=True,
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "边界消息")
        self.assertEqual(turn.busy_messages, [])
        self.assertEqual(turn.overlay_files, {})
        self.assertEqual(len(server.queue), 1)
        self.assertIn("边界消息", server.queue[0].message)
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
            channel=fake_channel, profile="fuyu", channel_wait_for=("Aki",)
        )
        names = {str(tool.get("name") or "") for tool in server._external_tools()}
        self.assertIn(WAIT_OTHER_TOOL, names)

        await server.stop()
        self.server = None
        plain = await self.start_server(channel=FakeChannel())
        plain_names = {str(tool.get("name") or "") for tool in plain._external_tools()}
        self.assertNotIn(WAIT_OTHER_TOOL, plain_names)

    async def test_unwaited_final_is_replaced_by_wait_guard_turn(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel,
            profile="fuyu",
            channel_wait_for=("Aki",),
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
        state = server._agent_channel_state("aki")
        state.active = True
        guard_turns: list[Turn] = []
        original_enqueue = server._enqueue_wait_guard_turn

        def capture_guard(turn: Turn, targets: list[tuple[str, str, str]]) -> None:
            original_enqueue(turn, targets)
            guard_turns.append(server.queue[-1])
            state.active = False

        with mock.patch.object(
            server, "_enqueue_wait_guard_turn", side_effect=capture_guard
        ):
            result = await channel_send(server, "hello")

        self.assertEqual(len(guard_turns), 1)
        guard_turn = guard_turns[0]
        self.assertEqual(guard_turn.origin, WAIT_GUARD_ORIGIN)
        self.assertIn("这是系统提示，不是用户消息", guard_turn.message)
        self.assertIn("这些目标是:Aki", guard_turn.message)
        self.assertEqual(guard_turn.wait_guard_targets, (("aki", "Aki", "Aki"),))
        self.assertNotIn("channel reply", result.final_texts())
        self.assertTrue(any("echo:" in text for text in result.final_texts()))
        ended = [
            event
            for event in result.events_of("turn")
            if event.get("phase") == "ended"
        ]
        self.assertTrue(any(event.get("ignored") == "true" for event in ended))

    async def test_idle_wait_target_does_not_trigger_wait_guard(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel,
            profile="fuyu",
            channel_wait_for=("Aki",),
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
        with mock.patch.object(server, "_enqueue_wait_guard_turn") as enqueue:
            result = await channel_send(server, "hello")
        enqueue.assert_not_called()
        self.assertEqual(result.final_texts(), ["channel reply"])

    async def test_wait_guard_targets_include_queued_work(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(),
            profile="fuyu",
            channel_wait_for=("Aki",),
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
        state = server._agent_channel_state("aki")
        state.queue_depth = 1
        turn = Turn(
            turn_id="wait-guard-queue-depth",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_participants=frozenset({"aki"}),
            state="running",
        )
        self.assertEqual(
            server._unfinished_wait_targets(turn),
            [("aki", "Aki", "Aki")],
        )

    async def test_wait_guard_turn_merges_consumed_message_into_human_role(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        human_prompt = server._channel_prompt("Master", "master", "原始问题")
        guard_prompt = server._wait_guard_notice([("aki", "Aki", "Aki")])
        self._write_channel_conversation(
            server,
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "entry-human",
                    "parentId": None,
                    "timestamp": "2026-10-28T00:00:00Z",
                    "message": {
                        "id": "ui-human",
                        "role": "user",
                        "content": [{"type": "text", "text": human_prompt}],
                        "timestamp": 1792942663000,
                    },
                },
                {
                    "type": "message",
                    "id": "entry-guard",
                    "parentId": "entry-human",
                    "timestamp": "2026-10-28T00:00:01Z",
                    "message": {
                        "id": "ui-guard",
                        "role": "user",
                        "content": [{"type": "text", "text": guard_prompt}],
                        "timestamp": 1792942664000,
                    },
                },
            ],
        )
        turn = Turn(
            turn_id="overlay-wait-guard-consumed",
            conn=server.channel_conn,
            message=guard_prompt,
            include_reasoning=False,
            client_message_id=None,
            origin=WAIT_GUARD_ORIGIN,
            origin_sender_id="master",
            channel_sender="Master",
            channel_participants=frozenset({"aki"}),
            wait_guard_targets=(("aki", "Aki", "Aki"),),
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "执行中补充")
        next(iter(turn.overlay_files.values())).unlink()
        await server._finalize_turn_overlays(turn)

        self.assertEqual(server.queue, [])
        rows = self._conversation_user_rows(server)
        human_row = next(row for row in rows if row.get("id") == "entry-human")
        guard_row = next(row for row in rows if row.get("id") == "entry-guard")
        self.assertEqual(len(human_row["message"]["content"]), 2)
        self.assertIn("执行中补充", human_row["message"]["content"][1]["text"])
        self.assertEqual(
            guard_row["message"]["content"],
            [{"type": "text", "text": guard_prompt}],
        )
        server.active = None

    async def test_aki_never_registers_wait_other(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(), profile="aki", channel_wait_for=("Fuyu",)
        )
        names = {str(tool.get("name") or "") for tool in server._external_tools()}
        self.assertNotIn(WAIT_OTHER_TOOL, names)
        turn = Turn(
            turn_id="aki-no-wait-other",
            conn=server.channel_conn,
            message="",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id="master",
            channel_message_seq=1,
            channel_sender="Master",
            channel_text="hello",
            channel_participants=frozenset({"fuyu"}),
        )
        block = await server._refresh_channel_turn(turn)
        self.assertNotIn("<wait-other", block)

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

    async def test_hidden_wait_tool_flushes_pre_tool_progress(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki",)
        )
        turn = Turn(
            turn_id="hidden-progress",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            channel_sender="Haru",
            channel_message_seq=1,
            state="running",
        )
        server.active = turn
        stamp = "2026-10-21T00:00:00Z"
        await server.channel_conn.send(
            make_event(turn.turn_id, 1, "turn_started", {}, stamp)
        )
        await server._emit(
            turn,
            "assistant_delta",
            {"text": "秋姐被点名了，我先听她说完再接。"},
        )
        await server._emit_tool_call_once(turn, "wait-hidden", WAIT_OTHER_TOOL, {})
        await wait_until(
            lambda: any(
                event.get("phase") == "progress"
                for event in fake_channel.events_of("turn")
            )
        )
        await server._emit(
            turn,
            "assistant_delta",
            {"text": "哼哼，那必须说得过去呀！"},
        )
        await server._emit(
            turn,
            "assistant_message",
            {"content": "哼哼，那必须说得过去呀！", "text": "哼哼，那必须说得过去呀！"},
        )
        await server._emit(turn, "turn_end", {})
        await wait_until(
            lambda: any(
                event.get("phase") == "final"
                for event in fake_channel.events_of("turn")
            )
        )

        turns = fake_channel.events_of("turn")
        progress = [event for event in turns if event.get("phase") == "progress"]
        finals = [event for event in turns if event.get("phase") == "final"]
        self.assertEqual(len(progress), 1)
        self.assertIn("秋姐被点名了", progress[0]["text"])
        self.assertEqual(len(finals), 1)
        self.assertNotIn("秋姐被点名了", finals[0]["text"])
        self.assertIn("哼哼，那必须说得过去呀", finals[0]["text"])
        self.assertFalse(
            any(event.get("kind") == "tool" for event in fake_channel.events)
        )
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

    async def test_wait_user_is_registered_with_channel_and_hidden(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        names = {str(tool.get("name") or "") for tool in server._external_tools()}
        self.assertIn(WAIT_USER_TOOL, names)
        self.assertEqual(WAIT_USER_TOOL, POLICY_WAIT_USER_TOOL)
        turn = Turn(
            turn_id="hidden-wait-user",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            state="running",
        )
        server.active = turn
        await server._emit_tool_call_once(turn, "wait-user-call", WAIT_USER_TOOL, {})
        await server._emit_tool_result_once(
            turn, "wait-user-call", {"status": "success", "tool_return": "hidden"}
        )
        await asyncio.sleep(0.05)
        self.assertEqual(fake_channel.events, [])
        self.assertEqual(turn.tool_names["wait-user-call"], WAIT_USER_TOOL)
        server.active = None

    async def test_wait_user_is_not_registered_without_channel(self) -> None:
        server = await self.start_server()
        names = {str(tool.get("name") or "") for tool in server._external_tools()}
        self.assertNotIn(WAIT_USER_TOOL, names)

    async def test_wait_user_returns_same_sender_and_does_not_requeue(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        sender_id = server._member_key({}, "Master", "master")
        turn = Turn(
            turn_id="wait-user-same-sender",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id=sender_id,
            channel_sender="Master",
            state="running",
        )
        server.active = turn
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            task = asyncio.create_task(
                server._handle_wait_user_tool(turn, "wait-user", {"timeout_s": 1})
            )
            await asyncio.sleep(0.02)
            await server._on_channel_text("Master", "master", "补充一句", {})
            await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(len(captured), 1)
        self.assertIn("WAIT_USER_RESULT", captured[0])
        self.assertIn("补充一句", captured[0])
        self.assertTrue(turn.waited_user_seqs)
        self.assertEqual(turn.overlay_files, {})
        self.assertFalse(turn.wait_user_call_active)
        turn.tool_result_events += 1
        server._queue_unhandled_busy_messages(turn)
        self.assertEqual(server.queue, [])
        server.active = None

    async def test_wait_user_ignores_other_senders_and_agent_messages(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        sender_id = server._member_key({}, "Master", "master")
        turn = Turn(
            turn_id="wait-user-filter",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id=sender_id,
            channel_sender="Master",
            state="running",
        )
        server.active = turn
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            task = asyncio.create_task(
                server._handle_wait_user_tool(turn, "wait-user", {"timeout_s": 1})
            )
            await asyncio.sleep(0.02)
            await server._on_channel_text("Other", "other", "别人的话", {})
            await server._on_channel_text(
                "Fuyu",
                "fuyu",
                "智能体的话",
                {"schema": "akid.v1", "kind": "turn", "phase": "final"},
            )
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            await server._on_channel_text("Master", "master", "同一用户", {})
            await asyncio.wait_for(task, timeout=2.0)
        self.assertEqual(len(captured), 1)
        self.assertIn("同一用户", captured[0])
        self.assertNotIn("别人的话", captured[0])
        self.assertNotIn("智能体的话", captured[0])
        server.active = None

    async def test_wait_user_only_consumes_messages_that_arrive_during_wait(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        sender_id = server._member_key({}, "Master", "master")
        turn = Turn(
            turn_id="wait-user-eligibility",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id=sender_id,
            channel_sender="Master",
            state="running",
        )
        server.active = turn
        await server._on_channel_text("Master", "master", "等待前补充")
        before_wait = next(iter(turn.overlay_files.values()))
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            task = asyncio.create_task(
                server._handle_wait_user_tool(turn, "wait-user-scope", {"timeout_s": 1})
            )
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            await server._on_channel_text("Master", "master", "等待中补充")
            await asyncio.wait_for(task, timeout=2.0)
        self.assertIn("等待中补充", captured[0])
        self.assertNotIn("等待前补充", captured[0])
        self.assertTrue(before_wait.exists())
        self.assertEqual(len(turn.overlay_files), 1)
        self.assertEqual(next(iter(turn.overlay_files.values())), before_wait)
        self.assertEqual(len(turn.waited_user_seqs), 1)
        self.assertTrue(turn.waited_user_seqs <= turn.wait_user_eligible_seqs)
        server.active = None

    async def test_wait_user_timeout_and_repeat_call(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        sender_id = server._member_key({}, "Master", "master")
        turn = Turn(
            turn_id="wait-user-repeat",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_sender_id=sender_id,
            channel_sender="Master",
            state="running",
        )
        server.active = turn
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            await asyncio.wait_for(
                server._handle_wait_user_tool(turn, "wait-user-timeout", {"timeout_s": 1}),
                timeout=2.0,
            )
            self.assertIn("WAIT_USER_TIMEOUT", captured[-1])
            task = asyncio.create_task(
                server._handle_wait_user_tool(turn, "wait-user-again", {"timeout_s": 1})
            )
            await asyncio.sleep(0.02)
            await server._on_channel_text("Master", "master", "第二次补充", {})
            await asyncio.wait_for(task, timeout=2.0)
        self.assertIn("第二次补充", captured[-1])
        server.active = None

    async def test_wait_user_rejects_agent_turn_and_bad_timeout(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        captured: list[str] = []

        async def capture(_tool_call_id, content, *, is_error=False):
            captured.append(content[0]["text"])

        turn = Turn(
            turn_id="wait-user-agent-turn",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            origin_is_agent=True,
            origin_sender_id="fuyu",
            channel_sender="Fuyu",
            state="running",
        )
        server.active = turn
        with mock.patch.object(
            server.backend, "send_external_tool_result", side_effect=capture
        ):
            await server._handle_wait_user_tool(turn, "wait-agent", {"timeout_s": 1})
            self.assertIn("WAIT_USER_UNAVAILABLE", captured[-1])
            human = Turn(
                turn_id="wait-user-bad-timeout",
                conn=server.channel_conn,
                message="working",
                include_reasoning=False,
                client_message_id=None,
                origin="mqtt",
                origin_sender_id="master",
                state="running",
            )
            await server._handle_wait_user_tool(human, "wait-bad", {"timeout_s": 0})
            self.assertIn("WAIT_USER_INVALID_TIMEOUT", captured[-1])
            await server._handle_wait_user_tool(human, "wait-bad", {"timeout_s": 26})
            self.assertIn("WAIT_USER_INVALID_TIMEOUT", captured[-1])
        server.active = None

    async def test_wait_user_broadcasts_and_clears_waiting_state(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        await server._set_wait_user_active(True)
        self.assertTrue(server._wait_user_active)
        self.assertIn(server._channel_self_key(), server._channel_waiting_user_ids())
        self.assertIn("true", server._channel_live_metadata()["waitingUser"])
        waiting_events = [
            event for event in fake_channel.events if event.get("kind") == "wait_user"
        ]
        self.assertEqual(waiting_events[-1]["waitingUser"], "true")

        await server._set_wait_user_active(False)
        self.assertFalse(server._wait_user_active)
        self.assertNotIn(server._channel_self_key(), server._channel_waiting_user_ids())
        waiting_events = [
            event for event in fake_channel.events if event.get("kind") == "wait_user"
        ]
        self.assertEqual(waiting_events[-1]["waitingUser"], "false")

    async def test_wait_other_broadcasts_and_clears_waiting_state(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, channel_wait_for=("Aki", "Fuyu")
        )
        await server._set_wait_other_active(True, ("Aki", "Fuyu"))
        self.assertTrue(server._wait_other_active)
        metadata = server._channel_live_metadata()
        self.assertEqual(metadata["waitingOther"], "true")
        self.assertEqual(metadata["waitingOtherTargets"], "Aki,Fuyu")
        waiting_events = [
            event for event in fake_channel.events if event.get("kind") == "wait_other"
        ]
        self.assertEqual(waiting_events[-1]["waitingOther"], "true")
        self.assertEqual(waiting_events[-1]["waitingOtherTargets"], "Aki,Fuyu")

        await server._set_wait_other_active(False)
        self.assertFalse(server._wait_other_active)
        self.assertEqual(server._channel_live_metadata()["waitingOther"], "false")
        waiting_events = [
            event for event in fake_channel.events if event.get("kind") == "wait_other"
        ]
        self.assertEqual(waiting_events[-1]["waitingOther"], "false")

    async def test_wait_user_state_event_updates_remote_waiter(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        fuyu_id = server._member_key({}, "Fuyu", "fuyu")
        history_before = len(server._channel_history)
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "waiting for user",
            {
                "schema": "akid.v1",
                "kind": "wait_user",
                "protocolVersion": str(1),
                "waitingUser": "true",
            },
        )
        self.assertIn(fuyu_id, server._channel_waiting_user_ids())
        self.assertEqual(len(server._channel_history), history_before)
        await server._on_channel_text(
            "Fuyu",
            "fuyu",
            "stopped waiting for user",
            {
                "schema": "akid.v1",
                "kind": "wait_user",
                "protocolVersion": str(1),
                "waitingUser": "false",
            },
        )
        self.assertNotIn(fuyu_id, server._channel_waiting_user_ids())

    async def test_user_message_without_mention_only_wakes_waiting_members(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        fuyu_id = server._member_key({}, "Fuyu", "fuyu")
        server._channel_waiting_user[fuyu_id] = time.monotonic()
        with mock.patch.object(server, "_enqueue_channel_turn") as enqueue:
            await server._on_channel_text("Master", "master", "普通补充", {})
            enqueue.assert_not_called()
            await server._on_channel_text("Master", "master", "@Aki 点名补充", {})
            enqueue.assert_called_once()

    async def test_working_member_is_not_extra_woken_by_wait_user_routing(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        fuyu_id = server._member_key({}, "Fuyu", "fuyu")
        server._channel_waiting_user[fuyu_id] = time.monotonic()
        turn = Turn(
            turn_id="working-before-waiter-rule",
            conn=server.channel_conn,
            message="working",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
            state="running",
        )
        server.active = turn
        with mock.patch.object(server, "_enqueue_channel_turn") as enqueue:
            await server._on_channel_text("Master", "master", "继续补充", {})
            enqueue.assert_not_called()
        self.assertTrue(any(item.get("text") == "继续补充" for item in turn.busy_messages))
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
        self.assertNotIn("wake-reminder:", prompt)
        self.assertNotIn("reply-target", prompt)
        self.assertNotIn("<channel-turn", prompt)

    async def test_legacy_dynamic_execution_lock_block_waits_for_migration(self) -> None:
        agent_path = self._write_test_agent(
            "BASE PROMPT\n\n<execution-lock authorized=\"true\">old</execution-lock>\n"
        )
        server = await self.start_server(channel=FakeChannel())
        await channel_send(server, "hello")
        system = self._agent_system(agent_path)
        self.assertIn("BASE PROMPT", system)
        self.assertIn("<execution-lock", system)

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
        server = await self.start_server(channel=FakeChannel())
        result = await channel_send(server, "LOCK_ACQUIRE")
        self.assertIn("EXECUTION_LOCK_ACQUIRED", result.final_text)
        await wait_until(lambda: not server.execution_lock.status()["held"])

        result = await channel_send(server, "LOCK_ACQUIRE_RELEASE")
        self.assertIn("EXECUTION_LOCK_ACQUIRED", result.final_text)
        self.assertIn("EXECUTION_LOCK_RELEASED", result.final_text)
        self.assertFalse(server.execution_lock.status()["held"])

    async def test_execution_lock_contention_returns_owner_and_ends_turn(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        holder = server.execution_lock
        acquired, _ = holder.acquire(
            profile="fuyu", agent_id="fuyu-agent", turn_id="foreign-turn"
        )
        self.assertTrue(acquired)
        try:
            result = await channel_send(server, "LOCK_ACQUIRE")
            self.assertIn("EXECUTION_LOCK_UNAVAILABLE", result.final_text)
            self.assertIn("fuyu", result.final_text)
            self.assertTrue(holder.status()["held"])
        finally:
            holder.release(profile="fuyu", turn_id="foreign-turn")

    async def test_execution_lock_is_released_when_turn_is_cancelled(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        task = asyncio.create_task(
            channel_send(server, "LOCK_ACQUIRE_SLOW", client_message_id="lock-slow")
        )
        await wait_until(lambda: server.execution_lock.status()["held"])
        await server._on_channel_text(
            "Master",
            "master",
            "",
            {
                "schema": "akid.v1",
                "kind": "cancel",
                "clientMessageId": "lock-slow",
            },
        )
        await task
        await wait_until(lambda: not server.execution_lock.status()["held"])

    async def test_unlocked_non_read_only_tool_is_denied(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        result = await channel_send(server, "unlocked-bash")
        self.assertIn(
            "error", [event.get("toolStatus") for event in result.events_of("tool")]
        )
        self.assertIn("tool denied", result.final_text)

    async def test_release_tool_is_auto_denied_without_approval(self) -> None:
        server = await self.start_server(channel=FakeChannel())
        result = await channel_send(server, "release-tool")
        self.assertFalse(result.events_of("approval"))
        self.assertIn(
            "error", [event.get("toolStatus") for event in result.events_of("tool")]
        )
        self.assertIn("manual lock release is not available", result.final_text)

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
        overlay = overlay_text(server)
        self.assertIn('source="user"', overlay)
        self.assertIn('new="true"', overlay)
        system = self._agent_system(agent_path)
        self.assertNotIn('source="user"', system)
        client = await self.client()
        await client.hello()
        for method in ("aki.chat.send", "aki.chat.cancel", "aki.chat.history"):
            with self.assertRaises(RpcError) as removed:
                await client.call(method, {"message": "local hello"})
            self.assertEqual(removed.exception.code, ErrorCode.METHOD_NOT_FOUND)

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
        self.assertNotIn("human bio", prompt)
        roster = render_channel_roster(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
        )
        self.assertIn('kind="user"', roster)
        self.assertIn("human bio", roster)
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

    async def test_group_messages_enter_public_memory(self) -> None:
        server = await self.start_server()
        server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="first public message",
            from_agent=False,
            message_id="public-1",
        )
        server._record_channel_message(
            sender="Fuyu",
            sender_id="fuyu",
            text="second public message",
            from_agent=True,
            message_id="public-2",
        )
        server._record_channel_message(
            sender="alice",
            sender_id="alice-1",
            text="first public message",
            from_agent=False,
            message_id="public-1",
        )

        self.assertEqual(server.public_memory.status()["records"], 2)
        records = server.public_memory.snapshot()
        self.assertEqual(
            [record["text"] for record in records],
            ["first public message", "second public message"],
        )
        block = server._public_memory_prompt_block()
        self.assertIn("<shared-memory", block)
        self.assertIn('records="2"', block)
        self.assertIn(str(server.public_memory.path), block)

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

    async def test_rollback_keeps_human_messages_before_the_temporary_turn(self) -> None:
        await self.start_server()
        assert self.server is not None
        directory = self.server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        rows = [
            {"type": "session"},
            {
                "type": "message",
                "id": "human-a",
                "message": {
                    "id": "ui-a",
                    "role": "user",
                    "content": [{"type": "text", "text": "A"}],
                },
            },
            {
                "type": "message",
                "id": "human-b",
                "message": {
                    "id": "ui-b",
                    "role": "user",
                    "content": [{"type": "text", "text": "B"}],
                },
            },
        ]
        messages.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        keep = messages.read_bytes()
        snapshot = self.server._snapshot_conversation("temporary C")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["last_entry_id"], "human-b")
        c_row = {
            "type": "message",
            "id": "temporary-c",
            "message": {
                "id": "ui-c",
                "role": "user",
                "content": [{"type": "text", "text": "temporary C"}],
            },
        }
        with messages.open("ab") as handle:
            handle.write((json.dumps(c_row, ensure_ascii=False) + "\n").encode())
            handle.write(
                (
                    json.dumps(
                        {
                            "type": "message",
                            "id": "temporary-c-reply",
                            "message": {
                                "id": "ui-c-reply",
                                "role": "assistant",
                                "content": [{"type": "text", "text": "C reply"}],
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
        await self.server._restore_conversation_snapshot(snapshot)
        self.assertEqual(messages.read_bytes(), keep)
        self.assertIn(b'"id": "human-a"', keep)
        self.assertIn(b'"id": "human-b"', keep)
        self.assertNotIn(b"temporary-c", messages.read_bytes())

    async def test_rollback_refuses_when_boundary_prompt_does_not_match(self) -> None:
        await self.start_server()
        assert self.server is not None
        directory = self.server._conversation_dir()
        directory.mkdir(parents=True, exist_ok=True)
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        messages.write_text(
            '{"type":"session"}\n'
            '{"type":"message","id":"human-a","message":{"id":"ui-a","role":"user","content":[{"type":"text","text":"A"}]}}\n',
            encoding="utf-8",
        )
        conversation.write_text('{"id":"default"}\n', encoding="utf-8")
        snapshot = self.server._snapshot_conversation("temporary C")
        self.assertIsNotNone(snapshot)
        with messages.open("ab") as handle:
            handle.write(
                b'{"type":"message","id":"human-b","message":{"id":"ui-b","role":"user","content":[{"type":"text","text":"B"}]}}\n'
            )
        before = messages.read_bytes()
        await self.server._restore_conversation_snapshot(snapshot)
        self.assertEqual(messages.read_bytes(), before)
        self.assertIsNotNone(
            self.server._channel_transcript_status.get("last_restore_error")
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

    async def test_user_channel_turn_persists_without_roster_or_journal(self) -> None:
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
            self.assertNotIn("<channel-context>", prompt)
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
            self.assertNotIn("wake-reminder:", text)
            self.assertIn("sender: alice", text)
        self.assertIn("message:\nfirst turn", rows[0]["message"]["content"][0]["text"])
        self.assertIn("message:\nsecond turn", rows[1]["message"]["content"][0]["text"])

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
            row = {
                "type": "message",
                "id": f"ephemeral-{len(messages.read_text().splitlines())}",
                "message": {
                    "role": "user",
                    "content": content
                    if isinstance(content, list)
                    else [{"type": "text", "text": content}],
                },
            }
            with messages.open("ab") as handle:
                handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
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
            await wait_until(lambda: server.active is None and not server.queue)
        self.assertTrue(server.bridge_restores)
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
            row_content = (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": str(content)}]
            )
            row = {
                "type": "message",
                "id": f"ephemeral-multimodal-{len(messages.read_text().splitlines())}",
                "message": {"role": "user", "content": row_content},
            }
            with messages.open("ab") as handle:
                handle.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
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
            await wait_until(lambda: server.active is None and not server.queue)
        self.assertTrue(server.bridge_restores)

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
        prompt = server._channel_prompt("alice", "alice-1", "hello everyone")
        self.assertNotIn("Fuyu", prompt)
        roster = render_channel_roster(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
        )
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
        self.assertNotIn("<reply-target>", own)
        self.assertNotIn("未点名你", own)
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="Fuyu offline",
                sender_id="fuyu",
                metadata={**metadata, "kind": "offline"},
            )
        )
        after_offline = render_channel_roster(
            self_member=server._channel_self_member(),
            members=server._channel_member_views(),
        )
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
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
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
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
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
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, approval_timeout=5.0
        )

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
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, approval_timeout=5.0
        )

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
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel, approval_timeout=5.0
        )
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
            server = await self.start_server(
                channel=FakeChannel(),
                sandbox_enabled=True,
                writable_state=state,
            )
            client = await self.client()
            await client.hello()
            await server._on_channel_text("Master", "master", "slow")
            await wait_until(lambda: server.active is not None)
            slow = server.active
            server._enqueue_channel_turn(
                "Fuyu", "fuyu", "after policy", origin_is_agent=True
            )
            await wait_until(lambda: bool(server.queue))
            queued = server.queue[0]

            added = await client.call("aki.fs.writable.add", {"path": str(allowed)})
            self.assertEqual(added["interrupted_turn_id"], slow.turn_id)
            await wait_until(lambda: server.active is None and not server.queue, timeout=15.0)
            self.assertTrue(
                any(
                    event.get("kind") == "error"
                    for event in server.channel.events
                )
            )
            self.assertEqual(server.channel.final_texts()[-1], "channel reply")
        finally:
            await asyncio.to_thread(__import__("shutil").rmtree, allowed, True)

    async def test_sandbox_unavailable_blocks_backend(self) -> None:
        server = await self.start_server(
            channel=FakeChannel(),
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
        await server._on_channel_text("Master", "master", "hello")
        self.assertIsNone(server.active)
        self.assertEqual(len(server.queue), 1)

    async def test_channel_text_cannot_manage_writable_dirs(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(
            channel=fake_channel,
            sandbox_enabled=True,
            writable_state=self.root / "writable.json",
        )
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
        image_path = self.root / "pixel.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        result = await channel_send(server, f"EXTERNAL_IMAGE:{image_path}")
        self.assertFalse(result.events_of("approval"))
        self.assertEqual(len(fake_channel.images), 1)
        self.assertEqual(fake_channel.images[0]["caption"], "fake caption")
        self.assertEqual(fake_channel.images[0]["mime"], "image/png")
        self.assertIn("external tool result: sent tokifactor image", result.final_text)

    async def test_tokifactor_image_external_tool_rejects_unregistered_source(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel)
        await wait_until(lambda: server._external_tools_registered)
        outside = Path(tempfile.mkdtemp(prefix="akid-no-image-", dir=str(Path.home() / ".local" / "share")))
        image_path = outside / "pixel.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        try:
            result = await channel_send(server, f"EXTERNAL_IMAGE:{image_path}")
            self.assertEqual(fake_channel.images, [])
            self.assertIn("registered writable directory", result.final_text)
        finally:
            await asyncio.to_thread(__import__("shutil").rmtree, outside, True)

    async def test_tokifactor_image_external_tool_can_require_approval(self) -> None:
        fake_channel = FakeChannel()
        server = await self.start_server(channel=fake_channel, image_require_approval=True)
        await wait_until(lambda: server._external_tools_registered)
        image_path = self.root / "pixel-approval.png"
        image_path.write_bytes(
            __import__("base64").b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        task = asyncio.create_task(
            channel_send(server, f"EXTERNAL_IMAGE:{image_path}")
        )
        approval = await wait_for_approval(server)
        self.assertEqual(approval["tool_name"], "tokifactor_send_image")
        await respond_approval(server, approval["approval_id"], allow=True)
        await task
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
                    channel=FakeChannel(),
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin="/definitely/missing/spectacle",
                    screenshot_output_dir=output_dir,
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                result = await channel_send(server, "EXTERNAL_SCREENSHOT")
                self.assertFalse(result.events_of("approval"))
                self.assertIn("screenshot captured path=", result.final_text)
                self.assertIn("display=wayland-2", result.final_text)
                self.assertIn("backend=grim", result.final_text)
                self.assertIn("width=1 height=1", result.final_text)
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
                    channel=FakeChannel(),
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin="/definitely/missing/spectacle",
                    screenshot_output_dir=self.root / "screenshots-approval",
                    screenshot_wayland_display="wayland-1",
                    screenshot_require_approval=True,
                )
                await wait_until(lambda: server._external_tools_registered)
                task = asyncio.create_task(
                    channel_send(server, "EXTERNAL_SCREENSHOT")
                )
                approval = await wait_for_approval(server)
                self.assertEqual(
                    approval["tool_name"], "desktop_take_screenshot"
                )
                await respond_approval(server, approval["approval_id"], allow=True)
                result = await task
                self.assertIn("screenshot captured path=", result.final_text)
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
                channel=FakeChannel(),
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

            task = asyncio.create_task(
                channel_send(server, "EXTERNAL_IMAGE_GENERATION")
            )
            approval = await wait_for_approval(server)
            self.assertEqual(approval["tool_name"], "generate_image")
            await respond_approval(server, approval["approval_id"], allow=True)
            result = await task
            self.assertIn("image generated", result.final_text)
            self.assertIn("tokifactor_send_image", result.final_text)
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
                    channel=FakeChannel(),
                    grim_bin="/definitely/missing/grim",
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "screenshots-kde",
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                result = await channel_send(server, "EXTERNAL_SCREENSHOT")
                self.assertIn("backend=spectacle", result.final_text)
                self.assertIn("display=wayland-1", result.final_text)
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
                    channel=FakeChannel(),
                    grim_bin=str(self._fake_grim()),
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1_BLACK)),
                    screenshot_output_dir=self.root / "screenshots-kde-fallback",
                    screenshot_wayland_display="wayland-1",
                )
                await wait_until(lambda: server._external_tools_registered)
                result = await channel_send(server, "EXTERNAL_SCREENSHOT")
                self.assertIn("black screenshot", result.final_text)
                self.assertNotIn("backend=grim", result.final_text)
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
                    channel=FakeChannel(),
                    live_context_enabled=True,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "live-shots",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                first_result = await channel_send(server, "hello")
                self.assertFalse(first_result.events_of("approval"))
                self.assertEqual(first_result.final_text, "channel reply")
                first_system = self._agent_system(agent_path)
                first_overlay = overlay_text(server)
                self.assertEqual(first_overlay.count("<live-context"), 1)
                self.assertEqual(first_overlay.count("</live-context>"), 1)
                self.assertNotIn("<live-context", first_system)
                self.assertIn("Fake Window &lt;test&gt;", first_overlay)
                self.assertIn("firefox", first_overlay)
                self.assertIn("python", first_overlay)
                self.assertIn("echo { CORE_MEMORY }", first_overlay)
                self.assertIn("live/desktop-", first_overlay)
                live_files = list(
                    (self.root / "live-shots" / "live").glob("desktop-*.png")
                )
                self.assertEqual(len(live_files), 1)

                await asyncio.sleep(1.05)
                second_result = await channel_send(server, "again")
                self.assertEqual(second_result.final_text, "channel reply")
                second_overlay = overlay_text(server)
                self.assertEqual(second_overlay.count("<live-context"), 1)
                self.assertEqual(second_overlay.count("</live-context>"), 1)
                self.assertNotEqual(first_overlay, second_overlay)
                client = await self.client()
                await client.hello()
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
                    channel=FakeChannel(),
                    live_context_enabled=True,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(failure),
                    screenshot_output_dir=self.root / "live-shots-failure",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                await channel_send(server, "hello")
                system = self._agent_system(agent_path)
                overlay = overlay_text(server)
                self.assertIn("<live-context", overlay)
                self.assertNotIn("<screenshot", overlay)
                self.assertIn("echo hi", overlay)
                self.assertNotIn("<live-context", system)
                client = await self.client()
                await client.hello()
                status = await client.call("aki.status", {})
                self.assertFalse(status["live_context"]["last_sections"]["screenshot"])
                self.assertTrue(status["live_context"]["last_sections"]["processes"])
        finally:
            for sock in sockets:
                sock.close()

    async def test_live_context_budget_and_disable_are_overlay_only(self) -> None:
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
                    channel=FakeChannel(),
                    live_context_enabled=True,
                    live_context_budget_tokens=2048,
                    ps_bin=str(self._fake_ps()),
                    zsh_history_path=history,
                    spectacle_bin=str(self._fake_spectacle(PNG_1X1)),
                    screenshot_output_dir=self.root / "live-shots-budget",
                    screenshot_wayland_display="wayland-1",
                    live_context_timeout=2.0,
                )
                await channel_send(server, "hello")
                overlay = overlay_text(server)
                block = overlay[
                    overlay.index("<live-context") : overlay.index(
                        "</live-context>"
                    )
                    + len("</live-context>")
                ]
                self.assertLessEqual(estimate_tokens(block), 2048)
                self.assertIn("<live-context", self._agent_system(agent_path))

                disabled_path = self._write_test_agent(
                    "BASE\n\n<live-context refreshed_at=\"old\">stale</live-context>\n"
                )
                await server.stop()
                self.server = None
                server = await self.start_server(
                    channel=FakeChannel(), live_context_enabled=False
                )
                cleared = self._agent_system(disabled_path)
                self.assertIn("<live-context", cleared)
                await channel_send(server, "hello")
                self.assertNotIn("<live-context", overlay_text(server))
                self.assertIn("<live-context", self._agent_system(disabled_path))
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
                overlay = overlay_text(server)
                self.assertEqual(overlay.count("<live-context"), 1)
                self.assertIn("echo channel", overlay)
                self.assertIn("<format-guard", overlay)
                self.assertLess(
                    overlay.index("<live-context"),
                    overlay.index("<format-guard"),
                )
                self.assertIn("不要使用 Markdown 标题", overlay)
                self.assertNotIn("<live-context", system)
        finally:
            for sock in sockets:
                sock.close()

    async def test_camera_external_tool_requires_approval_and_captures(self) -> None:
        output_dir = self.root / "camera"
        fake_device = self.root / "fake-video0"
        fake_device.write_text("device placeholder", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"FAKE_LETTA_CAMERA_DEVICE": str(fake_device)},
            clear=False,
        ):
            server = await self.start_server(
                channel=FakeChannel(),
                camera_output_dir=output_dir,
                camera_device=str(fake_device),
                ffmpeg_bin=str(self._fake_ffmpeg()),
                ffprobe_bin=None,
            )
        with mock.patch("engine.akid._is_video_device", return_value=True):
            await wait_until(lambda: server._external_tools_registered)
            task = asyncio.create_task(channel_send(server, "EXTERNAL_CAMERA"))
            approval = await wait_for_approval(server)
            self.assertEqual(approval["tool_name"], "camera_take_photo")
            self.assertTrue(approval["approval_id"].startswith("external-"))
            await respond_approval(server, approval["approval_id"], allow=True)
            result = await task
            self.assertEqual(len(result.events_of("approval")), 1)
            self.assertIn("photo captured path=", result.final_text)
        files = list(output_dir.glob("*.jpg"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].read_bytes().startswith(b"\xff\xd8\xff"))
        self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    async def test_camera_external_tool_deny_does_not_capture(self) -> None:
        output_dir = self.root / "camera-denied"
        fake_device = self.root / "fake-video0-denied"
        fake_device.write_text("device placeholder", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"FAKE_LETTA_CAMERA_DEVICE": str(fake_device)},
            clear=False,
        ):
            server = await self.start_server(
                channel=FakeChannel(),
                camera_output_dir=output_dir,
                camera_device=str(fake_device),
                ffmpeg_bin=str(self._fake_ffmpeg()),
                ffprobe_bin=None,
            )
        with mock.patch("engine.akid._is_video_device", return_value=True):
            await wait_until(lambda: server._external_tools_registered)
            task = asyncio.create_task(channel_send(server, "EXTERNAL_CAMERA"))
            approval = await wait_for_approval(server)
            await respond_approval(
                server,
                approval["approval_id"],
                allow=False,
                message="not now",
            )
            result = await task
            self.assertIn("camera photo not taken", result.final_text)
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
