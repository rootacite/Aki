from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine.live_context import estimate_tokens, render_solo_memories
from engine.solo_memory import (
    FALLBACK_SUMMARY_STATUS,
    SOLO_SUMMARY_TIMEOUT_SECONDS,
    SoloMemoryEntry,
    SoloMemoryManager,
    SoloMemorySession,
    capture_solo_entries,
    default_solo_memory_path,
    fallback_summary,
)


class FakePersonalityAPI:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
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
                payload = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(payload)
                response = fake.responses.pop(0) if fake.responses else {"summary": ""}
                body = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def response_with(payload: dict) -> dict:
    return {
        "choices": [
            {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
        ]
    }


def entry(seq: int, text: str, *, role: str = "user") -> SoloMemoryEntry:
    return SoloMemoryEntry(
        id=f"sme_{seq:024x}",
        seq=seq,
        role=role,
        sender="Master" if role == "user" else "Aki",
        timestamp=f"2026-10-27T10:00:{seq:02d}Z",
        text=text,
        tokens=estimate_tokens(text),
    )


def make_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


class SoloMemoryStoreTests(unittest.TestCase):
    def test_default_path_and_atomic_store(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            self.assertEqual(
                default_solo_memory_path("aki", home),
                home / ".aki" / "solo-memory" / "aki.json",
            )
            path = home / ".aki" / "solo-memory" / "aki.json"
            manager = SoloMemoryManager(
                profile="aki", path=path, memory_dir=home / "memory"
            )
            session = SoloMemorySession(
                session_id="solo-1",
                started_at="2026-10-27T10:00:00Z",
                ended_at="2026-10-27T10:10:00Z",
                summary="summary",
                entries=[entry(1, "hello")],
            )
            manager.add_session(session)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            data = json.loads(path.read_text())
            self.assertEqual(data["sessions"][0]["session_id"], "solo-1")
            loaded = SoloMemoryManager(
                profile="aki", path=path, memory_dir=home / "memory"
            )
            self.assertEqual(loaded.sessions[0].entries[0].text, "hello")

    def test_capture_filters_phase_empty_and_no_reply(self) -> None:
        class Row:
            def __init__(self, seq, text, phase="final", agent=False):
                self.seq = seq
                self.text = text
                self.phase = phase
                self.from_agent = agent
                self.sender = "Aki" if agent else "Master"
                self.timestamp = f"2026-10-27T10:00:0{seq}Z"
                self.message_id = f"m{seq}"

        rows = [
            Row(1, "old", "final"),
            Row(2, "keep"),
            Row(3, "", "final"),
            Row(4, "[NO_REPLY]", "final"),
            Row(5, "progress", "progress"),
            Row(6, "reply", "final", True),
        ]
        entries = capture_solo_entries(rows, session_id="s1", start_seq=1)
        self.assertEqual([item.text for item in entries], ["keep", "reply"])
        self.assertEqual(entries[1].role, "assistant")
        self.assertEqual(fallback_summary(entries).split(":")[0], "2 solo messages")


class SoloMemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.memory = self.root / "memory"
        make_git_repo(self.memory)
        (self.memory / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.memory), "add", "MEMORY.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.memory), "commit", "-qm", "init"], check=True
        )
        self.api = FakePersonalityAPI([])
        self.api.start()
        self.base = "You are Aki.\n\n<tool-ownership>tools</tool-ownership>\n<channel-policy>policy</channel-policy>\n<live-context>live</live-context>"

    async def asyncTearDown(self) -> None:
        self.api.stop()
        self.tempdir.cleanup()

    def manager(self, budget: int = 64 * 1024) -> SoloMemoryManager:
        return SoloMemoryManager(
            profile="aki",
            path=self.root / "solo-memory.json",
            memory_dir=self.memory,
            budget_tokens=budget,
        )

    def api_kwargs(self) -> dict[str, str]:
        return {
            "base_prompt": self.base,
            "proxy_base_url": f"http://127.0.0.1:{self.api.port}/v1",
            "proxy_token": "test-token",
            "model": "gemini-3.8-flash",
            "api": "openai-completions",
            "route_path": "/chat/completions",
        }

    async def test_summary_uses_personality_api_and_strips_runtime_blocks(self) -> None:
        self.api.responses = [response_with({"summary": "安静的一段独处"})]
        manager = self.manager()
        session = SoloMemorySession(
            session_id="s1",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            entries=[entry(1, "我今天有点累")],
        )
        await manager.summarize(session, **self.api_kwargs())
        self.assertEqual(session.summary, "安静的一段独处")
        self.assertEqual(session.summary_status, "ready")
        self.assertGreaterEqual(SOLO_SUMMARY_TIMEOUT_SECONDS, 200.0)
        system = self.api.requests[0]["messages"][0]["content"]
        self.assertIn("You are Aki.", system)
        self.assertNotIn("<channel-policy>", system)
        self.assertNotIn("<tool-ownership>", system)
        self.assertNotIn("<live-context>", system)

    async def test_summary_failure_uses_deterministic_fallback(self) -> None:
        self.api.responses = [{"choices": [{"message": {"content": "not json"}}]}]
        manager = self.manager()
        session = SoloMemorySession(
            session_id="s1",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            entries=[entry(1, "remember this")],
        )
        await manager.summarize(session, **self.api_kwargs())
        self.assertEqual(session.summary_status, FALLBACK_SUMMARY_STATUS)
        self.assertIn("remember this", session.summary)

    async def test_eviction_archives_entries_and_commits_memfs(self) -> None:
        self.api.responses = [
            response_with(
                {"evict_entries": [], "evict_sessions": ["s1"]}
            )
        ]
        manager = self.manager(budget=1024)
        session = SoloMemorySession(
            session_id="s1",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            summary="old session",
            entries=[entry(index, "长内容" * 300) for index in range(1, 5)],
        )
        manager.add_session(session)
        rendered = await manager.render_for_prompt(**self.api_kwargs())
        self.assertEqual(rendered, "")
        self.assertEqual(manager.sessions, [])
        archive = self.memory / "solo" / "s1.md"
        self.assertTrue(archive.is_file())
        self.assertIn("长内容", archive.read_text())
        self.assertTrue((self.memory / "solo" / "MEMORY.md").is_file())
        log = subprocess.run(
            ["git", "-C", str(self.memory), "log", "--oneline"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        self.assertIn("solo memory archive", log)

    async def test_invalid_eviction_does_not_modify_store(self) -> None:
        self.api.responses = [
            response_with(
                {"evict_entries": ["missing"], "evict_sessions": []}
            )
        ]
        manager = self.manager(budget=1024)
        session = SoloMemorySession(
            session_id="s1",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            summary="old session",
            entries=[entry(index, "长内容" * 300) for index in range(1, 5)],
        )
        manager.add_session(session)
        original = self.root.joinpath("solo-memory.json").read_text()
        rendered = await manager.render_for_prompt(**self.api_kwargs())
        self.assertTrue(rendered)
        self.assertLessEqual(estimate_tokens(rendered), manager.budget_tokens)
        self.assertEqual(rendered.count("<solo id="), 1)
        self.assertEqual(self.root.joinpath("solo-memory.json").read_text(), original)
        self.assertIsNotNone(manager.last_error)

    async def test_current_session_is_excluded_from_memories(self) -> None:
        manager = self.manager()
        manager.add_session(
            SoloMemorySession(
                session_id="active",
                started_at="2026-10-27T10:00:00Z",
                ended_at="2026-10-27T10:10:00Z",
                summary="active",
                entries=[entry(1, "secret current")],
            )
        )
        manager.add_session(
            SoloMemorySession(
                session_id="old",
                started_at="2026-10-26T10:00:00Z",
                ended_at="2026-10-26T10:10:00Z",
                summary="old",
                entries=[entry(2, "older memory")],
            )
        )
        rendered = await manager.render_for_prompt(
            current_session_id="active", **self.api_kwargs()
        )
        self.assertIn("older memory", rendered)
        self.assertNotIn("secret current", rendered)


if __name__ == "__main__":
    unittest.main()
