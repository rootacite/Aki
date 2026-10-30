from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.aki_config import ComfyUI
from engine.comfyui import (
    ComfyUIError,
    ComfyUIManager,
    NOT_CONFIGURED_MESSAGE,
    WORKFLOW_PATH,
    _group_alive,
    _pid_start_ticks,
    render_workflow,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeComfyHandler(BaseHTTPRequestHandler):
    server_version = "FakeComfy/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/system_stats":
            self._json({"system": {"comfyui_version": "test"}})
            return
        if self.path.startswith("/history/"):
            prompt_id = self.path.rsplit("/", 1)[-1]
            self._json(
                {
                    prompt_id: {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "222": {
                                "images": [
                                    {
                                        "filename": "Haru_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            )
            return
        if self.path.startswith("/view?"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_1X1)))
            self.end_headers()
            self.wfile.write(PNG_1X1)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/prompt":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(payload)  # type: ignore[attr-defined]
        self._json({"prompt_id": "prompt-test", "number": 1, "node_errors": {}})


class FakeComfyServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict]:
        return self.httpd.requests  # type: ignore[attr-defined]

    def __enter__(self) -> "FakeComfyServer":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class WorkflowTests(unittest.TestCase):
    def test_parameters_only_touch_the_first_pass_and_keep_defaults(self) -> None:
        original = WORKFLOW_PATH.read_text(encoding="utf-8")
        workflow, seed, applied = render_workflow(
            WORKFLOW_PATH,
            {
                "positive": "spring garden",
                "negative": "blurry",
                "seed": 42,
                "cfg": 6.5,
                "steps": 24,
                "width": 1024,
                "height": 768,
            },
        )
        self.assertEqual(workflow["215"]["inputs"]["text"], "spring garden")
        self.assertEqual(workflow["216"]["inputs"]["text"], "blurry")
        self.assertEqual(workflow["224"]["inputs"]["seed"], 42)
        self.assertEqual(workflow["19"]["inputs"]["cfg"], 6.5)
        self.assertEqual(workflow["19"]["inputs"]["steps"], 24)
        self.assertEqual(workflow["217"]["inputs"]["width"], 1024)
        self.assertEqual(workflow["217"]["inputs"]["height"], 768)
        self.assertEqual(workflow["198:196"]["inputs"]["cfg"], 5.2)
        self.assertEqual(workflow["198:196"]["inputs"]["steps"], 18)
        self.assertEqual(workflow["198:259"]["inputs"]["text"].split(",")[0], "best quality")
        self.assertEqual(seed, 42)
        self.assertEqual(applied["seed"], 42)
        self.assertEqual(WORKFLOW_PATH.read_text(encoding="utf-8"), original)

    def test_omitted_seed_is_random_and_other_defaults_are_unchanged(self) -> None:
        workflow, seed, _applied = render_workflow(
            WORKFLOW_PATH, {"positive": "only prompt"}
        )
        self.assertGreaterEqual(seed, 0)
        self.assertEqual(workflow["224"]["inputs"]["seed"], seed)
        self.assertEqual(workflow["19"]["inputs"]["cfg"], 5.2)
        self.assertEqual(workflow["19"]["inputs"]["steps"], 50)
        self.assertEqual(workflow["217"]["inputs"]["width"], 1152)
        self.assertEqual(workflow["217"]["inputs"]["height"], 768)
        self.assertEqual(workflow["216"]["inputs"]["text"].split(",")[0], "worst quality")

    def test_positive_and_dimensions_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            render_workflow(WORKFLOW_PATH, {"positive": "  "})
        with self.assertRaises(ValueError):
            render_workflow(WORKFLOW_PATH, {"positive": "x", "width": 1001})
        with self.assertRaises(ValueError):
            render_workflow(WORKFLOW_PATH, {"positive": "x", "steps": 0})


class ComfyUIConfigTests(unittest.TestCase):
    def test_unconfigured_startup_returns_exact_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = ComfyUIManager(
                ComfyUI(),
                profile="haru",
                state_path=root / "state.json",
                workflow_path=WORKFLOW_PATH,
            )
            with self.assertRaisesRegex(ComfyUIError, f"^{NOT_CONFIGURED_MESSAGE}$"):
                asyncio.run(manager.start())


class ComfyUIHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_posts_workflow_and_saves_final_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FakeComfyServer() as fake:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                    start_new_session=True,
                )
                manager = ComfyUIManager(
                    ComfyUI(
                        startup_script="/bin/true",
                        endpoint=fake.endpoint,
                        output_dir=str(root / "images"),
                        require_approval=False,
                    ),
                    profile="haru",
                    state_path=root / "state.json",
                    workflow_path=WORKFLOW_PATH,
                )
                manager._pgid = proc.pid
                manager._leader_pid = proc.pid
                manager._leader_start_ticks = _pid_start_ticks(proc.pid)
                manager._state_endpoint = fake.endpoint
                try:
                    result = await manager.generate(
                        {
                            "positive": "pixel art garden",
                            "negative": "",
                            "seed": 7,
                            "width": 512,
                            "height": 512,
                        }
                    )
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()

            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(Path(result["path"]).read_bytes(), PNG_1X1)
            self.assertEqual(result["mime"], "image/png")
            self.assertEqual(result["seed"], 7)
            self.assertEqual(result["prompt_id"], "prompt-test")
            posted = fake.requests[0]["prompt"]
            self.assertEqual(posted["215"]["inputs"]["text"], "pixel art garden")
            self.assertEqual(posted["216"]["inputs"]["text"], "")
            self.assertEqual(posted["217"]["inputs"]["width"], 512)

    async def test_generate_rejects_untracked_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FakeComfyServer() as fake:
                manager = ComfyUIManager(
                    ComfyUI(
                        startup_script="/bin/true",
                        endpoint=fake.endpoint,
                        output_dir=str(root / "images"),
                    ),
                    profile="haru",
                    state_path=root / "state.json",
                    workflow_path=WORKFLOW_PATH,
                )
                with self.assertRaisesRegex(ComfyUIError, "comfyui_startup"):
                    await manager.generate({"positive": "x"})

    async def test_startup_refuses_an_external_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FakeComfyServer() as fake:
                manager = ComfyUIManager(
                    ComfyUI(
                        startup_script="/bin/true",
                        endpoint=fake.endpoint,
                        output_dir=str(root / "images"),
                    ),
                    profile="haru",
                    state_path=root / "state.json",
                    workflow_path=WORKFLOW_PATH,
                )
                with self.assertRaisesRegex(ComfyUIError, "not started by comfyui_startup"):
                    await manager.start()


class ComfyUIProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def _server_script(self) -> Path:
        path = self.root / "fake_comfy_server.py"
        path.write_text(
            """
import http.server
import signal
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"system": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        return

def on_sigint(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGINT, on_sigint)
server = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
""",
            encoding="utf-8",
        )
        return path

    async def test_start_restore_and_sigint_stop(self) -> None:
        port = free_port()
        script = self._server_script()
        config = ComfyUI(
            startup_script=f"{sys.executable} {script} {port}",
            endpoint=f"http://127.0.0.1:{port}",
            output_dir=str(self.root / "images"),
            startup_timeout=5,
            shutdown_timeout=5,
        )
        state_path = self.root / "state.json"
        manager = ComfyUIManager(
            config, profile="haru", state_path=state_path, workflow_path=WORKFLOW_PATH
        )
        result = await manager.start()
        self.assertEqual(result["status"], "started")
        self.assertTrue(state_path.is_file())
        self.assertTrue(_group_alive(manager._pgid))

        restored = ComfyUIManager(
            config, profile="haru", state_path=state_path, workflow_path=WORKFLOW_PATH
        )
        self.assertTrue(restored.status()["running"])
        self.assertEqual(restored._pgid, manager._pgid)

        stopped = await manager.stop()
        self.assertEqual(stopped["signal"], "SIGINT")
        self.assertFalse(state_path.exists())
        self.assertFalse(_group_alive(result["pgid"]))

    async def test_startup_timeout_kills_the_process_group(self) -> None:
        script = self.root / "sleep.py"
        script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        config = ComfyUI(
            startup_script=f"{sys.executable} {script}",
            endpoint=f"http://127.0.0.1:{free_port()}",
            output_dir=str(self.root / "images"),
            startup_timeout=0.6,
            shutdown_timeout=1,
        )
        manager = ComfyUIManager(
            config,
            profile="haru",
            state_path=self.root / "state.json",
            workflow_path=WORKFLOW_PATH,
        )
        with self.assertRaisesRegex(ComfyUIError, "startup timed out"):
            await manager.start()
        self.assertIsNone(manager._pgid)
        self.assertFalse((self.root / "state.json").exists())

    async def test_idle_timeout_stops_the_process_group(self) -> None:
        port = free_port()
        script = self._server_script()
        config = ComfyUI(
            startup_script=f"{sys.executable} {script} {port}",
            endpoint=f"http://127.0.0.1:{port}",
            output_dir=str(self.root / "images"),
            startup_timeout=5,
            shutdown_timeout=5,
            idle_timeout=0.4,
        )
        state_path = self.root / "state.json"
        manager = ComfyUIManager(
            config, profile="haru", state_path=state_path, workflow_path=WORKFLOW_PATH
        )
        result = await manager.start()
        pgid = result["pgid"]
        self.assertTrue(_group_alive(pgid))

        deadline = asyncio.get_running_loop().time() + 5
        while (
            (_group_alive(pgid) or state_path.exists())
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)

        self.assertFalse(_group_alive(pgid))
        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
