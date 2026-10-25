from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from engine.layer_notify import (
    LayerNotifier,
    character_for_names,
    character_for_profile,
    preview_text,
    split_paragraphs,
)


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class LayerNotifyHelpersTests(unittest.TestCase):
    def test_preview_collapses_whitespace_and_truncates(self) -> None:
        self.assertEqual(preview_text("  hello\n  world  "), "hello world")
        self.assertEqual(preview_text("abcdef", 4), "abc…")
        self.assertEqual(preview_text("   "), "")

    def test_character_aliases(self) -> None:
        self.assertEqual(character_for_names("fuyu"), "Fuyu")
        self.assertEqual(character_for_names("冬"), "Fuyu")
        self.assertEqual(character_for_names("unknown", "Haru", "春"), "Haru")
        self.assertIsNone(character_for_names("unknown"))
        self.assertEqual(character_for_profile("natsu"), "Natsu")
        self.assertEqual(character_for_profile("unknown", "Aki"), "Aki")

    def test_split_paragraphs_matches_tokifactor_rules(self) -> None:
        self.assertEqual(
            split_paragraphs("First paragraph.\n\nSecond paragraph."),
            ["First paragraph.", "Second paragraph."],
        )
        self.assertEqual(
            split_paragraphs("One\n\n\nTwo\n  \nThree"),
            ["One", "Two", "Three"],
        )
        self.assertEqual(split_paragraphs("One\r\n\r\nTwo"), ["One", "Two"])
        self.assertEqual(
            split_paragraphs(
                "Before\n\n```kotlin\nval one = 1\n\nval two = 2\n```\n\nAfter"
            ),
            ["Before", "```kotlin\nval one = 1\n\nval two = 2\n```", "After"],
        )
        single = "A single paragraph with  spacing."
        code = "```\nfirst\n\nsecond\n```"
        self.assertEqual(split_paragraphs(single), [single])
        self.assertEqual(split_paragraphs(code), [code])


class LayerNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_json_to_control_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = Path(tempdir) / "aki-layer.sock"
            received: asyncio.Queue[dict[str, str]] = asyncio.Queue()

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                line = await reader.readline()
                received.put_nowait(json.loads(line))
                writer.write(b"queued test\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=str(socket_path))
            notifier = LayerNotifier(socket_path, warn_interval=3600)
            notifier.start()
            try:
                self.assertTrue(notifier.notify("Fuyu", "  hello\n  world  "))
                payload = await asyncio.wait_for(received.get(), 2.0)
                self.assertEqual(payload, {"character": "Fuyu", "text": "hello world"})
                await wait_for(lambda: notifier.status()["sent"] == 1)
            finally:
                await notifier.stop()
                server.close()
                await server.wait_closed()

    async def test_reports_socket_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            notifier = LayerNotifier(
                Path(tempdir) / "missing.sock",
                send_timeout=0.1,
                warn_interval=3600,
            )
            notifier.start()
            try:
                self.assertTrue(notifier.notify("Aki", "hello"))
                await wait_for(lambda: notifier.status()["failed"] == 1)
                self.assertIsNotNone(notifier.status()["last_error"])
            finally:
                await notifier.stop()

    async def test_splits_message_into_one_bubble_per_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = Path(tempdir) / "aki-layer.sock"
            received: asyncio.Queue[dict[str, str]] = asyncio.Queue()

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                line = await reader.readline()
                received.put_nowait(json.loads(line))
                writer.write(b"queued test\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=str(socket_path))
            notifier = LayerNotifier(socket_path, warn_interval=3600)
            notifier.start()
            try:
                self.assertTrue(
                    notifier.notify(
                        "Natsu",
                        "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.",
                    )
                )
                first = await asyncio.wait_for(received.get(), 2.0)
                second = await asyncio.wait_for(received.get(), 2.0)
                third = await asyncio.wait_for(received.get(), 2.0)
                self.assertEqual(
                    [first["text"], second["text"], third["text"]],
                    ["First paragraph.", "Second paragraph.", "Third paragraph."],
                )
                self.assertEqual(
                    {first["character"], second["character"], third["character"]},
                    {"Natsu"},
                )
                await wait_for(lambda: notifier.status()["sent"] == 3)
            finally:
                await notifier.stop()
                server.close()
                await server.wait_closed()

    async def test_sends_presence_snapshot_to_control_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            socket_path = Path(tempdir) / "aki-layer.sock"
            received: asyncio.Queue[dict[str, object]] = asyncio.Queue()

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                line = await reader.readline()
                received.put_nowait(json.loads(line))
                writer.write(b"ok presence\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=str(socket_path))
            notifier = LayerNotifier(socket_path, warn_interval=3600)
            notifier.start()
            try:
                self.assertTrue(
                    notifier.notify_presence(
                        {
                            "active": True,
                            "activeSeconds": 42,
                            "away": False,
                            "valid": True,
                        }
                    )
                )
                payload = await asyncio.wait_for(received.get(), 2.0)
                self.assertEqual(payload["kind"], "presence")
                presence = payload["presence"]
                self.assertTrue(presence["active"])
                self.assertEqual(presence["activeSeconds"], 42)
                self.assertFalse(presence["away"])
                await wait_for(lambda: notifier.status()["sent"] == 1)
            finally:
                await notifier.stop()
                server.close()
                await server.wait_closed()
