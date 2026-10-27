from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from engine.layer_process import (
    LayerProcess,
    default_layer_binary,
    discover_wayland_display,
)

ROOT = Path(__file__).resolve().parent.parent


class LayerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_and_stops_child_in_its_own_process_group(self) -> None:
        layer = LayerProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(ROOT),
            env=dict(os.environ),
            backoff=(0,),
        )
        layer.start()
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while not layer.running and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            self.assertTrue(layer.running)
            self.assertIsNotNone(layer.pid)
            self.assertEqual(layer.state, "running")
            status = layer.status()
            self.assertEqual(status["pid"], layer.pid)
            self.assertEqual(status["restart_count"], 0)
        finally:
            await layer.stop()

        self.assertFalse(layer.running)
        self.assertIsNone(layer.pid)
        self.assertEqual(layer.state, "stopped")

    async def test_reports_spawn_failure(self) -> None:
        layer = LayerProcess(
            [str(ROOT / "aki-layer" / "build" / "does-not-exist")],
            cwd=str(ROOT),
            env=dict(os.environ),
            backoff=(0.01,),
        )
        layer.start()
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while layer.last_error is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            self.assertIsNotNone(layer.last_error)
            self.assertIn("spawn failed", layer.last_error or "")
        finally:
            await layer.stop()

    async def test_clears_pid_while_waiting_to_restart(self) -> None:
        layer = LayerProcess(
            [sys.executable, "-c", "raise SystemExit(3)"],
            cwd=str(ROOT),
            env=dict(os.environ),
            backoff=(5,),
        )
        layer.start()
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while layer.restart_count == 0 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            self.assertEqual(layer.state, "restarting")
            self.assertIsNone(layer.pid)
            self.assertIn("code 3", layer.last_error or "")
        finally:
            await layer.stop()

    def test_default_binary_is_in_the_repository(self) -> None:
        self.assertEqual(
            default_layer_binary(),
            ROOT / "aki-layer" / "build" / "aki-layer",
        )

    def test_discover_wayland_display_prefers_configuration(self) -> None:
        self.assertEqual(
            discover_wayland_display(configured="wayland-9"),
            "wayland-9",
        )

    def test_discover_wayland_display_finds_newest_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            runtime = Path(tempdir)
            sockets = []
            for index, mtime in ((1, 10.0), (2, 20.0)):
                path = runtime / f"wayland-{index}"
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.bind(str(path))
                sockets.append(sock)
                os.utime(path, (mtime, mtime))
            (runtime / "wayland-notes").write_text("not a socket", encoding="utf-8")
            try:
                self.assertEqual(
                    discover_wayland_display(runtime),
                    "wayland-2",
                )
            finally:
                for sock in sockets:
                    sock.close()

    def test_discover_wayland_display_returns_none_without_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            self.assertIsNone(discover_wayland_display(tempdir))
