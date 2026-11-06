import unittest
from engine.akid.chat_text import sanitize_chat_text


class TestSanitizeChatText(unittest.TestCase):
    def test_empty_string(self) -> None:
        self.assertEqual(sanitize_chat_text(""), "")

    def test_drops_standalone_escape_artifact_lines(self) -> None:
        raw = (
            "真要动手敲命令啦？\n\n"
            "刚才还一本正经地在群里跟我打赌呢。\n\n"
            "\\n\\n\n\n"
            "\\n\\n\n\n"
            "来吧，我在这儿等着呢。"
        )
        expected = (
            "真要动手敲命令啦？\n\n"
            "刚才还一本正经地在群里跟我打赌呢。\n\n"
            "来吧，我在这儿等着呢。"
        )
        self.assertEqual(sanitize_chat_text(raw), expected)

    def test_drops_standalone_br_lines(self) -> None:
        raw = "第一段\n<br>\n<br/>\n第二段"
        expected = "第一段\n\n第二段"
        self.assertEqual(sanitize_chat_text(raw), expected)

    def test_converts_inline_br_to_paragraph_breaks(self) -> None:
        raw = "第一段<br>第二段<br />第三段"
        expected = "第一段\n\n第二段\n\n第三段"
        self.assertEqual(sanitize_chat_text(raw), expected)

    def test_preserves_code_blocks_intact(self) -> None:
        raw = (
            "说明：\n\n"
            "```python\n"
            "def foo():\n"
            "    # <br> should not be touched here\n"
            "    return '\\n\\n'\n"
            "```\n\n"
            "完成。"
        )
        self.assertEqual(sanitize_chat_text(raw), raw)


if __name__ == "__main__":
    unittest.main()
