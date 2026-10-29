from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from engine.execution_lock import ExecutionLock
from engine.execution_policy import (
    EXECUTION_LOCK_TAG,
    clear_execution_lock_policy,
    execution_policy_payload,
    is_read_only_tool,
    requires_execution_lock,
)


def _hold_lock(lock_path: str, status_path: str, ready, release) -> None:
    lock = ExecutionLock(Path(lock_path), Path(status_path))
    acquired, _ = lock.acquire(profile="fuyu", agent_id="fuyu-agent", turn_id="t1")
    ready.set()
    release.wait(10.0)
    if acquired:
        lock.release(profile="fuyu", turn_id="t1")


class ExecutionPolicyTests(unittest.TestCase):
    def test_static_read_only_classification_fails_closed(self) -> None:
        for name in (
            "Read",
            "ReadLSP",
            "Grep",
            "Glob",
            "LS",
            "ViewImage",
            "desktop_take_screenshot",
            "camera_take_photo",
            "wait_user",
            "mcp__tavily__tavily_search",
        ):
            self.assertTrue(is_read_only_tool(name), name)
            self.assertFalse(requires_execution_lock(name), name)
        for name in (
            "Bash",
            "Memory",
            "Agent",
            "Write",
            "Edit",
            "ApplyPatch",
            "mcp__local__nmap_scan",
            "unknown_tool",
        ):
            self.assertFalse(is_read_only_tool(name), name)
            self.assertTrue(requires_execution_lock(name), name)
        self.assertFalse(is_read_only_tool("generate_image"))
        self.assertFalse(requires_execution_lock("generate_image"))
        self.assertIn(
            "generate_image", execution_policy_payload()["lock_exempt_tools"]
        )
        self.assertFalse(is_read_only_tool("tokifactor_send_image"))
        self.assertFalse(requires_execution_lock("tokifactor_send_image"))
        self.assertIn(
            "tokifactor_send_image",
            execution_policy_payload()["lock_exempt_tools"],
        )
        self.assertFalse(is_read_only_tool("tokifactor_send_file"))
        self.assertFalse(requires_execution_lock("tokifactor_send_file"))
        self.assertIn(
            "tokifactor_send_file",
            execution_policy_payload()["lock_exempt_tools"],
        )
        self.assertFalse(requires_execution_lock("acquire_execution_lock"))
        self.assertFalse(requires_execution_lock("release_execution_lock"))

    def test_base_prompts_define_lock_rules_in_english(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for name in ("aki-system.md", "fuyu-system.md"):
            prompt = (root / "prompts" / name).read_text(encoding="utf-8")
            self.assertIn("## Execution lock", prompt)
            self.assertIn("Some tools are read-only", prompt)
            self.assertIn("`Grep`, `Glob`, `LS`", prompt)
            self.assertNotIn("ListDir", prompt)
            self.assertIn("requires the device-wide execution lock", prompt)
            self.assertIn("`tokifactor_send_image`, and `tokifactor_send_file` are explicitly lock-exempt", prompt)
            self.assertIn(
                "Manual lock release is intentionally unavailable", prompt
            )
            self.assertNotIn("`release_execution_lock`", prompt)
            self.assertIn("Do not mention lock status unless the user explicitly asks", prompt)
            self.assertIn("one assistant response", prompt)
            self.assertNotIn(f"<{EXECUTION_LOCK_TAG}", prompt)

    def test_base_prompts_waive_mention_requirement_in_solo(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for name in ("aki-system.md", "fuyu-system.md", "haru-system.md"):
            with self.subTest(name=name):
                prompt = (root / "prompts" / name).read_text(encoding="utf-8")
                self.assertIn("Solo mode is the exception", prompt)
                self.assertIn("does not need to @mention you", prompt)

    def test_legacy_dynamic_lock_block_is_removed(self) -> None:
        legacy = f"BASE\n\n<{EXECUTION_LOCK_TAG}>old</{EXECUTION_LOCK_TAG}>\n"
        cleared = clear_execution_lock_policy(legacy)
        self.assertNotIn(f"<{EXECUTION_LOCK_TAG}", cleared)
        self.assertIn("BASE", cleared)


class ExecutionLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "execution.lock"
        self.status = self.root / "execution.lock.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_acquire_status_and_release(self) -> None:
        lock = ExecutionLock(self.path, self.status)
        acquired, holder = lock.acquire(
            profile="aki", agent_id="aki-agent", turn_id="turn-1"
        )
        self.assertTrue(acquired)
        self.assertEqual(holder["profile"], "aki")
        self.assertTrue(self.status.is_file())
        status = lock.status()
        self.assertTrue(status["held"])
        self.assertEqual(status["holder"]["turn_id"], "turn-1")
        self.assertTrue(lock.release(profile="aki", turn_id="turn-1"))
        self.assertFalse(lock.status()["held"])
        self.assertFalse(self.status.exists())

    def test_second_holder_fails_immediately_with_owner(self) -> None:
        first = ExecutionLock(self.path, self.status)
        second = ExecutionLock(self.path, self.status)
        acquired, _ = first.acquire(
            profile="aki", agent_id="aki-agent", turn_id="turn-1"
        )
        self.assertTrue(acquired)
        acquired, holder = second.acquire(
            profile="fuyu", agent_id="fuyu-agent", turn_id="turn-2"
        )
        self.assertFalse(acquired)
        self.assertEqual(holder["profile"], "aki")
        self.assertEqual(holder["turn_id"], "turn-1")
        first.release(profile="aki", turn_id="turn-1")

    def test_cross_process_exclusion(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_lock,
            args=(str(self.path), str(self.status), ready, release),
        )
        process.start()
        try:
            self.assertTrue(ready.wait(5.0))
            contender = ExecutionLock(self.path, self.status)
            acquired, holder = contender.acquire(
                profile="aki", agent_id="aki-agent", turn_id="turn-1"
            )
            self.assertFalse(acquired)
            self.assertEqual(holder["profile"], "fuyu")
        finally:
            release.set()
            process.join(5.0)
            if process.is_alive():
                process.terminate()


if __name__ == "__main__":
    unittest.main()
