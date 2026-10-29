from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine.akid import (
    GatewayConfig,
    GatewayServer,
    NullConnection,
    Turn,
)
from engine.aki_config import ImageGeneration
from engine.life_ledger import LifeLedger
from engine.schedule_store import ScheduleStore


class DummyBackend:
    def __init__(self) -> None:
        self.ready = False
        self.state = "ready"
        self.results: list[tuple[str, str, bool]] = []
        self.sent: list[str] = []

    async def send_external_tool_result(
        self, tool_call_id: str, content, *, is_error: bool = False
    ) -> None:
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
        self.results.append((tool_call_id, text, is_error))

    async def send_user(self, content: str) -> None:
        self.sent.append(content)

    async def approve_tool(self, request_id: str, *, allow: bool, message=None) -> None:
        return

    async def stop(self) -> None:
        return


class FakeChannel:
    def __init__(self) -> None:
        self.connected = True
        self.texts: list[tuple[str, dict]] = []

    async def publish_text(self, text: str, metadata=None) -> None:
        self.texts.append((text, dict(metadata or {})))

    def health(self) -> dict:
        return {"enabled": True, "connected": True}


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def notify(self, character: str, text: str) -> bool:
        self.messages.append((character, text))
        return True


def make_server(
    root: Path,
    *,
    profile: str = "aki",
    backend: DummyBackend | None = None,
    image_generation: ImageGeneration | None = None,
) -> GatewayServer:
    backend = backend or DummyBackend()
    config = GatewayConfig(
        socket_path=root / "akid.sock",
        profile=profile,
        agent_id=f"agent-{profile}",
        conversation_id="default",
        cwd=root,
        backend_dir=root / "backend",
        memory_dir=root / "memory",
        schedule_state=root / "schedules.json",
        ledger_state=root / "ledger.json",
        presence_state=root / "presence.json",
        execution_lock_path=root / "execution.lock",
        execution_lock_status_path=root / "execution.lock.json",
        mcp_native_tools_enabled=False,
        camera_enabled=False,
        screenshot_enabled=False,
        live_context_enabled=False,
        image_generation=image_generation,
    )
    return GatewayServer(config, backend_factory=lambda *_: backend)


class ExclusiveToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def test_tool_registration_is_profile_scoped(self) -> None:
        aki = make_server(self.root / "aki", profile="aki")
        fuyu = make_server(self.root / "fuyu", profile="fuyu")
        aki_names = {tool["name"] for tool in aki._external_tools()}
        fuyu_names = {tool["name"] for tool in fuyu._external_tools()}
        self.assertIn("aki_schedule_create", aki_names)
        self.assertIn("aki_zero_reply_budget", aki_names)
        self.assertIn("aki_ledger_add", aki_names)
        self.assertNotIn("aki_schedule_create", fuyu_names)
        self.assertNotIn("aki_zero_reply_budget", fuyu_names)
        self.assertNotIn("aki_ledger_add", fuyu_names)
        image = ImageGeneration(
            enabled=True,
            upstream_name="minimax",
            model="image-01",
            path="/image_generation",
            base_url="https://api.minimaxi.com/v1",
            url="https://api.minimaxi.com/v1/image_generation",
            api_key="test-key",
            use_proxy=False,
            output_dir=str(self.root / "images"),
            require_approval=False,
            timeout=30,
            max_bytes=1024 * 1024,
            aspect_ratio="1:1",
            prompt_optimizer=False,
            aigc_watermark=False,
        )
        haru = make_server(
            self.root / "haru", profile="haru", image_generation=image
        )
        aki_configured = make_server(
            self.root / "aki-configured", profile="aki", image_generation=image
        )
        fuyu_configured = make_server(
            self.root / "fuyu-configured", profile="fuyu", image_generation=image
        )
        haru_names = {tool["name"] for tool in haru._external_tools()}
        self.assertIn("generate_image", haru_names)
        self.assertNotIn("generate_image", aki_names)
        self.assertNotIn("generate_image", fuyu_names)
        self.assertNotIn(
            "generate_image",
            {tool["name"] for tool in aki_configured._external_tools()},
        )
        self.assertNotIn(
            "generate_image",
            {tool["name"] for tool in fuyu_configured._external_tools()},
        )

    async def test_wrong_profile_tool_call_returns_owner_error(self) -> None:
        backend = DummyBackend()
        server = make_server(self.root, profile="fuyu", backend=backend)
        server.active = Turn(
            turn_id="turn-1",
            conn=NullConnection(),
            message="x",
            include_reasoning=False,
            client_message_id=None,
        )
        await server._handle_external_tool(
            {
                "request": {
                    "tool_call_id": "call-1",
                    "tool_name": "aki_schedule_create",
                    "input": {"title": "x", "starts_at": "2030-01-01T00:00:00Z"},
                }
            }
        )
        self.assertEqual(len(backend.results), 1)
        _call_id, text, is_error = backend.results[0]
        self.assertTrue(is_error)
        self.assertIn("TOOL_OWNERSHIP_DENIED", text)
        self.assertIn("Aki", text)
        self.assertIn("@Aki", text)

    async def test_image_generation_is_denied_to_non_haru(self) -> None:
        backend = DummyBackend()
        server = make_server(self.root, profile="fuyu", backend=backend)
        server.active = Turn(
            turn_id="turn-1",
            conn=NullConnection(),
            message="x",
            include_reasoning=False,
            client_message_id=None,
        )
        await server._handle_external_tool(
            {
                "request": {
                    "tool_call_id": "call-1",
                    "tool_name": "generate_image",
                    "input": {"prompt": "a spring garden"},
                }
            }
        )
        self.assertEqual(len(backend.results), 1)
        _call_id, text, is_error = backend.results[0]
        self.assertTrue(is_error)
        self.assertIn("TOOL_OWNERSHIP_DENIED", text)
        self.assertIn("Haru", text)
        self.assertIn("@Haru", text)

    async def test_schedule_tool_crud(self) -> None:
        server = make_server(self.root)
        server.schedule_store = ScheduleStore(self.root / "schedules.json")
        starts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        created = await server._run_schedule_tool(
            "aki_schedule_create",
            {
                "title": "测试日程",
                "starts_at": starts,
                "timezone": "UTC",
                "repeat": "daily",
            },
        )
        record_id = created["schedule"]["id"]
        listed = await server._run_schedule_tool("aki_schedule_list", {})
        self.assertEqual(len(listed["schedules"]), 1)
        updated = await server._run_schedule_tool(
            "aki_schedule_update", {"id": record_id, "title": "更新"}
        )
        self.assertEqual(updated["schedule"]["title"], "更新")
        deleted = await server._run_schedule_tool(
            "aki_schedule_delete", {"id": record_id}
        )
        self.assertEqual(deleted["cancelled"]["status"], "cancelled")

    async def test_ledger_tool_roundtrip(self) -> None:
        server = make_server(self.root)
        server.life_ledger = LifeLedger(self.root / "ledger.json")
        added = await server._run_ledger_tool("aki_ledger_add", {"text": "喜欢热牛奶"})
        self.assertEqual(added["count"], 1)
        listed = await server._run_ledger_tool("aki_ledger_list", {})
        self.assertEqual(listed["records"][0]["text"], "喜欢热牛奶")
        deleted = await server._run_ledger_tool(
            "aki_ledger_delete", {"id": listed["records"][0]["id"]}
        )
        self.assertTrue(deleted["deleted"]["text"].startswith("喜欢"))

    async def test_budget_rpc_zeroes_target(self) -> None:
        target_backend = DummyBackend()
        target = make_server(self.root / "target", profile="fuyu", backend=target_backend)
        socket_path = self.root / "target" / "fuyu.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        unix_server = await asyncio.start_unix_server(
            target._handle_connection, path=str(socket_path)
        )
        try:
            source = make_server(self.root / "source", profile="aki")
            with mock.patch.object(
                source,
                "_resolve_budget_target",
                return_value=("fuyu", self.root / "fuyu.conf", socket_path),
            ):
                result = await source._run_budget_tool(
                    {"target": "Fuyu", "reason": "stop the loop"}
                )
            self.assertEqual(result["target"], "fuyu")
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(target._agent_initiation_remaining, 0)
        finally:
            unix_server.close()
            await unix_server.wait_closed()

    async def test_target_rejects_self_and_non_aki_source(self) -> None:
        target = make_server(self.root / "target", profile="aki")
        with self.assertRaises(Exception):
            target._zero_initiation_budget({"source_profile": "aki"})
        with self.assertRaises(Exception):
            target._zero_initiation_budget({"source_profile": "fuyu"})

    async def test_proactive_delivery_is_user_only(self) -> None:
        server = make_server(self.root)
        channel = FakeChannel()
        notifier = FakeNotifier()
        server.channel = channel
        server._layer_notifier = notifier
        turn = Turn(
            turn_id="proactive-1",
            conn=NullConnection(),
            message="x",
            include_reasoning=False,
            client_message_id=None,
            origin="proactive",
        )
        await server._publish_proactive_text(turn, "该休息一下了。")
        self.assertEqual(channel.texts[0][0], "该休息一下了。")
        self.assertEqual(channel.texts[0][1]["wake"], "user")
        self.assertEqual(notifier.messages[0][1], "该休息一下了。")
        await server._publish_proactive_text(turn, "[NO_REPLY]")
        self.assertEqual(len(channel.texts), 1)
        self.assertEqual(len(notifier.messages), 1)

    async def test_schedule_poll_queues_proactive_turn(self) -> None:
        server = make_server(self.root)
        server.schedule_store = ScheduleStore(self.root / "schedules.json")
        starts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        server.schedule_store.create(
            {"title": "刚刚", "starts_at": starts, "timezone": "UTC"}
        )
        await server._poll_schedules()
        self.assertEqual(len(server.queue), 1)
        self.assertEqual(server.queue[0].origin, "proactive")
        self.assertIn("trigger", server.queue[0].message)

    async def test_old_schedule_is_missed_without_queue(self) -> None:
        server = make_server(self.root)
        server.schedule_store = ScheduleStore(self.root / "schedules.json")
        server.config.schedule_missed_grace_seconds = 0
        starts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        server.schedule_store.create(
            {"title": "错过", "starts_at": starts, "timezone": "UTC"}
        )
        await server._poll_schedules()
        self.assertEqual(server.queue, [])
        records = server.schedule_store.list(status="all")
        self.assertEqual(records[0].status, "missed")

    async def test_tool_ownership_is_rendered_without_mutating_agent_record(self) -> None:
        server = make_server(self.root)
        agents = self.root / "backend" / "agents"
        agents.mkdir(parents=True)
        path = agents / "agent.json"
        original = "BASE\n"
        path.write_text(
            json.dumps({"id": "agent-aki", "system": original}),
            encoding="utf-8",
        )
        block = server._tool_ownership_block()
        self.assertIn("<tool-ownership", block)
        self.assertIn('profile="aki"', block)
        self.assertIn("aki_zero_reply_budget", block)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["system"], original
        )

    async def test_operation_journal_tracks_lifecycle(self) -> None:
        server = make_server(self.root)
        turn = Turn(
            turn_id="turn-journal",
            conn=NullConnection(),
            message="x",
            include_reasoning=False,
            client_message_id=None,
        )
        await server._emit_tool_call_once(turn, "call-1", "Bash", {"command": "sleep 1"})
        self.assertTrue(server.operation_journal.outstanding())
        await server._send_external_result("call-1", "done", False)
        self.assertFalse(server.operation_journal.outstanding())
        recent = server.operation_journal.recent()
        self.assertEqual(recent[-1]["tool"], "Bash")
        self.assertEqual(recent[-1]["status"], "completed")

    def test_health_exposes_new_sections(self) -> None:
        server = make_server(self.root)
        health = server._health()
        self.assertIn("tool_ownership", health)
        self.assertIn("proactive", health)
        self.assertIn("schedule", health["proactive"])
        self.assertIn("presence", health["proactive"])


if __name__ == "__main__":
    unittest.main()
