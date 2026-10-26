"""Tests for the stream-json Aki bridge control methods."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from engine.letta_process import LettaProcess

ROOT = Path(__file__).resolve().parent.parent
FAKE_LETTA = ROOT / "tests" / "fake_letta.py"


class LettaBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aki-bridge-test-")
        self.control_path = Path(self.temp.name) / "control.jsonl"
        env = dict(os.environ)
        env["FAKE_LETTA_AKI_BRIDGE"] = "1"
        env["FAKE_LETTA_LAST_CONTROL"] = str(self.control_path)
        self.process = LettaProcess(
            [sys.executable, str(FAKE_LETTA)],
            cwd=str(ROOT),
            env=env,
            on_message=self._on_message,
            on_ready=self._on_ready,
            on_exit=self._on_exit,
        )
        self.messages: list[dict] = []
        self.process.start()
        self.assertTrue(await self.process.wait_ready(5.0))

    async def asyncTearDown(self) -> None:
        await self.process.stop()
        self.temp.cleanup()

    async def _on_message(self, message: dict) -> None:
        self.messages.append(message)

    async def _on_ready(self, payload: dict) -> None:
        return None

    async def _on_exit(self, returncode: int | None, detail: str | None) -> None:
        return None

    async def test_bridge_capabilities_and_control_methods(self) -> None:
        self.assertTrue(self.process.supports_aki_bridge)
        self.assertEqual(self.process.aki_bridge.get("version"), 1)

        self.assertEqual(
            (await self.process.begin_aki_turn("turn-1", {"turn_context": {}}))[
                "ok"
            ],
            True,
        )
        self.assertTrue((await self.process.update_aki_turn("turn-1", {}))["ok"])
        self.assertTrue((await self.process.end_aki_turn("turn-1"))["ok"])
        self.assertTrue(
            (await self.process.reload_conversation())["reloaded"]
        )
        snapshot = await self.process.snapshot_conversation()
        self.assertEqual(snapshot["snapshot_id"], "fake-snapshot")
        self.assertTrue(
            (await self.process.restore_conversation("fake-snapshot"))["restored"]
        )
        compact = await self.process.compact_conversation(mode="all")
        self.assertEqual(compact["num_messages_after"], 0)
        self.assertFalse((await self.process.reflect_conversation())["launched"])
        self.assertEqual(
            (await self.process.memory_tokens())["total_tokens"], 1
        )
        self.assertEqual((await self.process.mcp_list())["servers"], [])
        self.assertEqual(
            (await self.process.mcp_call("mcp__tavily__search", {"query": "x"}))[
                "result"
            ]["content"][0]["text"],
            "fake mcp",
        )

        lines = [
            json.loads(line)
            for line in self.control_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        subtypes = [line.get("request", {}).get("subtype") for line in lines]
        for expected in (
            "aki_begin_turn",
            "aki_update_turn",
            "aki_end_turn",
            "aki_reload_conversation",
            "aki_conversation_snapshot",
            "aki_conversation_restore",
            "aki_compact",
            "aki_reflect",
            "aki_memory_tokens",
            "aki_mcp_list",
            "aki_mcp_call",
        ):
            self.assertIn(expected, subtypes)


if __name__ == "__main__":
    unittest.main()
