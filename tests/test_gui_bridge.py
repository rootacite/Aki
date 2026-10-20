from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from engine.gui_bridge import GuiBridge, ensure_token


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


class GuiBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.socket_path = self.root / "akid.sock"
        self.endpoint_path = self.root / "gui-bridge.json"
        self.akid_lines: list[str] = []

        async def akid_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    text = line.decode("utf-8").rstrip("\n")
                    self.akid_lines.append(text)
                    request = json.loads(text)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {"echo": request.get("method")},
                    }
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.akid_server = await asyncio.start_unix_server(akid_handler, path=str(self.socket_path))
        self.bridge = GuiBridge(
            socket_path=self.socket_path,
            host="127.0.0.1",
            port=0,
            token="test-token",
            endpoint_path=self.endpoint_path,
        )
        self.task = asyncio.create_task(self.bridge.start())
        await wait_until(self.endpoint_path.exists)
        data = json.loads(self.endpoint_path.read_text("utf-8"))
        self.port = data["port"]

    async def asyncTearDown(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        await self.bridge.stop()
        self.akid_server.close()
        await self.akid_server.wait_closed()
        self.tempdir.cleanup()

    async def test_unauthorized_connection_is_rejected(self) -> None:
        with self.assertRaises(InvalidStatus) as context:
            async with connect(f"ws://127.0.0.1:{self.port}/?token=wrong"):
                pass
        self.assertEqual(context.exception.response.status_code, 401)

    async def test_authorized_connection_forwards_rpc(self) -> None:
        async with connect(f"ws://127.0.0.1:{self.port}/?token=test-token") as websocket:
            await websocket.send(
                json.dumps({"jsonrpc": "2.0", "id": "1", "method": "aki.status", "params": {}})
            )
            reply = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            self.assertEqual(reply["id"], "1")
            self.assertEqual(reply["result"]["echo"], "aki.status")
            self.assertEqual(json.loads(self.akid_lines[-1])["method"], "aki.status")

    def test_token_and_endpoint_are_private(self) -> None:
        token_path = self.root / "token"
        token = ensure_token(token_path)
        self.assertTrue(token)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.endpoint_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
