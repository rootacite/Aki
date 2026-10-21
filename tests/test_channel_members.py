from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from engine.akid import GatewayConfig, GatewayServer
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
            mqtt_sender_id=channel_id,
            channel_id=channel_id,
            channel_name=name,
            channel_aliases=(name,),
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
        await wait_until(lambda: "fuyu" in aki._channel_members)
        await wait_until(lambda: "aki" in fuyu._channel_members)
        self.assertEqual(aki._channel_members["fuyu"]["name"], "Fuyu")
        self.assertEqual(aki.active, None)
        self.assertEqual(aki.queue, [])
        self.assertEqual(fuyu.active, None)
        self.assertEqual(fuyu.queue, [])

        await fuyu.stop()
        self.servers.remove(fuyu)
        await wait_until(lambda: "fuyu" not in aki._channel_members)


if __name__ == "__main__":
    unittest.main()
