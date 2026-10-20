from __future__ import annotations

import base64
import hashlib
import unittest

from engine.tokifactor import (
    AvatarEnvelope,
    TextChunkAssembler,
    TextEnvelope,
    decode_envelope,
    decode_single_payload,
    detect_image_mime,
    derive_key,
    encode_envelope,
    encode_avatar_have,
    encode_avatar_want,
    encode_text_payloads,
    encode_picture_abort,
    encode_picture_payloads,
    encrypt_bytes,
    decrypt_bytes,
    looks_like_chunk,
    parse_text_chunk,
    parse_avatar_envelope,
)

MAGIC = "akid-default-channel"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TokifactorWireTests(unittest.TestCase):
    def test_hkdf_key_matches_bouncycastle_vector(self) -> None:
        self.assertEqual(
            derive_key(MAGIC).hex(),
            "9ef95897998fd4dc1f1eeddaa13325a322cecd14bf64b114722c75df87a5a94a",
        )

    def test_decrypts_bouncycastle_vector(self) -> None:
        nonce = bytes.fromhex("000102030405060708090a0b")
        ciphertext = bytes.fromhex(
            "718150e2f8440324cbc1ca50407ce62eb3b0bc4082365b8bf6cf4819c33ae3b6"
        )
        self.assertEqual(decrypt_bytes(nonce + ciphertext, MAGIC), b"hello tokifactor")

    def test_envelope_round_trip(self) -> None:
        envelope = TextEnvelope(
            sender="Aki",
            text="你好\nline two",
            sender_id="akid-test",
            avatar_hash="abc",
        )
        encoded = encode_envelope(envelope)
        decoded = decode_envelope(encoded)
        self.assertEqual(decoded, envelope)

    def test_structured_envelope_round_trip(self) -> None:
        metadata = {
            "schema": "akid.v1",
            "kind": "turn",
            "turnId": "turn-1",
            "segment": "0",
            "seq": "7",
            "phase": "final",
            "format": "markdown",
        }
        envelope = TextEnvelope(
            sender="akid",
            text="# heading\n\n- item",
            sender_id="akid",
            metadata=metadata,
        )
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.text, envelope.text)
        self.assertEqual(decoded.metadata, metadata)
        self.assertIn('"schema":"akid.v1"', encode_envelope(envelope))

    def test_control_status_envelope_round_trip(self) -> None:
        metadata = {
            "schema": "akid.v1",
            "kind": "status",
            "agent": "aki",
            "agentName": "Aki",
            "model": "aki/deepseek-flash",
            "reasoning": "low",
            "capabilities": "approvals,history",
            "protocolVersion": "1",
            "contextLimit": "400000",
            "contextTokens": "1234",
            "backendReady": "true",
            "channelReady": "true",
            "sandboxReady": "true",
            "queueDepth": "2",
            "activeTurn": "true",
            "pendingApprovals": "1",
            "uptimeS": "60",
            "memoryTokens": "42",
        }
        envelope = TextEnvelope(
            sender="akid",
            text="Aki status",
            sender_id="akid",
            metadata=metadata,
        )
        decoded = decode_envelope(encode_envelope(envelope))
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.metadata, metadata)

    def test_avatar_envelope_round_trip(self) -> None:
        have = encode_avatar_have("akid", "abc123", "ZW5jcnlwdGVk")
        parsed = parse_avatar_envelope(have)
        self.assertEqual(
            parsed,
            AvatarEnvelope(type="have", id="akid", hash="abc123", data="ZW5jcnlwdGVk"),
        )
        want = encode_avatar_want("akid", "abc123")
        parsed_want = parse_avatar_envelope(want)
        self.assertIsNotNone(parsed_want)
        assert parsed_want is not None
        self.assertEqual(parsed_want.type, "want")
        self.assertEqual(parsed_want.id, "akid")

    def test_short_payload_round_trip(self) -> None:
        payload = encode_text_payloads(
            "hello channel",
            MAGIC,
            sender="alice",
            sender_id="alice-1",
        )[0]
        self.assertFalse(looks_like_chunk(payload))
        decoded = decode_single_payload(payload, MAGIC)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.text, "hello channel")
        self.assertEqual(decoded.sender_id, "alice-1")

    def test_wrong_magic_drops_payload(self) -> None:
        payload = encode_text_payloads(
            "secret", MAGIC, sender="alice", sender_id="alice-1"
        )[0]
        self.assertIsNone(decode_single_payload(payload, "wrong-magic"))

    def test_chunked_payload_round_trip(self) -> None:
        text = "x" * (10 * 1024) + "\n结尾"
        payloads = encode_text_payloads(
            text, MAGIC, sender="alice", sender_id="alice-1"
        )
        self.assertGreaterEqual(len(payloads), 2)
        assembler = TextChunkAssembler(MAGIC)
        result = None
        for payload in payloads:
            chunk = parse_text_chunk(payload)
            self.assertIsNotNone(chunk)
            assert chunk is not None
            result = assembler.add(chunk)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, text)
        self.assertEqual(result.sender_id, "alice-1")

    def test_structured_chunk_metadata_round_trip(self) -> None:
        text = "# response\n\n" + "z" * (10 * 1024)
        metadata = {
            "schema": "akid.v1",
            "kind": "turn",
            "turnId": "turn-long",
            "segment": "2",
            "seq": "99",
            "phase": "final",
            "format": "markdown",
        }
        payloads = encode_text_payloads(
            text,
            MAGIC,
            sender="akid",
            sender_id="akid",
            metadata=metadata,
        )
        assembler = TextChunkAssembler(MAGIC)
        result = None
        for payload in payloads:
            chunk = parse_text_chunk(payload)
            self.assertIsNotNone(chunk)
            assert chunk is not None
            self.assertEqual(chunk["metadata"], metadata)
            result = assembler.add(chunk)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, text)
        self.assertEqual(result.metadata, metadata)

    def test_chunk_hash_mismatch_is_rejected(self) -> None:
        text = "y" * (10 * 1024)
        payloads = encode_text_payloads(
            text, MAGIC, sender="alice", sender_id="alice-1"
        )
        chunk = parse_text_chunk(payloads[0])
        assert chunk is not None
        chunk["hash"] = hashlib.sha256(b"different").hexdigest()
        assembler = TextChunkAssembler(MAGIC)
        result = None
        for index, payload in enumerate(payloads):
            parsed = parse_text_chunk(payload)
            assert parsed is not None
            if index == 0:
                parsed = chunk
            result = assembler.add(parsed)
        self.assertIsNone(result)

    def test_encrypt_round_trip(self) -> None:
        packet = encrypt_bytes(b"binary", MAGIC)
        self.assertEqual(decrypt_bytes(packet, MAGIC), b"binary")
        self.assertTrue(base64.b64encode(packet))

    def test_detect_image_mime(self) -> None:
        self.assertEqual(detect_image_mime(PNG_1X1), "image/png")
        self.assertEqual(detect_image_mime(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(detect_image_mime(b"GIF89a..."), "image/gif")
        self.assertEqual(detect_image_mime(b"RIFF1234WEBP..."), "image/webp")
        self.assertIsNone(detect_image_mime(b"not an image"))

    def test_picture_payload_round_trip(self) -> None:
        data = PNG_1X1 + b"x" * (9 * 1024)
        transfer_id, payloads = encode_picture_payloads(
            data, MAGIC, sender="akid", sender_id="akid", caption="lab desk"
        )
        self.assertTrue(transfer_id)
        self.assertGreaterEqual(len(payloads), 2)
        pieces: list[bytes] = []
        digests = set()
        for payload in payloads:
            chunk = parse_text_chunk(payload)
            self.assertIsNotNone(chunk)
            assert chunk is not None
            self.assertEqual(chunk["sender"], "akid")
            self.assertEqual(chunk["total"], len(payloads))
            self.assertEqual(chunk["size"], len(data))
            self.assertEqual(
                decrypt_bytes(base64.b64decode(chunk["caption"], validate=True), MAGIC),
                b"lab desk",
            )
            digests.add(chunk["hash"])
            encrypted = base64.b64decode(chunk["encrypted_data"], validate=True)
            pieces.append(decrypt_bytes(encrypted, MAGIC))
        self.assertEqual(len(digests), 1)
        self.assertEqual(hashlib.sha256(data).hexdigest(), digests.pop())
        self.assertEqual(b"".join(pieces), data)

    def test_picture_abort_shape(self) -> None:
        payload = encode_picture_abort(
            "transfer-1", sender="akid", sender_id="akid", reason="fail"
        )
        self.assertIn('"status":"abort"', payload)
        self.assertIn('"id":"transfer-1:0:1"', payload)


if __name__ == "__main__":
    unittest.main()
