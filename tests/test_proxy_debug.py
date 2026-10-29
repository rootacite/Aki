from __future__ import annotations

import asyncio
import gzip
import json
import socket
import tempfile
import unittest
from pathlib import Path

from engine.proxy_debug import ProxyDebugStream, SCHEMA


async def _read_event(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    if not line:
        raise AssertionError("debug socket closed")
    return json.loads(line)


class ProxyDebugStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tempdir.name) / "debug.sock"
        self.stream = ProxyDebugStream(self.socket_path, "aki")
        await self.stream.start()

    async def asyncTearDown(self) -> None:
        await self.stream.stop()
        self.tempdir.cleanup()

    async def test_hello_replays_ring_then_streams_live(self) -> None:
        self.stream.publish("request", request_id="req-1", tool_names=["Grep", "LS"])
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            hello = await _read_event(reader)
            self.assertEqual(hello["schema"], SCHEMA)
            self.assertEqual(hello["event"], "hello")
            replayed = await _read_event(reader)
            self.assertEqual(replayed["event"], "request")
            self.assertEqual(replayed["request_id"], "req-1")
            self.assertGreaterEqual(replayed["seq"], 1)

            self.stream.publish("response_chunk", request_id="req-1", data="ok", encoding="utf-8")
            live = await _read_event(reader)
            self.assertEqual(live["event"], "response_chunk")
            self.assertEqual(live["data"], "ok")
            self.assertGreater(live["seq"], replayed["seq"])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_redacts_headers_and_base64_payload(self) -> None:
        self.stream.publish(
            "request",
            request_id="req-2",
            headers={"authorization": "Bearer secret", "content-type": "application/json"},
            payload={"data": "x" * 2000, "messages": []},
        )
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            await _read_event(reader)
            event = await _read_event(reader)
            self.assertEqual(event["headers"]["authorization"], "[redacted]")
            self.assertEqual(event["headers"]["content-type"], "application/json")
            self.assertEqual(event["payload"]["data"], "[base64 omitted]")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_decodes_gzip_response_chunks(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            await _read_event(reader)
            response = self.stream.begin_response(
                "req-gzip",
                status=200,
                headers={"content-encoding": "gzip", "set-cookie": "secret"},
            )
            response.chunk(gzip.compress(b"data: hello\\n\\n"))
            response.end()
            events = []
            while True:
                event = await _read_event(reader)
                events.append(event)
                if event["event"] == "response_end":
                    break
            self.assertEqual(events[0]["headers"]["set-cookie"], "[redacted]")
            chunks = "".join(
                event["data"] for event in events if event["event"] == "response_chunk"
            )
            self.assertEqual(chunks, "data: hello\\n\\n")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_socket_permissions_and_stale_socket_cleanup(self) -> None:
        self.assertEqual(self.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.socket_path.parent.stat().st_mode & 0o777, 0o700)
        await self.stream.stop()

        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.close()
        self.assertTrue(self.socket_path.exists())

        self.stream = ProxyDebugStream(self.socket_path, "aki")
        await self.stream.start()
        self.assertTrue(self.socket_path.exists())

    async def test_refuses_non_socket_path(self) -> None:
        await self.stream.stop()
        self.socket_path.write_text("not a socket", encoding="utf-8")
        self.stream = ProxyDebugStream(self.socket_path, "aki")
        with self.assertRaises(RuntimeError):
            await self.stream.start()


if __name__ == "__main__":
    unittest.main()
