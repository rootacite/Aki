from __future__ import annotations

import unittest

from engine.channel.text import insert_chinese_period_breaks


class ChannelTextDeliveryTests(unittest.TestCase):
    def test_inserts_blank_lines_after_chinese_periods(self) -> None:
        self.assertEqual(
            insert_chinese_period_breaks("第一句。第二句。"),
            "第一句。\n\n第二句。",
        )

    def test_leaves_periods_before_newlines_unchanged(self) -> None:
        self.assertEqual(
            insert_chinese_period_breaks("第一句。\n第二句。\n\n第三句。"),
            "第一句。\n第二句。\n\n第三句。",
        )
        self.assertEqual(
            insert_chinese_period_breaks("第一句。\r\n第二句。"),
            "第一句。\r\n第二句。",
        )
        self.assertEqual(
            insert_chinese_period_breaks("第一句。  \n第二句。"),
            "第一句。  \n第二句。",
        )

    def test_skips_fenced_and_inline_code(self) -> None:
        text = "说明。\n\n```text\n代码。下一行。\n```\n结束。`内联。代码`继续。"
        self.assertEqual(
            insert_chinese_period_breaks(text),
            "说明。\n\n```text\n代码。下一行。\n```\n结束。\n\n`内联。代码`继续。",
        )

    def test_is_idempotent(self) -> None:
        once = insert_chinese_period_breaks("第一句。第二句。")
        self.assertEqual(insert_chinese_period_breaks(once), once)


if __name__ == "__main__":
    unittest.main()
