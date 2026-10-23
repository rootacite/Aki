from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from engine.akid import ChannelTranscriptEntry, GatewayConfig, GatewayServer
from engine.tokifactor import TextEnvelope
from engine.member_profile import (
    MAX_MEMBER_DESCRIPTION_UNITS,
    member_description_weight,
    member_name_key,
    normalize_member_kind,
    truncate_member_description,
)
from engine.member_identity import legacy_member_id
from tests.fake_mqtt_broker import FakeMqttBroker

ROOT = Path(__file__).resolve().parent.parent


async def wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


class DummyBackend:
    ready = True

    def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def wait_ready(self, timeout: float) -> bool:
        return True


def factory(on_message, on_ready, on_exit):
    return DummyBackend()


class ChannelMemberDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.broker = FakeMqttBroker()
        await self.broker.start()
        self.servers: list[GatewayServer] = []

    async def asyncTearDown(self) -> None:
        for server in reversed(self.servers):
            await server.stop()
        await self.broker.stop()
        self.tempdir.cleanup()

    def make_server(self, name: str, channel_id: str) -> GatewayServer:
        base = self.root / name
        member_id = legacy_member_id(name)
        config = GatewayConfig(
            socket_path=base / "akid.sock",
            profile="aki" if channel_id == "aki" else channel_id,
            agent_id=f"agent-{name}",
            conversation_id="default",
            cwd=ROOT,
            memory_dir=base / "memory",
            backend_dir=base / "backend",
            mqtt_enabled=True,
            mqtt_host="127.0.0.1",
            mqtt_port=self.broker.port,
            mqtt_topic="tokifactor/text",
            mqtt_magic="member-discovery-test",
            mqtt_sender_name=name,
            mqtt_sender_id=member_id,
            channel_id=member_id,
            channel_member_id=member_id,
            channel_name=name,
            channel_aliases=(name,),
            identity_registry_path=base / "identities.json",
            mqtt_state=base / "mqtt.conf",
            writable_state=base / "writable.conf",
            live_context_enabled=False,
            screenshot_enabled=False,
            camera_enabled=False,
        )
        return GatewayServer(config, backend_factory=factory)

    async def test_two_akids_discover_and_remove_offline_members(self) -> None:
        aki = self.make_server("Aki", "aki")
        fuyu = self.make_server("Fuyu", "fuyu")
        self.servers.extend([aki, fuyu])
        await aki.start()
        await fuyu.start()
        await wait_until(
            lambda: aki.channel is not None
            and fuyu.channel is not None
            and aki.channel.connected
            and fuyu.channel.connected
        )
        fuyu_id = legacy_member_id("Fuyu")
        aki_id = legacy_member_id("Aki")
        await wait_until(lambda: fuyu_id in aki._channel_members)
        await wait_until(lambda: aki_id in fuyu._channel_members)
        self.assertEqual(aki._channel_members[fuyu_id]["name"], "Fuyu")
        self.assertEqual(aki._channel_members[fuyu_id]["kind"], "agent")
        self.assertEqual(fuyu._channel_members[aki_id]["kind"], "agent")
        self.assertEqual(aki.active, None)
        self.assertEqual(aki.queue, [])
        self.assertEqual(fuyu.active, None)
        self.assertEqual(fuyu.queue, [])

        await fuyu.stop()
        self.servers.remove(fuyu)
        await wait_until(lambda: fuyu_id not in aki._channel_members)

    async def test_same_name_is_one_member_and_first_owner_keeps_profile(self) -> None:
        server = self.make_server("Aki", "aki")
        self.servers.append(server)
        await server.start()
        base = {
            "schema": "akid.v1",
            "kind": "status",
            "protocolVersion": "1",
            "agentId": "fuyu",
            "agent": "fuyu",
            "agentName": "Fuyu",
        }
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="first",
                sender_id="device-a",
                metadata={
                    **base,
                    "description": "owner bio",
                    "memberKind": "agent",
                },
            )
        )
        await server._on_channel_control(
            TextEnvelope(
                sender="fuyu",
                text="second",
                sender_id="device-b",
                metadata={
                    **base,
                    "agentName": "fuyu",
                    "description": "second bio",
                    "memberKind": "user",
                },
            )
        )
        fuyu_id = legacy_member_id("Fuyu")
        self.assertEqual(list(server._channel_members), [fuyu_id])
        member = server._channel_members[fuyu_id]
        self.assertEqual(member["name"], "Fuyu")
        self.assertEqual(member["description"], "owner bio")
        self.assertEqual(member["kind"], "agent")
        self.assertEqual(set(member["sources"]), {"device-a", "device-b"})

        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="first offline",
                sender_id="device-a",
                metadata={**base, "kind": "offline"},
            )
        )
        self.assertIn(fuyu_id, server._channel_members)

        await server._on_channel_control(
            TextEnvelope(
                sender="fuyu",
                text="second offline",
                sender_id="device-b",
                metadata={**base, "agentName": "fuyu", "kind": "offline"},
            )
        )
        self.assertNotIn(fuyu_id, server._channel_members)

    async def test_existing_agent_classification_is_migrated_when_name_is_user(self) -> None:
        server = self.make_server("Aki", "aki")
        self.servers.append(server)
        await server.start()
        server._channel_history = [
            ChannelTranscriptEntry(
                seq=1,
                sender="Acite Lee",
                sender_id="acite lee",
                text="old message",
                from_agent=True,
            )
        ]
        await server._on_channel_control(
            TextEnvelope(
                sender="Acite Lee",
                text="profile",
                sender_id="acite lee",
                metadata={
                    "schema": "akid.v1",
                    "kind": "status",
                    "protocolVersion": "1",
                    "agentId": "acite lee",
                    "agent": "acite lee",
                    "agentName": "Acite Lee",
                    "description": "human bio",
                    "memberKind": "user",
                },
            )
        )
        self.assertFalse(server._channel_history[0].from_agent)

    async def test_explicit_member_id_is_the_roster_and_mention_key(self) -> None:
        server = self.make_server("Aki", "aki")
        self.servers.append(server)
        await server.start()
        member_id = "mem_0123456789abcdef0123456789abcdef"
        await server._on_channel_control(
            TextEnvelope(
                sender="Haru",
                text="profile",
                sender_id="device-haru",
                metadata={
                    "schema": "akid.v1",
                    "kind": "status",
                    "protocolVersion": "1",
                    "memberId": member_id,
                    "memberName": "Haru",
                    "sourceId": "device-haru",
                    "agentId": member_id,
                    "agentName": "Haru",
                    "aliases": "Haru,小春",
                    "memberKind": "agent",
                },
            )
        )
        self.assertIn(member_id, server._channel_members)
        self.assertEqual(server._channel_members[member_id]["name"], "Haru")
        self.assertEqual(server._channel_message_mentions("@小春 你好"), {member_id})

    async def test_conflicting_label_does_not_steal_mention_owner(self) -> None:
        server = self.make_server("Aki", "aki")
        self.servers.append(server)
        await server.start()
        owner_id = legacy_member_id("Fuyu")
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="owner",
                sender_id="device-owner",
                metadata={
                    "schema": "akid.v1",
                    "kind": "status",
                    "protocolVersion": "1",
                    "agentId": owner_id,
                    "agentName": "Fuyu",
                    "memberKind": "agent",
                },
            )
        )
        conflict_id = "mem_0123456789abcdef0123456789abcdef"
        await server._on_channel_control(
            TextEnvelope(
                sender="Fuyu",
                text="conflict",
                sender_id="device-conflict",
                metadata={
                    "schema": "akid.v1",
                    "kind": "status",
                    "protocolVersion": "1",
                    "memberId": conflict_id,
                    "memberName": "Fuyu",
                    "sourceId": "device-conflict",
                    "agentId": conflict_id,
                    "agentName": "Fuyu",
                    "aliases": "Fuyu",
                    "memberKind": "agent",
                },
            )
        )
        self.assertIn(conflict_id, server._channel_members)
        self.assertEqual(server._channel_message_mentions("@Fuyu 你好"), {owner_id})


class MemberProfileLimitTests(unittest.TestCase):
    def test_name_key_normalizes_whitespace_and_case(self) -> None:
        self.assertEqual(member_name_key("  Master "), "master")
        self.assertEqual(member_name_key("FUYU"), "fuyu")
        self.assertEqual(member_name_key(" 秋 "), "秋")

    def test_weighted_description_limit(self) -> None:
        self.assertEqual(member_description_weight("a" * 512), MAX_MEMBER_DESCRIPTION_UNITS)
        self.assertEqual(member_description_weight("中" * 256), MAX_MEMBER_DESCRIPTION_UNITS)
        self.assertEqual(truncate_member_description("a" * 600), "a" * 512)
        self.assertEqual(truncate_member_description("中" * 257), "中" * 256)
        self.assertEqual(truncate_member_description("abc中中", 5), "abc中")

    def test_missing_kind_means_agent(self) -> None:
        self.assertEqual(normalize_member_kind(None), "agent")
        self.assertEqual(normalize_member_kind(""), "agent")
        self.assertEqual(normalize_member_kind("agent"), "agent")
        self.assertEqual(normalize_member_kind("user"), "user")


if __name__ == "__main__":
    unittest.main()
