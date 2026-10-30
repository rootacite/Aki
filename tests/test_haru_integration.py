from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engine.client import GatewayClient
from engine.mqtt_client import MqttClient
from engine.tokifactor import (
    CONTROL_TOPIC,
    PICTURE_TOPIC,
    TextChunkAssembler,
    decode_single_payload,
    encode_text_payloads,
    encode_picture_payloads,
    looks_like_chunk,
    parse_text_chunk,
)
from tests.fake_mqtt_broker import FakeMqttBroker

ROOT = Path(__file__).resolve().parent.parent
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _FakeGemini:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
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

            def log_message(self, *_args) -> None:  # noqa: D401
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(payload)
                chunks = fake._chunks(payload)
                body = b"".join(
                    b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _chunks(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        messages = payload.get("messages") or []
        last_user = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            default=-1,
        )
        text = messages[last_user].get("content") if last_user >= 0 else ""
        if isinstance(text, list):
            text = " ".join(
                part.get("text", "")
                for part in text
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(text or "")
        tail = messages[last_user + 1 :]
        tool_calls = sum(
            len(message.get("tool_calls") or [])
            for message in tail
            if isinstance(message, dict) and message.get("role") == "assistant"
        )
        model = payload.get("model") or "gemini-3.8-flash"
        chunks: list[dict[str, Any]] = []

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            chunks.append(
                {
                    "id": "chatcmpl-haru-test",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )

        target = ""
        for word in text.split():
            if word.startswith("TARGET="):
                target = word.split("=", 1)[1]
        if "WRITE_TMP" in text:
            if tool_calls == 0:
                chunk(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-lock-tmp",
                                "type": "function",
                                "function": {"name": "acquire_execution_lock", "arguments": "{}"},
                            }
                        ],
                    }
                )
                chunk({}, "tool_calls")
            elif tool_calls == 1:
                chunk(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-write-tmp",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {"file_path": target, "content": "hello from haru"}
                                    ),
                                },
                            }
                        ],
                    }
                )
                chunk({}, "tool_calls")
            else:
                chunk({"role": "assistant", "content": "写好了，哥哥。"})
                chunk({}, "stop")
        elif "WRITE_REPO" in text:
            if tool_calls == 0:
                chunk(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-lock-repo",
                                "type": "function",
                                "function": {"name": "acquire_execution_lock", "arguments": "{}"},
                            }
                        ],
                    }
                )
                chunk({}, "tool_calls")
            elif tool_calls == 1:
                chunk(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-write-repo",
                                "type": "function",
                                "function": {
                                    "name": "Write",
                                    "arguments": json.dumps(
                                        {"file_path": target, "content": "must not be written"}
                                    ),
                                },
                            }
                        ],
                    }
                )
                chunk({}, "tool_calls")
            else:
                chunk({"role": "assistant", "content": "仓库不能写，哥哥。"})
                chunk({}, "stop")
        else:
            chunk({"role": "assistant", "content": "你好，哥哥！我是 Haru。"})
            chunk({}, "stop")
        return chunks


@unittest.skipUnless(shutil.which("letta"), "Letta is not installed")
@unittest.skipUnless(shutil.which("node"), "node is required for Letta mods")
class HaruLettaIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        cache = Path.home() / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(prefix="haru-int-", dir=cache)
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.source = self.workdir / "userdata3"
        shutil.copytree(ROOT / "userdata3", self.source)
        (self.source / "identity.json").unlink(missing_ok=True)
        self.backend = self.home / ".letta" / "lc-local-backend"
        self.config = self.home / ".aki" / "haru.conf"
        self.runtime = self.root / "run"
        self.runtime.mkdir(mode=0o700)
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "LETTA_HOME": str(self.home / ".letta"),
                "LETTA_LOCAL_BACKEND_DIR": str(self.backend),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "AKI_CWD": str(self.workdir),
                "AKI_LETTA_BIN": str(ROOT / "bin" / "letta-aki"),
                "AKI_LETTA_RUNTIME": str(
                    Path.home() / ".local" / "lib" / "aki" / "letta-code" / "current"
                ),
                "AKI_LAYER_ENABLED": "0",
                "AKI_LAYER_NOTIFY_ENABLED": "0",
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.upstream = _FakeGemini()
        self.upstream.start()
        self.processes: list[subprocess.Popen[bytes]] = []
        self.akid_log = self.root / "akid.log"
        self.proxy_log = self.root / "proxy.log"

    def tearDown(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            handle = getattr(process, "_log_handle", None)
            if handle is not None:
                handle.close()
        self.upstream.stop()
        self.tempdir.cleanup()

    def _bootstrap(self) -> dict[str, Any]:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootstrap-member.py"),
                "--config",
                str(self.config),
                "--source",
                str(self.source),
                "--json",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _rewrite_config(self) -> None:
        text = self.config.read_text(encoding="utf-8")
        text = text.replace("sandbox = true", "sandbox = false")
        text = text.replace(
            "path = assets/small/haru.jpg",
            f"path = {ROOT / 'assets' / 'small' / 'haru.jpg'}",
        )
        text = text.replace(
            "base_url = http://127.0.0.1:8317/v1",
            f"base_url = http://127.0.0.1:{self.upstream.port}/v1",
        )
        text = text.replace(
            "api_key =\nuse_proxy = false\n\n[upstream.minimax]",
            "api_key = fake-upstream-key\nuse_proxy = false\n\n[upstream.minimax]",
        )
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.proxy_port = int(probe.getsockname()[1])
        probe.close()
        text = text.replace(
            "listen = 127.0.0.1:7723", f"listen = 127.0.0.1:{self.proxy_port}"
        )
        self.config.write_text(text, encoding="utf-8")

    def _spawn(self, args: list[str], log_path: Path) -> subprocess.Popen[bytes]:
        log = log_path.open("wb")
        process = subprocess.Popen(
            args,
            env=self.env,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        process._log_handle = log  # type: ignore[attr-defined]
        self.processes.append(process)
        return process

    def _wait_socket(self, process: subprocess.Popen[bytes], path: Path) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail(f"process exited early; log:\n{self.akid_log.read_text(errors='replace')[-4000:]}")
            time.sleep(0.1)
        self.fail(f"socket did not appear: {path}")

    def test_haru_joins_isolated_mqtt_roster_and_handles_mentions(self) -> None:
        asyncio.run(self._run_isolated_mqtt_member_test())

    async def _run_isolated_mqtt_member_test(self) -> None:
        broker = FakeMqttBroker()
        await broker.start()
        magic = "haru-isolated-member-test"
        user_id = "legacy_48487683f202ce845f7df0e56c94b11d"
        peer_id = "legacy_8ddfb8585d44e615b2fe8b37846b4ef6"
        final_texts: list[str] = []
        assembler = TextChunkAssembler(magic)

        async def on_message(topic: str, payload: bytes, _replayed: bool) -> None:
            if topic != "tokifactor/text":
                return
            try:
                raw = payload.decode("utf-8")
            except UnicodeDecodeError:
                return
            if looks_like_chunk(raw):
                chunk = parse_text_chunk(raw)
                envelope = assembler.add(chunk) if chunk is not None else None
            else:
                envelope = decode_single_payload(raw, magic)
            if envelope is None:
                return
            if (
                envelope.sender_id == self.boot["identity"]["id"]
                and envelope.metadata.get("kind") == "turn"
                and envelope.metadata.get("phase") == "final"
            ):
                final_texts.append(envelope.text)

        publisher = MqttClient(
            "127.0.0.1", broker.port, client_id="haru-isolated-test-publisher", on_message=on_message
        )
        try:
            boot = self._bootstrap()
            self.boot = boot
            self.assertEqual(boot["status"], "created")
            self._rewrite_config()

            self._spawn(
                [sys.executable, str(ROOT / "engine" / "akid_proxy.py"), "--profile", "haru"],
                self.proxy_log,
            )
            await self._wait_tcp_async("127.0.0.1", self.proxy_port, self.proxy_log)

            akid = self._spawn(
                [
                    sys.executable,
                    "-m",
                    "engine.akid",
                    "--profile",
                    "haru",
                    "--mqtt-host",
                    "127.0.0.1",
                    "--mqtt-port",
                    str(broker.port),
                    "--mqtt-magic",
                    magic,
                    "--no-layer",
                    "--no-layer-notify",
                    "--no-camera",
                    "--no-screenshot",
                    "--no-live-context",
                ],
                self.akid_log,
            )
            socket_path = self.runtime / "aki" / "haru.sock"
            await self._wait_socket_async(akid, socket_path)

            client = GatewayClient(socket_path).connect()
            try:
                client.handshake()
                deadline = time.monotonic() + 30
                health: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    health = client.call("aki.health", timeout=10)
                    if health.get("ready"):
                        break
                    await asyncio.sleep(0.2)
                self.assertTrue(health.get("ready"), health)
                self.assertTrue(health["channel"]["connected"])
                self.assertEqual(health["channel_identity"]["id"], boot["identity"]["id"])
                self.assertEqual(health["channel"]["wait_for"], ["Aki", "Fuyu"])
                self.assertEqual(
                    health["external_tools"]["tools"],
                    [
                        "acquire_execution_lock",
                        "search_chat_history",
                        "comfyui_startup",
                        "comfyui_generate",
                        "comfyui_end",
                        "tokifactor_send_image",
                        "tokifactor_send_file",
                        "wait_user",
                        "wait_other",
                    ],
                )
            finally:
                client.close()

            await publisher.connect()
            await publisher.subscribe("tokifactor/text", qos=1)

            status = {
                "schema": "akid.v1",
                "kind": "status",
                "protocolVersion": "1",
                "memberId": peer_id,
                "memberName": "Aki",
                "sourceId": peer_id,
                "agentId": peer_id,
                "agentName": "Aki",
                "aliases": "Aki,aki,秋",
                "memberKind": "agent",
                "description": "isolated peer",
            }
            for payload in encode_text_payloads(
                "Aki status", magic, sender="Aki", sender_id=peer_id, metadata=status
            ):
                await publisher.publish(CONTROL_TOPIC, payload.encode("utf-8"), qos=0)
            await asyncio.sleep(2.0)

            async def publish_user(text: str) -> None:
                for payload in encode_text_payloads(
                    text, magic, sender="Acite Lee", sender_id=user_id
                ):
                    await publisher.publish("tokifactor/text", payload.encode("utf-8"), qos=1)

            await publish_user("@Aki 只回复 Aki。")
            await asyncio.sleep(3.0)
            self.assertEqual(final_texts, [], "Haru answered a message addressed to another member")

            await publish_user("@Haru 用一句话介绍你自己，并叫我一声。")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not final_texts:
                await asyncio.sleep(0.2)
            self.assertTrue(final_texts, "Haru did not answer @Haru on the isolated channel")
            self.assertTrue(any("哥哥" in text for text in final_texts), final_texts)

            final_texts.clear()
            message_id = str(uuid.uuid4())
            _, picture_payloads = encode_picture_payloads(
                PNG_1X1,
                magic,
                sender="Acite Lee",
                sender_id=user_id,
                caption="图片1",
                message_id=message_id,
                attachment_index=0,
                attachment_count=1,
            )
            for payload in picture_payloads:
                await publisher.publish(PICTURE_TOPIC, payload.encode("utf-8"), qos=1)
            for payload in encode_text_payloads(
                "@Haru 看看图片1。",
                magic,
                sender="Acite Lee",
                sender_id=user_id,
                message_id=message_id,
                metadata={"attachmentCount": "1"},
            ):
                await publisher.publish("tokifactor/text", payload.encode("utf-8"), qos=1)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not final_texts:
                await asyncio.sleep(0.2)
            self.assertTrue(final_texts, "Haru did not answer the multimodal message")
            request = self.upstream.requests[-1]
            user_messages = [
                message
                for message in request.get("messages", [])
                if message.get("role") == "user"
            ]
            last_user = user_messages[-1]
            parts = last_user.get("content")
            self.assertIsInstance(parts, list, last_user)
            self.assertTrue(
                any(part.get("type") in ("image_url", "input_image") for part in parts),
                parts,
            )
            system_text = "\n".join(
                str(message.get("content") or "")
                for message in request.get("messages", [])
                if message.get("role") == "system"
            )
            self.assertNotIn(base64.b64encode(PNG_1X1).decode("ascii"), system_text)
        finally:
            await publisher.close()
            await broker.stop()

    async def _wait_tcp_async(self, host: str, port: int, log_path: Path) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                _reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        self.fail(f"TCP service did not start:\n{log_path.read_text(errors='replace')[-3000:]}")

    async def _wait_socket_async(
        self, process: subprocess.Popen[bytes], path: Path
    ) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail(
                    f"process exited early; log:\n"
                    f"{self.akid_log.read_text(errors='replace')[-4000:]}"
                )
            await asyncio.sleep(0.1)
        self.fail(f"socket did not appear: {path}")


if __name__ == "__main__":
    unittest.main()
