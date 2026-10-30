from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine.akid.remote_tools import RemoteToolsMixin


class RemoteToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_call_uses_isolated_letta_process(self) -> None:
        server = SimpleNamespace(
            config=SimpleNamespace(
                letta_bin="letta-aki",
                backend_dir=Path("/tmp/aki-backend"),
                cwd=Path("/tmp/aki-worktree"),
                agent_id="agent-local-test",
                mcp_tool_timeout=30.0,
            ),
            writable=SimpleNamespace(letta_home=Path("/tmp/aki-home")),
        )
        payload = json.dumps(
            {"content": [{"type": "text", "text": "search result"}]}
        ).encode()
        process = mock.Mock(returncode=0)
        process.communicate = mock.AsyncMock(return_value=(payload, b""))
        create = mock.AsyncMock(return_value=process)

        with mock.patch(
            "engine.akid.remote_tools.asyncio.create_subprocess_exec",
            new=create,
        ):
            text, is_error = await RemoteToolsMixin._call_mcp_tool(
                server,
                "mcp__tavily__tavily_search",
                {"query": "OpenAI"},
            )

        self.assertEqual((text, is_error), ("search result", False))
        args, kwargs = create.await_args
        self.assertEqual(
            args[:5],
            ("letta-aki", "--backend", "local", "mcp", "call"),
        )
        self.assertIn("mcp__tavily__tavily_search", args)
        self.assertEqual(
            kwargs["env"]["LETTA_LOCAL_BACKEND_DIR"],
            "/tmp/aki-backend",
        )
        self.assertEqual(kwargs["env"]["LETTA_HOME"], "/tmp/aki-home")


if __name__ == "__main__":
    unittest.main()
