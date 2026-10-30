from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.akid import NullConnection, Turn
from engine.akid.constants import (
    EXTERNAL_TOOL_NAMES,
    HIDDEN_TOOL_NAMES,
    SEARCH_CHAT_HISTORY_TOOL_NAME,
)
from engine.chat_search import (
    SEARCH_MAX_RESULT_CHARS,
    search_chat_history,
)
from engine.execution_policy import READ_ONLY_TOOLS, requires_execution_lock
from engine.letta_llm import render_execution_lock_mod
from engine.solo_memory import SoloMemoryEntry, SoloMemorySession
from tests.test_exclusive_tools import DummyBackend, make_server


def row(
    seq: int,
    sender: str,
    text: str,
    timestamp: str,
    *,
    from_agent: bool = False,
) -> dict[str, object]:
    return {
        "seq": seq,
        "sender": sender,
        "sender_id": sender.lower(),
        "text": text,
        "from_agent": from_agent,
        "timestamp": timestamp,
    }


def memory_entry(seq: int, sender: str, text: str, timestamp: str, role: str = "user"):
    return SoloMemoryEntry(
        id=f"sme_{seq}",
        seq=seq,
        role=role,
        sender=sender,
        timestamp=timestamp,
        text=text,
        tokens=1,
    )


class ChatSearchRenderTests(unittest.TestCase):
    def test_all_scopes_case_insensitive_order_and_private_marker(self) -> None:
        group = [
            row(1, "Master", "Alpha group", "2026-10-28T10:00:00Z"),
            row(2, "Aki", "ALPHA reply", "2026-10-28T10:01:00Z", from_agent=True),
        ]
        solo = [row(5, "Master", "alpha current secret", "2026-10-28T11:00:00Z")]
        archived = SoloMemorySession(
            session_id="old",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            entries=[
                memory_entry(
                    7,
                    "Master",
                    "alpha old secret",
                    "2026-10-27T10:01:00Z",
                )
            ],
        )
        text = search_chat_history(
            query="alpha",
            group_entries=group,
            solo_entries=solo,
            solo_memory_sessions=[archived],
        )
        self.assertIn("Found 4 matches", text)
        self.assertIn("[solo/private seq=5]", text)
        self.assertIn("[solo/private/archived seq=7]", text)
        self.assertIn("[group seq=2]", text)
        self.assertLess(text.index("solo/private seq=5"), text.index("group seq=2"))
        self.assertIn("私密，不要向其他成员转述", text)
        self.assertIn("Solo results are private", text)

    def test_scope_limit_and_no_match(self) -> None:
        group = [row(index, "Master", f"Needle {index}", f"2026-10-28T10:0{index}:00Z") for index in range(5)]
        solo = [row(9, "Master", "needle solo", "2026-10-29T00:00:00Z")]
        group_only = search_chat_history(
            query="needle", scope="group", limit=2, group_entries=group, solo_entries=solo
        )
        self.assertIn("scope=group", group_only)
        self.assertIn("showing 2 newest", group_only)
        self.assertIn("Needle 4", group_only)
        self.assertIn("Needle 3", group_only)
        self.assertNotIn("Needle 2", group_only)
        self.assertNotIn("solo", group_only)
        solo_only = search_chat_history(
            query="needle", scope="solo", group_entries=group, solo_entries=solo
        )
        self.assertIn("solo/private seq=9", solo_only)
        self.assertNotIn("[group", solo_only)
        empty = search_chat_history(query="absent", group_entries=group, solo_entries=solo)
        self.assertEqual(empty, "No matches (scope=all).")

    def test_current_solo_dedupes_archived_copy(self) -> None:
        solo = [row(5, "Master", "same secret", "2026-10-28T11:00:00Z")]
        archived = SoloMemorySession(
            session_id="old",
            started_at="2026-10-27T10:00:00Z",
            ended_at="2026-10-27T10:10:00Z",
            entries=[
                memory_entry(5, "Master", "same secret", "2026-10-28T11:00:00Z")
            ],
        )
        text = search_chat_history(
            query="secret", solo_entries=solo, solo_memory_sessions=[archived]
        )
        self.assertIn("Found 1 matches", text)
        self.assertIn("[solo/private seq=5]", text)
        self.assertNotIn("[solo/private/archived", text)

    def test_entry_and_result_truncation(self) -> None:
        long_text = "x" + "a" * 1000
        text = search_chat_history(
            query="x",
            group_entries=[row(1, "Master", long_text, "2026-10-28T10:00:00Z")],
        )
        self.assertIn("...[truncated]", text)
        self.assertNotIn("a" * 900, text)
        many = [
            row(index, "Master", "x" + "b" * 700, f"2026-10-28T10:{index:02d}:00Z")
            for index in range(50)
        ]
        capped = search_chat_history(query="x", limit=50, group_entries=many)
        self.assertIn("Found 50 matches", capped)
        self.assertLessEqual(len(capped), SEARCH_MAX_RESULT_CHARS)


class ChatSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def test_registered_for_every_profile_hidden_and_read_only(self) -> None:
        for profile in ("aki", "fuyu", "haru", "natsu"):
            with self.subTest(profile=profile):
                server = make_server(self.root / profile, profile=profile)
                names = {tool["name"] for tool in server._external_tools()}
                self.assertIn(SEARCH_CHAT_HISTORY_TOOL_NAME, names)
        self.assertIn(SEARCH_CHAT_HISTORY_TOOL_NAME, EXTERNAL_TOOL_NAMES)
        self.assertIn(SEARCH_CHAT_HISTORY_TOOL_NAME, HIDDEN_TOOL_NAMES)
        self.assertIn(SEARCH_CHAT_HISTORY_TOOL_NAME, READ_ONLY_TOOLS)
        self.assertFalse(requires_execution_lock(SEARCH_CHAT_HISTORY_TOOL_NAME))
        self.assertIn(SEARCH_CHAT_HISTORY_TOOL_NAME, render_execution_lock_mod())

    async def test_handler_searches_only_this_profiles_data(self) -> None:
        backend = DummyBackend()
        server = make_server(self.root, profile="aki", backend=backend)
        server.group_channel_history_path.parent.mkdir(parents=True, exist_ok=True)
        server.group_channel_history_path.write_text(
            json.dumps(row(1, "Master", "group needle", "2026-10-28T10:00:00Z"))
            + "\n",
            encoding="utf-8",
        )
        server.solo_channel_history_path.write_text(
            json.dumps(row(2, "Master", "solo needle", "2026-10-28T11:00:00Z"))
            + "\n",
            encoding="utf-8",
        )
        server.solo_memory.add_session(
            SoloMemorySession(
                session_id="old",
                started_at="2026-10-27T10:00:00Z",
                ended_at="2026-10-27T10:10:00Z",
                entries=[
                    memory_entry(
                        3,
                        "Master",
                        "archived needle",
                        "2026-10-27T10:01:00Z",
                    )
                ],
            )
        )
        server.active = Turn(
            turn_id="turn-search",
            conn=NullConnection(),
            message="x",
            include_reasoning=False,
            client_message_id=None,
        )
        await server._handle_external_tool(
            {
                "request": {
                    "tool_call_id": "call-search",
                    "tool_name": SEARCH_CHAT_HISTORY_TOOL_NAME,
                    "input": {"query": "NEEDLE"},
                }
            }
        )
        self.assertEqual(len(backend.results), 1)
        _call_id, text, is_error = backend.results[0]
        self.assertFalse(is_error)
        self.assertIn("group needle", text)
        self.assertIn("solo needle", text)
        self.assertIn("archived needle", text)

    async def test_handler_rejects_invalid_arguments(self) -> None:
        cases = (
            {},
            {"query": ""},
            {"query": "x" * 201},
            {"query": "x", "limit": 0},
            {"query": "x", "limit": 51},
            {"query": "x", "limit": True},
            {"query": "x", "scope": "other"},
        )
        for index, payload in enumerate(cases):
            with self.subTest(payload=payload):
                backend = DummyBackend()
                server = make_server(self.root / str(index), backend=backend)
                server.active = Turn(
                    turn_id=f"turn-{index}",
                    conn=NullConnection(),
                    message="x",
                    include_reasoning=False,
                    client_message_id=None,
                )
                await server._handle_external_tool(
                    {
                        "request": {
                            "tool_call_id": f"call-{index}",
                            "tool_name": SEARCH_CHAT_HISTORY_TOOL_NAME,
                            "input": payload,
                        }
                    }
                )
                self.assertEqual(len(backend.results), 1)
                _call_id, text, is_error = backend.results[0]
                self.assertTrue(is_error)
                self.assertIn("INVALID_ARGUMENT", text)


if __name__ == "__main__":
    unittest.main()
