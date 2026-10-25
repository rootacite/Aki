from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PERSONAS = {
    "Aki": ROOT / "userdata" / "persona.md",
    "Fuyu": ROOT / "userdata2" / "persona.md",
    "Haru": ROOT / "userdata3" / "persona.md",
}


def body_first_line(text: str) -> str:
    if text.startswith("---\n"):
        _, _, body = text[4:].partition("\n---\n")
    else:
        body = text
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


class PersonaPovTests(unittest.TestCase):
    def test_persona_templates_use_second_person_identity_anchor(self) -> None:
        for name, path in PERSONAS.items():
            with self.subTest(name=name):
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"name: {name}", text)
                self.assertTrue(body_first_line(text).startswith(f"你是 {name}"))
                self.assertNotIn(f"我是 {name}", body_first_line(text))


if __name__ == "__main__":
    unittest.main()
