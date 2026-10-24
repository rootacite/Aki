from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engine.image_generation import (
    ImageGenerationError,
    generate_minimax_images,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


class FakeMiniMaxImageAPI:
    def __init__(self, payload: dict[str, Any] | None = None, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length)
                fake.requests.append(json.loads(body or b"{}"))
                fake.headers.append(dict(self.headers.items()))
                payload = fake.payload
                if payload is None:
                    payload = {
                        "id": "image-task-1",
                        "data": {
                            "image_base64": [base64.b64encode(PNG_1X1).decode("ascii")]
                        },
                        "metadata": {"success_count": "1", "failed_count": "0"},
                        "base_resp": {"status_code": 0, "status_msg": "success"},
                    }
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


class ImageGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.output = self.root / "images"
        self.api = FakeMiniMaxImageAPI()
        self.api.start()

    def tearDown(self) -> None:
        self.api.stop()
        self.tempdir.cleanup()

    def generate(self, **overrides):
        arguments = {
            "base_url": f"http://127.0.0.1:{self.api.port}/v1",
            "endpoint": "/image_generation",
            "api_key": "test-minimax-key",
            "model": "image-01",
            "prompt": "spring garden with a tiny paint brush",
            "output_dir": self.output,
            "aspect_ratio": "16:9",
            "n": 1,
            "timeout": 5.0,
            "prompt_optimizer": True,
            "aigc_watermark": False,
            "use_proxy": False,
        }
        arguments.update(overrides)
        return generate_minimax_images(**arguments)

    def test_generates_base64_image_and_private_file(self) -> None:
        result = self.generate()
        self.assertEqual(result["count"], 1)
        path = Path(result["images"][0]["path"])
        self.assertEqual(path.read_bytes(), PNG_1X1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result["images"][0]["mime"], "image/png")

        request = self.api.requests[0]
        self.assertEqual(request["model"], "image-01")
        self.assertEqual(request["aspect_ratio"], "16:9")
        self.assertEqual(request["response_format"], "base64")
        self.assertEqual(request["n"], 1)
        self.assertTrue(request["prompt_optimizer"])
        self.assertFalse(request["aigc_watermark"])
        headers = {key.lower(): value for key, value in self.api.headers[0].items()}
        self.assertEqual(headers["authorization"], "Bearer test-minimax-key")

    def test_reports_application_error(self) -> None:
        self.api.payload = {
            "base_resp": {"status_code": 1004, "status_msg": "invalid api key"}
        }
        with self.assertRaisesRegex(ImageGenerationError, "invalid api key"):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_rejects_invalid_prompt_without_request(self) -> None:
        with self.assertRaisesRegex(ImageGenerationError, "1500"):
            self.generate(prompt="x" * 1501)
        self.assertEqual(self.api.requests, [])

    def test_rejects_unknown_image_format(self) -> None:
        self.api.payload = {
            "data": {"image_base64": [base64.b64encode(b"not an image").decode()]},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        }
        with self.assertRaisesRegex(ImageGenerationError, "unsupported format"):
            self.generate()
        self.assertEqual(list(self.output.glob("image-*")), [])


if __name__ == "__main__":
    unittest.main()
