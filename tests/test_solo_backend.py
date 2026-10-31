from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from engine.aki_config import SoloBackend
from engine.solo_backend import (
    SoloBackendError,
    SoloBackendManager,
    discover_solo_route,
)


MODEL_PATH = "/opt/models/language/Qwen-Test-35B-A3B-IQ4_XS.gguf"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _MetadataHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": MODEL_PATH,
                        "object": "model",
                        "architecture": {"input_modalities": ["text", "image"]},
                        "meta": {"n_ctx": 150000, "n_ctx_train": 262144},
                    }
                ],
            }
        elif self.path == "/props":
            payload = {
                "default_generation_settings": {
                    "n_ctx": 150000,
                    "params": {"n_predict": 32768},
                },
                "modalities": {"vision": True},
                "model_path": MODEL_PATH,
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class DiscoverSoloRouteTests(unittest.TestCase):
    def test_reads_live_model_context_and_output_cap(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _MetadataHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            route = discover_solo_route(f"http://127.0.0.1:{port}/v1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(
            route.model_id, "Qwen-Test-35B-A3B-IQ4_XS"
        )
        self.assertEqual(route.upstream_model, MODEL_PATH)
        self.assertEqual(route.context_window, 150000)
        self.assertEqual(route.max_tokens, 32768)
        self.assertEqual(route.inputs, ("text", "image"))


class SoloBackendManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.server_script = self.root / "fake_llama.py"
        self.server_script.write_text(
            textwrap.dedent(
                """
                import json
                import sys
                from http.server import BaseHTTPRequestHandler, HTTPServer

                port = int(sys.argv[1])
                n_ctx = int(sys.argv[2])
                model = __MODEL_PATH__

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        if self.path == "/v1/models":
                            payload = {"data": [{"id": model, "meta": {"n_ctx": n_ctx},
                                "architecture": {"input_modalities": ["text", "image"]}}]}
                        elif self.path == "/props":
                            payload = {"default_generation_settings": {"n_ctx": n_ctx,
                                "params": {"n_predict": 4096}}, "model_path": model,
                                "modalities": {"vision": True}}
                        else:
                            self.send_response(404)
                            self.end_headers()
                            return
                        body = json.dumps(payload).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, format, *args):
                        return

                HTTPServer(("127.0.0.1", port), Handler).serve_forever()
                """
            ).replace("__MODEL_PATH__", repr(MODEL_PATH)),
            encoding="utf-8",
        )
        self.managers: list[SoloBackendManager] = []

    async def asyncTearDown(self) -> None:
        for manager in self.managers:
            await manager.close()
        self.tempdir.cleanup()

    def _manager(
        self,
        port: int,
        *,
        idle_timeout: float = 900,
        startup_timeout: float = 5,
    ) -> SoloBackendManager:
        script = (
            f"{sys.executable} {self.server_script} {port} 120000"
        )
        manager = SoloBackendManager(
            SoloBackend(
                startup_script=script,
                endpoint=f"http://127.0.0.1:{port}/v1",
                startup_timeout=startup_timeout,
                shutdown_timeout=2,
                idle_timeout=idle_timeout,
            ),
            profile="test",
            state_path=self.root / "test.backend.json",
            activity_path=self.root / "test.activity",
            log_path=self.root / "test.backend.log",
        )
        self.managers.append(manager)
        return manager

    async def test_starts_discovers_and_stops(self) -> None:
        port = free_port()
        manager = self._manager(port)
        route = await manager.start()
        self.assertEqual(route.model_id, "Qwen-Test-35B-A3B-IQ4_XS")
        self.assertEqual(route.context_window, 120000)
        self.assertEqual(route.max_tokens, 4096)
        self.assertTrue(manager.status()["running"])
        state = json.loads((self.root / "test.backend.json").read_text())
        self.assertTrue(state["running"])
        self.assertEqual(state["route"]["model_id"], route.model_id)
        await manager.stop()
        self.assertFalse(manager.status()["running"])
        self.assertFalse((self.root / "test.activity").exists())

    async def test_reserves_existing_group_model_id(self) -> None:
        manager = self._manager(free_port())
        route = await manager.start(
            reserved_model_ids={"Qwen-Test-35B-A3B-IQ4_XS"}
        )
        self.assertEqual(route.model_id, "Qwen-Test-35B-A3B-IQ4_XS-solo")

    async def test_idle_monitor_stops_process(self) -> None:
        manager = self._manager(free_port(), idle_timeout=0.2)
        await manager.start()
        await asyncio.sleep(0.8)
        self.assertFalse(manager.status()["running"])

    async def test_rejects_unmanaged_endpoint(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _MetadataHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            manager = self._manager(server.server_address[1])
            with self.assertRaisesRegex(SoloBackendError, "already in use"):
                await manager.start()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    async def test_startup_failure_surfaces_log_path(self) -> None:
        port = free_port()
        manager = SoloBackendManager(
            SoloBackend(
                startup_script=f"{sys.executable} -c 'import sys; sys.exit(2)'",
                endpoint=f"http://127.0.0.1:{port}/v1",
                startup_timeout=1,
                shutdown_timeout=1,
                idle_timeout=900,
            ),
            profile="test",
            state_path=self.root / "test.backend.json",
            activity_path=self.root / "test.activity",
            log_path=self.root / "test.backend.log",
        )
        self.managers.append(manager)
        with self.assertRaisesRegex(SoloBackendError, "exited with code 2"):
            await manager.start()
        self.assertFalse(manager.status()["running"])
