from __future__ import annotations

import json
import unittest

from engine.akid.tool_detail import (
    TOOL_DETAIL_MAX_CHARS,
    render_tool_result,
    render_tool_value,
    tool_detail_body,
)


class ToolDetailTests(unittest.TestCase):
    def test_objects_are_indented_and_strings_stay_raw(self) -> None:
        rendered = render_tool_value({"path": "MEMORY.md"})
        self.assertIn('"path": "MEMORY.md"', rendered)
        self.assertEqual(render_tool_value("echo hi"), "echo hi")
        self.assertEqual(render_tool_value(None), "")

    def test_result_includes_distinct_stdout_and_stderr(self) -> None:
        rendered = render_tool_result(
            {
                "tool_return": "tool output",
                "stdout": "tool output",
                "stderr": "boom",
            }
        )
        self.assertEqual(rendered, "tool output\n--- stderr ---\nboom")

    def test_long_values_are_truncated(self) -> None:
        rendered = render_tool_value("x" * (TOOL_DETAIL_MAX_CHARS + 50))
        self.assertEqual(len(rendered), TOOL_DETAIL_MAX_CHARS)
        self.assertTrue(rendered.endswith("\n… (truncated)"))

    def test_running_body_omits_result_and_finished_body_keeps_it(self) -> None:
        running = json.loads(tool_detail_body(arguments='{\n  "command": "echo"\n}', result=None))
        self.assertEqual(set(running), {"arguments"})
        finished = json.loads(
            tool_detail_body(arguments=running["arguments"], result="tool output")
        )
        self.assertEqual(finished["result"], "tool output")
        self.assertIn("echo", finished["arguments"])


if __name__ == "__main__":
    unittest.main()
