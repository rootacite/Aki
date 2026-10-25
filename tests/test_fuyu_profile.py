from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
USERDATA = ROOT / "userdata2"
REQUIRED_FILES = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)


class FuyuProfileTests(unittest.TestCase):
    def test_fuyu_has_complete_persona_templates(self) -> None:
        for rel in REQUIRED_FILES:
            path = USERDATA / rel
            self.assertTrue(path.is_file(), rel)
            self.assertTrue(path.read_text(encoding="utf-8").strip(), rel)

    def test_fuyu_uses_custom_base_prompt_not_letta_upstream(self) -> None:
        prompt = (ROOT / "prompts" / "fuyu-system.md").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("You are Fuyu"))
        self.assertEqual(prompt.count("{CORE_MEMORY}"), 1)
        for upstream in (
            "You are a Letta Code agent",
            "Letta Corporation",
            "machines that learn",
            "You are Aki",
        ):
            self.assertNotIn(upstream, prompt)

    def test_fuyu_persona_is_independent_from_aki(self) -> None:
        persona = (USERDATA / "persona.md").read_text(encoding="utf-8")
        self.assertIn("name: Fuyu", persona)
        self.assertIn("你是 Fuyu", persona)
        self.assertIn("毒舌", persona)
        self.assertIn("抬杠", persona)
        self.assertIn("阴阳怪气", persona)
        self.assertIn("{{USERNAME}}", persona)
        self.assertNotIn("我是 Fuyu", persona)
        self.assertNotIn("我是 Aki", persona)
        self.assertNotIn("小秋", persona)

    def test_fuyu_voice_uses_black_humor_not_soft_comfort(self) -> None:
        voice = (USERDATA / "character" / "voice.md").read_text(encoding="utf-8")
        self.assertIn("黑色幽默", voice)
        self.assertIn("严禁温情脉脉", voice)
        self.assertIn("严禁无脑爆粗口", voice)

    def test_boundary_only_requires_no_verbal_abuse(self) -> None:
        boundaries = (USERDATA / "character" / "boundaries.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("不能辱骂", boundaries)
        self.assertNotIn("保持尊重", boundaries)
        self.assertNotIn("明显脆弱", boundaries)


if __name__ == "__main__":
    unittest.main()
