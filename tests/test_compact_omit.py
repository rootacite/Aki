from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import akictl
from engine.akictl.chat import _parse_compact_args
from engine.protocol import ErrorCode, RpcError
from tests.test_exclusive_tools import make_server


def tool_result(
    message_id: str,
    call_id: str,
    content,
    *,
    tool_name: str = "Read",
    metadata: dict | None = None,
) -> dict:
    return {
        "type": "message",
        "id": f"row-{message_id}",
        "message": {
            "id": message_id,
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": tool_name,
            "content": content,
            "isError": False,
            "metadata": metadata or {"agent_id": "agent", "conversation_id": "default"},
        },
    }


class CompactOmitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.server = make_server(self.root)
        self.conversation = self.server._conversation_dir()
        self.conversation.mkdir(parents=True, exist_ok=True)
        self.messages = self.conversation / "messages.jsonl"
        self.conversation_record = {
            "id": "default",
            "in_context_message_ids": ["ui-1", "ui-2"],
            "summary": None,
        }
        (self.conversation / "conversation.json").write_text(
            json.dumps(self.conversation_record), encoding="utf-8"
        )
        self.rows = [
            {"type": "session", "version": 3, "id": "default"},
            {
                "type": "message",
                "id": "row-user",
                "message": {
                    "id": "ui-1",
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                },
            },
            tool_result(
                "ui-2",
                "call-string",
                "plain tool output",
                metadata={"agent_id": "agent", "custom": "keep"},
            ),
            tool_result(
                "ui-3",
                "call-list",
                [
                    {"type": "text", "text": "list output"},
                    {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
                ],
            ),
            tool_result("ui-4", "call-done", "[Omitted]"),
            {
                "type": "message",
                "id": "row-assistant",
                "message": {
                    "id": "ui-5",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "reply"}],
                },
            },
        ]
        self.write_rows(self.rows)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def write_rows(self, rows: list[dict]) -> None:
        self.messages.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def read_rows(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.messages.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def test_replaces_only_tool_results_and_preserves_metadata(self) -> None:
        reload = mock.AsyncMock(return_value={"reloaded": True})
        with mock.patch.object(
                self.server.backend, "reload_conversation", new=reload, create=True
            ):
            result = await self.server._omit_conversation_tool_results()
        self.assertEqual(result["tool_results_replaced"], 2)
        self.assertEqual(result["tool_results_skipped"], 1)
        backup = Path(result["omit_tool_results_backup"])
        self.assertTrue(backup.is_dir())
        self.assertIn("omit-tool-results", backup.name)
        self.assertIn("plain tool output", (backup / "messages.jsonl").read_text())

        rows = self.read_rows()
        self.assertEqual(rows[1]["message"]["content"], [{"type": "text", "text": "hello"}])
        replaced_string = rows[2]["message"]
        self.assertEqual(replaced_string["content"], "[Omitted]")
        self.assertEqual(replaced_string["toolCallId"], "call-string")
        self.assertEqual(replaced_string["toolName"], "Read")
        self.assertEqual(replaced_string["metadata"]["custom"], "keep")
        replaced_list = rows[3]["message"]
        self.assertEqual(
            replaced_list["content"], [{"type": "text", "text": "[Omitted]"}]
        )
        self.assertEqual(replaced_list["toolCallId"], "call-list")
        self.assertEqual(rows[4]["message"]["content"], "[Omitted]")
        self.assertEqual(rows[5]["message"]["content"], [{"type": "text", "text": "reply"}])
        self.assertEqual(
            json.loads((self.conversation / "conversation.json").read_text()),
            self.conversation_record,
        )
        reload.assert_awaited_once()

    async def test_idempotent_and_no_tool_results(self) -> None:
        reload = mock.AsyncMock(return_value={"reloaded": True})
        with mock.patch.object(
                self.server.backend, "reload_conversation", new=reload, create=True
            ):
            first = await self.server._omit_conversation_tool_results()
            second = await self.server._omit_conversation_tool_results()
        self.assertEqual(first["tool_results_replaced"], 2)
        self.assertEqual(second["tool_results_replaced"], 0)
        self.assertEqual(second["tool_results_skipped"], 3)
        self.assertIsNone(second["omit_tool_results_backup"])
        self.assertEqual(reload.await_count, 1)

        self.write_rows(
            [
                {"type": "session", "version": 3, "id": "default"},
                {
                    "type": "message",
                    "id": "row-user",
                    "message": {"id": "ui-1", "role": "user", "content": "hi"},
                },
            ]
        )
        with mock.patch.object(
            self.server.backend,
            "reload_conversation",
            new=mock.AsyncMock(return_value={"reloaded": True}),
            create=True,
        ) as reload_again:
            result = await self.server._omit_conversation_tool_results()
        self.assertEqual(result["tool_results_replaced"], 0)
        self.assertEqual(result["tool_results_skipped"], 0)
        self.assertIsNone(result["omit_tool_results_backup"])
        reload_again.assert_not_awaited()

    async def test_reload_failure_restores_original(self) -> None:
        original = self.messages.read_bytes()
        reload = mock.AsyncMock(side_effect=[RuntimeError("reload failed"), {"reloaded": True}])
        with mock.patch.object(
                self.server.backend, "reload_conversation", new=reload, create=True
            ):
            with self.assertRaises(RpcError) as raised:
                await self.server._omit_conversation_tool_results()
        self.assertEqual(raised.exception.code, ErrorCode.COMPACT_FAILED)
        self.assertIn("original conversation restored", raised.exception.message)
        self.assertEqual(self.messages.read_bytes(), original)
        self.assertEqual(reload.await_count, 2)

    async def test_rollback_reload_failure_is_reported(self) -> None:
        original = self.messages.read_bytes()
        reload = mock.AsyncMock(
            side_effect=[RuntimeError("first"), RuntimeError("second")]
        )
        with mock.patch.object(
                self.server.backend, "reload_conversation", new=reload, create=True
            ):
            with self.assertRaises(RpcError) as raised:
                await self.server._omit_conversation_tool_results()
        self.assertEqual(raised.exception.code, ErrorCode.COMPACT_FAILED)
        self.assertIn("rollback also failed", raised.exception.message)
        self.assertEqual(self.messages.read_bytes(), original)

    async def test_compact_rpc_applies_omission_after_success(self) -> None:
        omit = mock.AsyncMock(
            return_value={
                "tool_results_replaced": 2,
                "tool_results_skipped": 1,
                "omit_tool_results_backup": "/tmp/backup",
            }
        )
        with mock.patch.object(
            self.server,
            "_aki_bridge_available",
            return_value=True,
        ), mock.patch.object(
            self.server,
            "_compact_conversation_bridge",
            new=mock.AsyncMock(
                return_value={
                    "compacted": True,
                    "mode": "sliding_window",
                    "num_messages_before": 10,
                    "num_messages_after": 4,
                    "summary": "summary",
                    "reflection": "failed",
                }
            ),
        ), mock.patch.object(
            self.server, "_omit_conversation_tool_results", new=omit
        ):
            result = await self.server._compact_conversation(
                {"mode": "sliding_window", "omit_tool_results": True}
            )
        self.assertTrue(result["compacted"])
        self.assertEqual(result["num_messages_before"], 10)
        self.assertEqual(result["num_messages_after"], 4)
        self.assertEqual(result["tool_results_replaced"], 2)
        self.assertEqual(result["tool_results_skipped"], 1)
        self.assertEqual(result["omit_tool_results_backup"], "/tmp/backup")
        omit.assert_awaited_once()

    async def test_compact_failure_does_not_omit(self) -> None:
        omit = mock.AsyncMock()
        with mock.patch.object(
            self.server,
            "_aki_bridge_available",
            return_value=True,
        ), mock.patch.object(
            self.server,
            "_compact_conversation_bridge",
            new=mock.AsyncMock(side_effect=RuntimeError("compact failed")),
        ), mock.patch.object(
            self.server, "_omit_conversation_tool_results", new=omit
        ):
            with self.assertRaises(RpcError) as raised:
                await self.server._compact_conversation(
                    {"mode": "all", "omit_tool_results": True}
                )
        self.assertEqual(raised.exception.code, ErrorCode.COMPACT_FAILED)
        omit.assert_not_awaited()

    async def test_rpc_rejects_non_boolean_omit_flag(self) -> None:
        with self.assertRaises(RpcError) as raised:
            await self.server._compact_conversation(
                {"omit_tool_results": "yes"}
            )
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_PARAMS)


class CompactCliTests(unittest.TestCase):
    def test_cli_dispatches_omit_flag(self) -> None:
        args = mock.Mock(
            mode=None,
            mode_option="sliding_window",
            timeout=120,
            omit_tool_results=True,
        )
        with mock.patch(
            "engine.akictl._management", return_value=0
        ) as management, mock.patch("engine.akictl.print"):
            result = akictl._compact(args, "haru")
        self.assertEqual(result, 0)
        management.assert_called_once_with(
            args,
            "aki.conversation.compact",
            {
                "timeout": 120,
                "mode": "sliding_window",
                "omit_tool_results": True,
            },
            profile="haru",
            timeout=150.0,
        )

    def test_cli_parser_accepts_omit_flag_with_mode(self) -> None:
        parser = akictl.build_parsers()["compact"]
        args = parser.parse_args(["sliding_window", "--omit-tool-results"])
        self.assertEqual(args.mode, "sliding_window")
        self.assertTrue(args.omit_tool_results)
        args = parser.parse_args(["--omit-tool-results"])
        self.assertIsNone(args.mode)
        self.assertTrue(args.omit_tool_results)

    def test_repl_compact_parser_accepts_mode_and_flag_in_either_order(self) -> None:
        self.assertEqual(
            _parse_compact_args(["sliding_window", "--omit-tool-results"]),
            {"mode": "sliding_window", "omit_tool_results": True},
        )
        self.assertEqual(
            _parse_compact_args(["--omit-tool-results", "all"]),
            {"omit_tool_results": True, "mode": "all"},
        )
        self.assertEqual(_parse_compact_args([]), {})


if __name__ == "__main__":
    unittest.main()
