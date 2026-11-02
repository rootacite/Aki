from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.live_context import (
    ChannelMember,
    ChannelTranscriptEntry,
)
from engine.solo_memory import SoloMemoryEntry, SoloMemorySession


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "local-benchmark.py"


def load_benchmark_module():
    spec = importlib.util.spec_from_file_location("local_benchmark", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BENCHMARK = load_benchmark_module()


class BenchmarkPromptTests(unittest.TestCase):
    def context(self):
        member = ChannelMember(
            id="legacy_aki",
            name="Aki",
            aliases=("Aki",),
            description="test agent",
            is_self=True,
            kind="agent",
        )
        memory = SoloMemorySession(
            session_id="solo-old",
            started_at="2026-10-27T00:00:00Z",
            ended_at="2026-10-27T00:10:00Z",
            summary="old summary",
            entries=[
                SoloMemoryEntry(
                    id="sme_old",
                    seq=1,
                    role="user",
                    sender="Master",
                    timestamp="2026-10-27T00:01:00Z",
                    text="old private line",
                    tokens=4,
                )
            ],
        )
        return BENCHMARK.SoloContext(
            profile="aki",
            username="Master",
            display_name="Acite Lee",
            user_id="legacy_user",
            member=member,
            scene="private room",
            base_system="BASE SYSTEM",
            emoji_entries=[],
            conversation_messages=[
                {"role": "user", "content": "earlier user"},
                {"role": "assistant", "content": "earlier reply"},
            ],
            group_entries=[
                ChannelTranscriptEntry(
                    seq=3,
                    sender="Master",
                    sender_id="legacy_user",
                    text="frozen group line",
                )
            ],
            solo_entries=[],
            solo_sessions=[memory],
            session_id="solo-test",
            session_active=True,
            next_seq=4,
            online_profiles=frozenset({"aki", "fuyu"}),
            history_messages=2,
            history_tokens=20,
        )

    def test_solo_stable_and_volatile_block_order_matches_runtime(self) -> None:
        built = BENCHMARK.build_request(
            self.context(),
            task="benchmark task",
            live_block="# runtime-context\n\ndisabled desktop context",
            model="local-model.gguf",
            max_tokens=128,
            thinking=True,
            reasoning_budget_tokens=1536,
            tools=[],
            temperature=None,
            top_p=None,
            top_k=None,
            sent_at_ms=1_700_000_000_000,
        )
        system = built.system_prompt
        self.assertTrue(system.startswith("BASE SYSTEM\n\n<channel-identity"))
        self.assertLess(system.index("<channel-identity"), system.index("<tool-ownership"))
        self.assertLess(system.index("<tool-ownership"), system.index("<solo-policy"))
        self.assertLess(system.index("<solo-policy"), system.index("# solo-mode"))
        self.assertLess(system.index("# solo-mode"), system.index("# solo-memories"))
        self.assertLess(system.index("# solo-memories"), system.index("# channel-transcript"))
        self.assertIn("old private line", system)
        self.assertIn("frozen group line", system)
        self.assertNotIn('just_ended="true"', system)

        prefix = built.user_prefix
        self.assertLess(prefix.index("# runtime-context"), prefix.index("# format-guard"))
        self.assertLess(prefix.index("# format-guard"), prefix.index("# wait-user"))
        messages = built.payload["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": system})
        self.assertEqual(messages[1]["content"], "earlier user")
        self.assertEqual(messages[2]["content"], "earlier reply")
        current = messages[-1]["content"]
        self.assertTrue(current.startswith(prefix))
        self.assertIn("[akid MQTT channel message]", current)
        self.assertTrue(current.endswith("benchmark task"))
        self.assertEqual(built.payload["model"], "local-model.gguf")
        self.assertEqual(built.payload["reasoning_effort"], "medium")
        self.assertEqual(built.payload["reasoning_budget_tokens"], 1536)
        self.assertTrue(built.payload["stream"])
        self.assertNotIn("tools", built.payload)
        labels = [part["label"] for part in built.token_parts]
        self.assertIn("system/base", labels)
        self.assertIn("system/channel-identity", labels)
        self.assertIn("system/tool-ownership", labels)
        self.assertIn("system/solo-policy", labels)
        self.assertIn("system/solo-mode", labels)
        self.assertIn("system/solo-memories", labels)
        self.assertIn("system/channel-transcript", labels)
        self.assertIn("user-prefix/live-context", labels)
        self.assertIn("user-prefix/format-guard", labels)
        self.assertIn("user-prefix/wait-user", labels)
        self.assertIn("current-user/envelope+task", labels)
        self.assertEqual(
            sum(int(part["tokens"]) for part in built.token_parts),
            built.total_tokens,
        )
        breakdown = BENCHMARK.format_token_breakdown(built)
        self.assertIn("prompt token breakdown", breakdown)
        self.assertIn("TOTAL", breakdown)
        case = next(
            item for item in BENCHMARK.BENCHMARK_CASES
            if item.key == "coding-merge-intervals"
        )
        preview = BENCHMARK.format_case_preview(case, 128)
        self.assertIn("=== benchmark case: coding-merge-intervals ===", preview)
        self.assertIn("function=merge_intervals", preview)
        self.assertIn("tests=", preview)

    def test_first_cases_use_aki_private_interaction_framing(self) -> None:
        generation, coding, _rest = BENCHMARK.BENCHMARK_CASES
        self.assertIn("我靠在你旁边", generation.prompt)
        self.assertIn("我想多赖你一会儿", generation.prompt)
        self.assertNotIn("请以小秋", generation.prompt)
        self.assertNotIn("测试", generation.prompt)
        self.assertNotIn("约三百字", generation.prompt)
        self.assertIn("我窝在你旁边", coding.prompt)
        self.assertIn("帮我写个小函数", coding.prompt)
        self.assertEqual(coding.function, "merge_intervals")

    def test_tool_schemas_are_opt_in(self) -> None:
        built = BENCHMARK.build_request(
            self.context(),
            task="use no tools",
            live_block="",
            model="local-model.gguf",
            max_tokens=64,
            thinking=False,
            reasoning_budget_tokens=0,
            tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
            temperature=0.0,
            top_p=0.9,
            top_k=20,
        )
        self.assertEqual(built.payload["tools"][0]["function"]["name"], "x")
        self.assertEqual(built.payload["temperature"], 0.0)
        self.assertNotIn("reasoning_effort", built.payload)

    def test_benchmark_uses_32k_memories_and_drops_whole_sessions(self) -> None:
        self.assertEqual(BENCHMARK.SOLO_MEMORY_BUDGET_TOKENS, 32 * 1024)
        old = SoloMemorySession(
            session_id="old",
            started_at="2026-10-24T10:00:00Z",
            ended_at="2026-10-24T10:10:00Z",
            summary="OLD SESSION SUMMARY",
            entries=[
                SoloMemoryEntry(
                    id="sme_old",
                    seq=1,
                    role="user",
                    sender="Master",
                    timestamp="2026-10-24T10:01:00Z",
                    text="OLD_SESSION_BODY " + "旧" * 40000,
                    tokens=40000,
                )
            ],
        )
        newest = SoloMemorySession(
            session_id="newest",
            started_at="2026-10-26T10:00:00Z",
            ended_at="2026-10-26T10:10:00Z",
            summary="NEWEST SESSION SUMMARY",
            entries=[
                SoloMemoryEntry(
                    id="sme_new",
                    seq=2,
                    role="user",
                    sender="Master",
                    timestamp="2026-10-26T10:01:00Z",
                    text="NEWEST_SESSION_BODY",
                    tokens=4,
                )
            ],
        )
        context = self.context()
        context.solo_sessions = [old, newest]
        built = BENCHMARK.build_request(
            context,
            task="benchmark task",
            live_block="",
            model="local-model.gguf",
            max_tokens=64,
            thinking=False,
            reasoning_budget_tokens=0,
            tools=[],
            temperature=None,
            top_p=None,
            top_k=None,
        )
        self.assertIn("NEWEST_SESSION_BODY", built.system_prompt)
        self.assertNotIn("OLD_SESSION_BODY", built.system_prompt)
        self.assertNotIn("OLD SESSION SUMMARY", built.system_prompt)

    def test_base_system_uses_current_agent_record_not_stale_compiled_cache(self) -> None:
        agent_id = "agent-local-benchmark-test"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AKI_LETTA_BACKEND_DIR": directory}
        ):
            agents = Path(directory) / "agents"
            agents.mkdir(parents=True, exist_ok=True)
            (agents / "agent.json").write_text(
                json.dumps(
                    {"id": agent_id, "system": "CURRENT AGENT SYSTEM PROMPT"}
                ),
                encoding="utf-8",
            )
            conversation = BENCHMARK.conversation_directory(
                Path(directory),
                BENCHMARK.solo_conversation_id(agent_id),
                agent_id,
            )
            conversation.mkdir(parents=True, exist_ok=True)
            (conversation / "system-prompt.json").write_text(
                json.dumps({"content": "STALE COMPILED SYSTEM PROMPT"}),
                encoding="utf-8",
            )
            config = SimpleNamespace(agent_id=agent_id)
            self.assertEqual(
                BENCHMARK.load_base_system(config),
                "CURRENT AGENT SYSTEM PROMPT",
            )

    def test_base_system_injects_current_committed_memfs_projection(self) -> None:
        agent_id = "agent-local-memfs-test"
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AKI_LETTA_BACKEND_DIR": directory}
        ):
            backend = Path(directory)
            agents = backend / "agents"
            agents.mkdir(parents=True)
            (agents / "agent.json").write_text(
                json.dumps({"id": agent_id, "system": "BASE {CORE_MEMORY}"}),
                encoding="utf-8",
            )
            memory = backend / "memfs" / agent_id / "memory"
            (memory / "character").mkdir(parents=True)
            (memory / "MEMORY.md").write_text("memory index\n", encoding="utf-8")
            (memory / "persona.md").write_text("current persona\n", encoding="utf-8")
            (memory / "character" / "MEMORY.md").write_text(
                "character index\n", encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q", str(memory)], check=True)
            subprocess.run(
                ["git", "-C", str(memory), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(memory), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(["git", "-C", str(memory), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(memory), "commit", "-qm", "memory"],
                check=True,
            )
            config = SimpleNamespace(agent_id=agent_id)
            system = BENCHMARK.load_base_system(config)
        self.assertIn("current persona", system)
        self.assertIn("<deferred-memory>", system)
        self.assertIn('<directory path="character/" index="character/MEMORY.md" />', system)
        self.assertNotIn("{CORE_MEMORY}", system)


class BenchmarkStreamTests(unittest.TestCase):
    def test_stream_chat_tracks_ttft_usage_and_reasoning(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                events = [
                    {"choices": [{"delta": {"reasoning_content": "think"}}]},
                    {"choices": [{"delta": {"content": "hello "}}]},
                    {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
                        "timings": {
                            "prompt_n": 11,
                            "prompt_ms": 20.0,
                            "predicted_n": 2,
                            "predicted_ms": 10.0,
                        },
                    },
                ]
                for event in events:
                    self.wfile.write(
                        f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                    time.sleep(0.01)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        live = io.StringIO()
        try:
            with contextlib.redirect_stdout(live):
                result = BENCHMARK.stream_chat(
                    f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                    {"model": "x", "messages": [], "stream": True},
                    timeout=5,
                    live_output=True,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.reasoning, "think")
        self.assertEqual(result.usage["completion_tokens"], 2)
        self.assertEqual(result.timings["predicted_n"], 2)
        self.assertEqual(result.finish_reason, "stop")
        self.assertIsNotNone(result.ttft_s)
        self.assertIsNotNone(result.first_content_s)
        self.assertGreaterEqual(result.elapsed_s, result.ttft_s or 0.0)
        self.assertIn("hello world", live.getvalue())


class BenchmarkCodeTests(unittest.TestCase):
    def test_correct_merge_solution_passes_all_tests(self) -> None:
        case = next(
            item for item in BENCHMARK.BENCHMARK_CASES
            if item.key == "coding-merge-intervals"
        )
        code = """```python
def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
```"""
        check = BENCHMARK.check_code(case, code, execute=True)
        assert check is not None
        self.assertTrue(check.compile_ok)
        self.assertTrue(check.tests_ok, check.error)
        self.assertEqual(check.tests_passed, len(case.tests))

    def test_unsafe_import_is_rejected_before_execution(self) -> None:
        error = BENCHMARK.validate_code_safety("import os\nos.system('true')\n")
        self.assertIn("import not allowed", error)

    def test_syntax_error_is_reported(self) -> None:
        case = next(
            item for item in BENCHMARK.BENCHMARK_CASES
            if item.key == "coding-merge-intervals"
        )
        check = BENCHMARK.check_code(case, "def merge_intervals(:\n  pass\n", execute=False)
        assert check is not None
        self.assertFalse(check.compile_ok)
        self.assertIn("syntax error", check.error)


if __name__ == "__main__":
    unittest.main()
