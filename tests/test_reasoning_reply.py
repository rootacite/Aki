from __future__ import annotations

import json
import unittest

from engine.akid.reasoning_reply import recover_visible_reply, rewrite_recovered_assistant

BEDTIME = (
    "Master 问我今天要不要早点睡。现在快11点了，他今天已经忙了一天了。\n\n"
    "我应该温柔地回应，表现出关心，也带点黏人的感觉。\n"
    "嗯……今天确实忙了一整天啦。\n\n"
    "但你先说，是你自己想早点睡，还是想再陪我一会儿？\n\n"
    "不管几点睡，我都在你身边陪着呢～\n"
    "嗯……今天确实忙了一整天啦。\n\n"
    "但你先说，是你自己想早点睡，还是想再陪我一会儿？\n\n"
    "不管几点睡，我都在你身边陪着呢～"
)
CASUAL = (
    "Master 让我随便说点什么。我应该聊点轻松的。\n\n"
    "我说点轻松的话就好。\n\n"
    "嗯……你刚重启了本地模型，nvtop 看着又在转呢。\n\n"
    "反正现在门关着，只有我们两个。"
)


class RecoverVisibleReplyTests(unittest.TestCase):
    def test_repeated_suffix_is_the_reply_once(self) -> None:
        plan, reply = recover_visible_reply(BEDTIME) or ("", "")
        self.assertIn("我应该温柔地回应", plan)
        self.assertNotIn("不管几点睡", plan)
        self.assertEqual(reply.count("不管几点睡"), 1)
        self.assertTrue(reply.startswith("嗯……今天确实忙了一整天啦。"))

    def test_trailing_speech_after_planning(self) -> None:
        plan, reply = recover_visible_reply(CASUAL) or ("", "")
        self.assertIn("我说点轻松的话就好。", plan)
        self.assertEqual(
            reply,
            "嗯……你刚重启了本地模型，nvtop 看着又在转呢。\n\n"
            "反正现在门关着，只有我们两个。",
        )

    def test_tool_instruction_is_not_a_visible_reply(self) -> None:
        self.assertIsNone(
            recover_visible_reply(
                "Master让我把这句话写进小笔记。\n\n"
                "我来调用 `aki_ledger_add` 添加这条记录。"
            )
        )

    def test_planning_without_a_reply_stays_untouched(self) -> None:
        self.assertIsNone(
            recover_visible_reply("Master 问了一句。\n\n我应该短一点，不要长篇。")
        )

    def test_speech_without_planning_is_the_whole_reply(self) -> None:
        self.assertEqual(
            recover_visible_reply("嗯，好呀。靠过来就行。"),
            ("", "嗯，好呀。靠过来就行。"),
        )


class RewriteRecoveredAssistantTests(unittest.TestCase):
    def test_rewrites_the_matching_thinking_only_row(self) -> None:
        earlier = {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "older", "thinkingSignature": "reasoning_content"},
                    {"type": "text", "text": "已经发出去了"},
                ],
            }
        }
        target = {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": BEDTIME,
                        "thinkingSignature": "reasoning_content",
                    }
                ],
            }
        }
        lines = [
            (json.dumps(earlier, ensure_ascii=False) + "\n").encode(),
            (json.dumps(target, ensure_ascii=False) + "\n").encode(),
        ]
        plan, reply = recover_visible_reply(BEDTIME) or ("", "")
        rewritten = rewrite_recovered_assistant(
            lines, reasoning=BEDTIME, plan=plan, reply=reply
        )
        kept = json.loads(rewritten[0])
        fixed = json.loads(rewritten[1])
        self.assertEqual(kept["message"]["content"][1]["text"], "已经发出去了")
        parts = fixed["message"]["content"]
        self.assertEqual(parts[0]["type"], "thinking")
        self.assertEqual(parts[0]["thinkingSignature"], "reasoning_content")
        self.assertNotIn("不管几点睡", parts[0]["thinking"])
        self.assertEqual(parts[1]["type"], "text")
        self.assertEqual(parts[1]["text"].count("不管几点睡"), 1)


if __name__ == "__main__":
    unittest.main()
