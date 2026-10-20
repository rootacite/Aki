from __future__ import annotations

import json
import unittest

from engine.akid import (
    HISTORY_IMAGE_PLACEHOLDER,
    HISTORY_MAX_BYTES,
    MAX_HISTORY_TEXT_CHARS,
    _compact_history_messages,
    _sanitize_history_value,
)


class HistorySanitizeTests(unittest.TestCase):
    def test_image_blocks_are_replaced(self) -> None:
        value = {
            "type": "tool_return_message",
            "tool_return": [
                {"type": "text", "text": "[Image: shot.png]"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "A" * 4096,
                    },
                },
            ],
        }
        sanitized = _sanitize_history_value(value)
        dumped = json.dumps(sanitized)
        self.assertNotIn("A" * 300, dumped)
        self.assertIn(HISTORY_IMAGE_PLACEHOLDER, dumped)

    def test_long_text_is_truncated(self) -> None:
        sanitized = _sanitize_history_value({"text": "x" * (MAX_HISTORY_TEXT_CHARS + 50)})
        self.assertLess(len(sanitized["text"]), MAX_HISTORY_TEXT_CHARS + 100)
        self.assertIn("truncated 50 chars", sanitized["text"])

    def test_batch_is_capped(self) -> None:
        messages = [
            {
                "id": f"m{index}",
                "message_type": "assistant_message",
                "content": [{"type": "text", "text": "y" * 30000}],
            }
            for index in range(60)
        ]
        compacted, dropped = _compact_history_messages(messages)
        self.assertTrue(dropped)
        self.assertGreater(len(compacted), 0)
        payload = json.dumps(compacted, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(payload), HISTORY_MAX_BYTES)

    def test_non_dict_messages_are_ignored(self) -> None:
        compacted, dropped = _compact_history_messages(["bad", None, {"id": "m1"}])
        self.assertEqual(compacted, [{"id": "m1"}])
        self.assertFalse(dropped)


if __name__ == "__main__":
    unittest.main()
