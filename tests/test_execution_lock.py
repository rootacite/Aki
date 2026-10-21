from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from engine.execution_lock import ExecutionLock
from engine.execution_policy import (
    EXECUTION_LOCK_TAG,
    apply_execution_lock_policy,
    clear_execution_lock_policy,
    is_read_only_tool,
    render_execution_lock_policy,
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
            "ListDir",
            "ViewImage",
            "desktop_take_screenshot",
            "camera_take_photo",
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
            "tokifactor_send_image",
            "mcp__local__nmap_scan",
            "unknown_tool",
        ):
            self.assertFalse(is_read_only_tool(name), name)
            self.assertTrue(requires_execution_lock(name), name)
        self.assertFalse(requires_execution_lock("acquire_execution_lock"))
        self.assertFalse(requires_execution_lock("release_execution_lock"))

    def test_policy_block_is_idempotent_and_explains_read_only_exemption(self) -> None:
        block = render_execution_lock_policy(
            authorized=False,
            reason="user-must-name-executor",
        )
        self.assertIn(EXECUTION_LOCK_TAG, block)
        self.assertIn("只读工具不需要锁", block)
        self.assertIn("acquire_execution_lock", block)
        self.assertIn("没有授权", block)
        once = apply_execution_lock_policy("BASE\n", block)
        twice = apply_execution_lock_policy(once, block)
        self.assertEqual(twice.count(f"<{EXECUTION_LOCK_TAG}"), 1)
        cleared = clear_execution_lock_policy(twice)
        self.assertNotIn(f"<{EXECUTION_LOCK_TAG}", cleared)
        self.assertIn("BASE", cleared)

    def test_authorized_policy_allows_lock_acquisition(self) -> None:
        block = render_execution_lock_policy(
            authorized=True,
            reason="user-mentioned",
        )
        self.assertIn('authorized="true"', block)
        self.assertIn("已获得非只读执行授权", block)


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
