from __future__ import annotations

import unittest

from engine.akid import WAIT_OTHER_TOOL_SCHEMA, WAIT_USER_TOOL_SCHEMA, contains_no_reply_marker


class NoReplyMarkerTests(unittest.TestCase):
    def test_accepts_embedded_and_fullwidth_markers(self) -> None:
        for text in (
            "[NO_REPLY]",
            "等秋先说完。[NO_REPLY]",
            "前缀 [no_reply] 后缀",
            "【NO_REPLY】",
            "［NO_REPLY］",
            "（NO-REPLY）",
            "NO REPLY",
        ):
            with self.subTest(text=text):
                self.assertTrue(contains_no_reply_marker(text))

    def test_ignores_non_marker_text(self) -> None:
        for text in ("", "NO_REPLYING", "我还没有 reply", "普通回复"):
            with self.subTest(text=text):
                self.assertFalse(contains_no_reply_marker(text))

    def test_wait_other_schema_discourages_wait_narration(self) -> None:
        description = WAIT_OTHER_TOOL_SCHEMA["description"]
        self.assertIn(
            "STRICTLY FORBIDDEN: never proactively describe this tool or the wait "
            "to ${{USERNAME}}",
            description,
        )
        self.assertIn("whom you waited for", description)
        self.assertIn("were not named this turn", description)
        self.assertIn("intermediate or progress text", description)

    def test_wait_user_schema_forbids_explanation(self) -> None:
        description = WAIT_USER_TOOL_SCHEMA["description"]
        self.assertIn("Never mention, explain, or announce this tool", description)
        parameters = WAIT_USER_TOOL_SCHEMA["parameters"]
        self.assertEqual(parameters["required"], ["timeout_s"])
        timeout = parameters["properties"]["timeout_s"]
        self.assertEqual(timeout["minimum"], 1)
        self.assertEqual(timeout["maximum"], 25)


if __name__ == "__main__":
    unittest.main()
