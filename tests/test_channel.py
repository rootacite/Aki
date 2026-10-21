from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.channel import TokifactorChannel
from engine.mqtt_client import MqttClient
from engine.tokifactor import (
    AVATAR_TOPIC,
    CONTROL_TOPIC,
    PICTURE_TOPIC,
    decrypt_bytes,
    decode_single_payload,
    encode_avatar_want,
    encode_text_payloads,
    parse_avatar_envelope,
    parse_text_chunk,
)
from engine.tokifactor import TextEnvelope
from tests.fake_mqtt_broker import FakeMqttBroker

MAGIC = "unit-test-channel"
TOPIC = "tokifactor/text"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached before timeout")


class ChannelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.broker = FakeMqttBroker()
        await self.broker.start()
        self.received: list[tuple[str, str, str, dict[str, str]]] = []
        self.replayed: list[bool] = []
        self.channel = TokifactorChannel(
            host="127.0.0.1",
            port=self.broker.port,
            magic=MAGIC,
            topic=TOPIC,
            sender_name="akid",
            sender_id="akid",
            on_text=self._on_text,
            on_control=self._on_control,
        )
        self.peer: MqttClient | None = None
        self.peer_messages: list[tuple[str, str, str]] = []
        self.peer_avatar_hashes: list[str] = []
        self.peer_metadata: list[dict[str, str]] = []
        self.picture_payloads: list[str] = []
        self.avatar_messages: list[str] = []
        self.control_messages: list[TextEnvelope] = []
        self.control_payloads: list[TextEnvelope] = []

    async def asyncTearDown(self) -> None:
        await self.channel.stop()
        if self.peer is not None:
            await self.peer.close()
        await self.broker.stop()

    async def _on_text(
        self,
        sender: str,
        sender_id: str,
        text: str,
        metadata: dict[str, str],
        message_id: str = "",
        sent_at: int = 0,
        replayed: bool = False,
    ) -> None:
        self.received.append((sender, sender_id, text, metadata))
        self.replayed.append(replayed)

    async def _on_control(self, envelope: TextEnvelope) -> None:
        self.control_messages.append(envelope)

    def _peer_message(self, topic: str, payload: bytes, replayed: bool = False) -> None:
        if topic == AVATAR_TOPIC:
            self.avatar_messages.append(payload.decode("utf-8"))
            return
        if topic == PICTURE_TOPIC:
            self.picture_payloads.append(payload.decode("utf-8"))
            return
        if topic == CONTROL_TOPIC:
            envelope = decode_single_payload(payload.decode("utf-8"), MAGIC)
            if envelope is not None:
                self.control_payloads.append(envelope)
            return
        envelope = decode_single_payload(payload.decode("utf-8"), MAGIC)
        if envelope is not None:
            self.peer_messages.append((envelope.sender, envelope.sender_id, envelope.text))
            self.peer_avatar_hashes.append(envelope.avatar_hash)
            self.peer_metadata.append(envelope.metadata)

    async def test_subscribe_publish_and_encrypted_round_trip(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)

        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(TOPIC, qos=1)

        payloads = encode_text_payloads(
            "hello from alice",
            MAGIC,
            sender="alice",
            sender_id="alice-1",
        )
        for payload in payloads:
            await self.peer.publish(TOPIC, payload.encode("utf-8"), qos=1)
        await wait_until(lambda: bool(self.received))
        self.assertEqual(
            self.received[0], ("alice", "alice-1", "hello from alice", {})
        )
        self.assertFalse(self.replayed[0])

        await self.channel.publish_text("reply from akid")
        await wait_until(
            lambda: any(item[1] == "akid" and item[2] == "reply from akid" for item in self.peer_messages)
        )
        health = self.channel.health()
        self.assertEqual(health["messages_received"], 1)
        self.assertEqual(health["replies_sent"], 1)
        self.assertTrue(health["connected"])

    async def test_persistent_session_delivers_offline_and_dedups_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            self.channel.dedup_path = Path(tempdir) / "dedup.json"
            await self.channel.start()
            await wait_until(lambda: self.channel.connected)
            first_connect = self.broker.connects[-1]
            self.assertEqual(first_connect["client_id"], self.channel.session_client_id)
            self.assertFalse(first_connect["clean_session"])

            payloads = encode_text_payloads(
                "offline hello",
                MAGIC,
                sender="alice",
                sender_id="alice-1",
                message_id="offline-message-1",
                sent_at=1_700_000_000_000,
            )
            await self.channel.stop()
            await asyncio.sleep(0.1)

            self.peer = MqttClient(
                "127.0.0.1",
                self.broker.port,
                client_id="peer-offline-test",
                on_message=self._peer_message,
            )
            await self.peer.connect()
            for payload in payloads:
                await self.peer.publish(TOPIC, payload.encode("utf-8"), qos=1)
            await wait_until(
                lambda: bool(
                    self.broker._offline.get(self.channel.session_client_id)
                )
            )

            await self.channel.start()
            await wait_until(lambda: bool(self.received))
            self.assertEqual(
                self.received[0],
                ("alice", "alice-1", "offline hello", {}),
            )
            self.assertTrue(self.replayed[0])
            self.assertEqual(
                self.broker.connects[-1]["client_id"],
                self.channel.session_client_id,
            )
            self.assertFalse(self.broker.connects[-1]["clean_session"])

            await self.channel.stop()
            await self.channel.start()
            await wait_until(lambda: self.channel.connected)
            for payload in payloads:
                await self.peer.publish(TOPIC, payload.encode("utf-8"), qos=1)
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.received), 1)
            self.assertGreaterEqual(self.channel.health()["duplicates_dropped"], 1)

    async def test_text_uses_qos1_and_control_uses_qos0(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        await self.channel.publish_text("chat message")
        await self.channel.publish_control(
            "probe", {"schema": "akid.v1", "kind": "probe", "probeId": "qos-test"}
        )
        await wait_until(
            lambda: len(self.broker.published_qos) >= 2
        )
        by_topic: dict[str, int] = {}
        for (topic, _payload), qos in zip(
            self.broker.published, self.broker.published_qos
        ):
            by_topic[topic] = qos
        self.assertEqual(by_topic[TOPIC], 1)
        self.assertEqual(by_topic[CONTROL_TOPIC], 0)

    async def test_structured_metadata_reaches_text_handler(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-metadata-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(TOPIC, qos=1)
        payloads = encode_text_payloads(
            "final agent text",
            MAGIC,
            sender="Fuyu",
            sender_id="fuyu",
            metadata={
                "schema": "akid.v1",
                "kind": "turn",
                "phase": "final",
                "turnId": "turn-1",
            },
        )
        for payload in payloads:
            await self.peer.publish(TOPIC, payload.encode("utf-8"), qos=1)
        await wait_until(lambda: bool(self.received))
        _sender, _sender_id, _text, metadata = self.received[0]
        self.assertEqual(metadata["kind"], "turn")
        self.assertEqual(metadata["phase"], "final")
        self.assertEqual(metadata["turnId"], "turn-1")

    async def test_own_echo_is_ignored(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        await self.channel.publish_text("self notice")
        await asyncio.sleep(0.3)
        self.assertEqual(self.received, [])

    async def test_publish_structured_round_trip(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-structured-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(TOPIC, qos=1)

        metadata = {
            "schema": "akid.v1",
            "kind": "turn",
            "turnId": "turn-1",
            "segment": "0",
            "seq": "3",
            "phase": "final",
            "format": "markdown",
        }
        await self.channel.publish_structured("**done**", metadata)
        await wait_until(lambda: bool(self.peer_metadata))
        self.assertEqual(self.peer_messages[-1][2], "**done**")
        self.assertEqual(self.peer_metadata[-1], metadata)

    async def test_control_probe_is_separate_from_chat(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-control-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(CONTROL_TOPIC, qos=1)

        metadata = {
            "schema": "akid.v1",
            "kind": "probe",
            "protocolVersion": "1",
            "probeId": "probe-1",
        }
        for payload in encode_text_payloads(
            "Aki probe",
            MAGIC,
            sender="tokifactor",
            sender_id="peer-control",
            metadata=metadata,
        ):
            await self.peer.publish(CONTROL_TOPIC, payload.encode("utf-8"), qos=1)
        await wait_until(lambda: bool(self.control_messages))
        self.assertEqual(self.control_messages[0].metadata, metadata)
        self.assertEqual(self.received, [])

        bad_magic = encode_text_payloads(
            "Aki probe",
            "wrong-magic",
            sender="tokifactor",
            sender_id="peer-control",
            metadata={**metadata, "probeId": "probe-bad-magic"},
        )
        for payload in bad_magic:
            await self.peer.publish(CONTROL_TOPIC, payload.encode("utf-8"), qos=1)
        own_echo = encode_text_payloads(
            "Aki probe",
            MAGIC,
            sender="akid",
            sender_id="akid",
            metadata={**metadata, "probeId": "probe-own-echo"},
        )
        for payload in own_echo:
            await self.peer.publish(CONTROL_TOPIC, payload.encode("utf-8"), qos=1)
        await asyncio.sleep(0.25)
        self.assertEqual(len(self.control_messages), 1)

        status = {
            "schema": "akid.v1",
            "kind": "status",
            "agent": "aki",
            "protocolVersion": "1",
            "queueDepth": "2",
        }
        await self.channel.publish_control("Aki status", status)
        await wait_until(
            lambda: any(
                envelope.metadata.get("kind") == "status"
                for envelope in self.control_payloads
            )
        )
        status_payload = next(
            envelope
            for envelope in self.control_payloads
            if envelope.metadata.get("kind") == "status"
        )
        self.assertEqual(status_payload.metadata, status)
        self.assertEqual(self.peer_messages, [])
        self.assertGreaterEqual(self.channel.health()["control"]["messages_sent"], 1)

    async def test_publish_image_round_trip(self) -> None:
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)
        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-image-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(PICTURE_TOPIC, qos=1)
        await self.peer.subscribe(TOPIC, qos=1)

        result = await self.channel.publish_image(
            PNG_1X1, caption="lab desk", mime="image/png"
        )
        await wait_until(lambda: bool(self.picture_payloads))
        chunk = parse_text_chunk(self.picture_payloads[0])
        self.assertIsNotNone(chunk)
        assert chunk is not None
        encrypted = base64.b64decode(chunk["encrypted_data"], validate=True)
        self.assertEqual(decrypt_bytes(encrypted, MAGIC), PNG_1X1)
        self.assertEqual(
            decrypt_bytes(base64.b64decode(chunk["caption"], validate=True), MAGIC),
            b"lab desk",
        )
        self.assertEqual(result["mime"], "image/png")
        self.assertEqual(result["bytes"], len(PNG_1X1))
        self.assertEqual(self.channel.health()["images_sent"], 1)
        await asyncio.sleep(0.2)
        self.assertEqual(self.peer_messages, [])

    async def test_avatar_have_want_and_message_hash(self) -> None:
        self.channel = TokifactorChannel(
            host="127.0.0.1",
            port=self.broker.port,
            magic=MAGIC,
            topic=TOPIC,
            sender_name="akid",
            sender_id="akid",
            avatar_bytes=PNG_1X1,
            avatar_mime="image/png",
            on_text=self._on_text,
        )
        await self.channel.start()
        await wait_until(lambda: self.channel.connected)

        self.peer = MqttClient(
            "127.0.0.1",
            self.broker.port,
            client_id="peer-avatar-test",
            on_message=self._peer_message,
        )
        await self.peer.connect()
        await self.peer.subscribe(AVATAR_TOPIC, qos=1)
        await self.peer.subscribe(TOPIC, qos=1)

        await self.peer.publish(
            AVATAR_TOPIC,
            encode_avatar_want("akid", "").encode("utf-8"),
            qos=1,
        )
        def have_messages():
            parsed = [parse_avatar_envelope(item) for item in self.avatar_messages]
            return [item for item in parsed if item is not None and item.type == "have"]

        await wait_until(lambda: bool(have_messages()))
        envelope = have_messages()[-1]
        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.type, "have")
        self.assertEqual(envelope.id, "akid")
        clear = decrypt_bytes(base64.b64decode(envelope.data), MAGIC)
        self.assertEqual(clear, PNG_1X1)
        self.assertEqual(envelope.hash, hashlib.sha256(PNG_1X1).hexdigest())

        await self.channel.publish_text("avatar-bearing reply")
        await wait_until(lambda: bool(self.peer_messages))
        self.assertEqual(self.peer_avatar_hashes[-1], envelope.hash)
        avatar_health = self.channel.health()["avatar"]
        self.assertTrue(avatar_health["published"])
        self.assertTrue(avatar_health["configured"])


if __name__ == "__main__":
    unittest.main()
