from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import akictl
from engine.aki_config import template_text
from engine.member_identity import IdentityRegistry, MemberIdentity, legacy_member_id
from engine.mqtt_client import MqttClient
from engine.tokifactor import decode_single_payload, encode_text_payloads
from engine.aki_config import read_mqtt
from tests.fake_mqtt_broker import FakeMqttBroker


class AkictlMqttTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.broker = FakeMqttBroker()
        await self.broker.start()
        self.config = SimpleNamespace(
            mqtt_host="127.0.0.1",
            mqtt_port=self.broker.port,
            mqtt_magic="cli-test-magic",
            mqtt_topic="tokifactor/text",
        )

    async def asyncTearDown(self) -> None:
        await self.broker.stop()
        self.tempdir.cleanup()

    async def test_chat_receives_progress_and_final_for_multiple_agents(self) -> None:
        observed: list[dict[str, str]] = []
        magic = self.config.mqtt_magic
        topic = self.config.mqtt_topic

        async def responder_on_message(_topic: str, payload: bytes, _replayed: bool) -> None:
            envelope = decode_single_payload(payload.decode("utf-8"), magic)
            if envelope is None:
                return
            metadata = dict(envelope.metadata)
            if metadata.get("kind") != "message":
                return
            client_id = metadata.get("clientMessageId") or ""
            observed.append(metadata)
            await asyncio.sleep(0.05)
            for index, (name, member_id) in enumerate(
                (("Aki", legacy_member_id("Aki")), ("Fuyu", legacy_member_id("Fuyu")))
            ):
                turn_id = f"turn-cli-{index}"
                for phase, text in (
                    ("started", "working"),
                    ("progress", f"{name} progress"),
                    ("final", f"{name} final"),
                    ("ended", ""),
                ):
                    event = {
                        "schema": "akid.v1",
                        "kind": "turn",
                        "turnId": turn_id,
                        "clientMessageId": client_id,
                        "phase": phase,
                        "seq": str(index),
                    }
                    for part in encode_text_payloads(
                        text,
                        magic,
                        sender=name,
                        sender_id=member_id,
                        metadata=event,
                    ):
                        await responder.publish(topic, part.encode("utf-8"), qos=1)

        responder = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="cli-test-responder",
            on_message=responder_on_message,
        )
        await responder.connect()
        await responder.subscribe(topic, qos=1)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                code = await akictl._mqtt_chat_once(
                    self.config,
                    "hello",
                    sender_id=legacy_member_id("Acite Lee"),
                    sender_name="Acite Lee",
                    client_message_id="cli-message-1",
                    json_mode=False,
                    timeout=5.0,
                )
        finally:
            await responder.close()
        self.assertEqual(code, 0)
        self.assertEqual(observed[0]["clientMessageId"], "cli-message-1")
        self.assertIn("Aki final", output.getvalue())
        self.assertIn("Fuyu final", output.getvalue())
        self.assertIn("[Aki] Aki progress", output.getvalue())

    async def test_cancel_payload_targets_client_message_id(self) -> None:
        messages: list[dict[str, str]] = []

        async def on_message(_topic: str, payload: bytes, _replayed: bool) -> None:
            envelope = decode_single_payload(payload.decode("utf-8"), self.config.mqtt_magic)
            if envelope is not None:
                messages.append(dict(envelope.metadata))

        subscriber = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="cli-cancel-subscriber",
            on_message=on_message,
        )
        publisher = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="cli-cancel-publisher",
        )
        await subscriber.connect()
        await subscriber.subscribe(self.config.mqtt_topic, qos=1)
        await publisher.connect()
        try:
            await akictl._publish_mqtt_cancel(
                publisher,
                self.config,
                sender_id=legacy_member_id("Acite Lee"),
                sender_name="Acite Lee",
                client_message_id="cancel-me",
            )
            await asyncio.sleep(0.1)
        finally:
            await publisher.close()
            await subscriber.close()
        self.assertTrue(messages)
        self.assertEqual(messages[-1]["kind"], "cancel")
        self.assertEqual(messages[-1]["clientMessageId"], "cancel-me")

    def test_unique_user_identity_requires_exactly_one_user(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaises(Exception):
                    akictl._unique_user_identity()
                registry = IdentityRegistry(home / ".aki" / "identities.json")
                user = MemberIdentity(
                    id=legacy_member_id("Acite Lee"),
                    name="Acite Lee",
                    kind="user",
                )
                registry.register(user, source="test")
                agent = MemberIdentity(
                    id=legacy_member_id("Aki"), name="Aki", kind="agent"
                )
                registry.register(agent, source="test")
                self.assertEqual(
                    akictl._unique_user_identity(),
                    (user.id, "Acite Lee"),
                )
                registry.register(
                    MemberIdentity(
                        id=legacy_member_id("Second Human"),
                        name="Second Human",
                        kind="user",
                    ),
                    source="test",
                )
                with self.assertRaises(Exception):
                    akictl._unique_user_identity()

    def test_history_reads_local_channel_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            config_path = home / ".aki" / "aki.conf"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(template_text("agent-test"), encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                config = akictl._profile_config("aki")
                key = __import__("engine.channel_history", fromlist=["channel_history_key"]).channel_history_key(
                    host=config.mqtt_host,
                    port=config.mqtt_port,
                    topic=config.mqtt_topic,
                    magic=config.mqtt_magic,
                )
                history = (
                    home
                    / ".aki"
                    / "channel-history"
                    / key
                    / "aki.solo.jsonl"
                )
                history.parent.mkdir(parents=True)
                history.write_text(
                    json.dumps(
                        {
                            "seq": 1,
                            "sender": "Aki",
                            "text": "solo secret",
                            "phase": "final",
                            "timestamp": "2026-10-27T10:00:00Z",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                args = SimpleNamespace(
                    limit=10, solo=True, json=False, config_path=None
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = akictl._history(args, "aki")
        self.assertEqual(code, 0)
        self.assertIn("solo secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
