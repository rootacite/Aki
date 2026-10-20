from __future__ import annotations

import json
import unittest

from engine.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    FrameError,
    RpcError,
    decode_message,
    encode_message,
    make_error,
    make_event,
    make_response,
    parse_request,
)


class ProtocolTests(unittest.TestCase):
    def test_ndjson_round_trip_unicode(self) -> None:
        message = {"jsonrpc": "2.0", "id": 1, "result": {"text": "你好\naki"}}
        raw = encode_message(message)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(decode_message(raw), message)

    def test_oversized_frame_rejected(self) -> None:
        with self.assertRaises(FrameError):
            encode_message({"value": "x" * (MAX_LINE_BYTES + 10)})
        with self.assertRaises(FrameError):
            decode_message(b"{" + b"x" * (MAX_LINE_BYTES + 10) + b"}")

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(FrameError):
            decode_message(b"{not json}\n")

    def test_request_validation(self) -> None:
        request = parse_request(
            {"jsonrpc": "2.0", "id": "x", "method": "aki.health", "params": {}}
        )
        self.assertEqual(request.method, "aki.health")
        self.assertFalse(request.is_notification)
        with self.assertRaises(RpcError) as ctx:
            parse_request({"jsonrpc": "2.0", "id": "x", "params": {}})
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_REQUEST)

    def test_response_and_event_shapes(self) -> None:
        self.assertEqual(
            make_response(3, {"ok": True}),
            {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}},
        )
        self.assertEqual(
            make_error(4, ErrorCode.QUEUE_FULL, "full", {"queue_max": 32}),
            {
                "jsonrpc": "2.0",
                "id": 4,
                "error": {"code": ErrorCode.QUEUE_FULL, "message": "full", "data": {"queue_max": 32}},
            },
        )
        event = make_event("turn-1", 1, "assistant_delta", {"text": "a"}, "now")
        self.assertEqual(event["method"], "aki.event")
        self.assertEqual(event["params"]["seq"], 1)
        json.dumps(event)


if __name__ == "__main__":
    unittest.main()
