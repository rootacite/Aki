from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-system-prompt.py"
spec = importlib.util.spec_from_file_location("install_system_prompt", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


AGENT_ID = "agent-test"
CONVERSATION_ID = "default"
CORE = "<persona>\nAki\n</persona>\n"
LIVE = '<live-context refreshed_at="2026-01-01T00:00:00+08:00">\n<note>desktop</note>\n</live-context>'
MEMORY = "<memory>\n# Memory\n\n- [character](character/MEMORY.md)\n</memory>"


class InstallSystemPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.backend = self.root / "backend"
        self.agents = self.backend / "agents"
        self.agents.mkdir(parents=True)
        self.agent_path = self.agents / "agent.json"
        self.conversation_dir = self.backend / "conversations" / base64.urlsafe_b64encode(
            f"{CONVERSATION_ID}:{AGENT_ID}".encode()
        ).decode().rstrip("=")
        self.conversation_dir.mkdir(parents=True)
        self.prompt = self.root / "aki-system.md"
        self.prompt.write_text("Aki base prompt.\n\n{CORE_MEMORY}\n", encoding="utf-8")
        self.settings = self.root / "settings.json"
        self.backups = self.root / "backups"
        old_base = "Old Letta Code prompt.\n\n{CORE_MEMORY}\n"
        self.agent_path.write_text(
            json.dumps({"id": AGENT_ID, "system": old_base.rstrip() + "\n\n" + LIVE + "\n"}),
            encoding="utf-8",
        )
        self.prompt_path = self.conversation_dir / "system-prompt.json"
        self.prompt_path.write_text(
            json.dumps(
                {
                    "content": old_base.replace("{CORE_MEMORY}", CORE).rstrip() + "\n\n" + LIVE + "\n",
                    "coreMemory": CORE,
                    "rawSystemHash": "old",
                    "compiledAt": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        self.settings.write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "agentId": AGENT_ID,
                            "systemPromptPreset": "letta",
                            "systemPromptHash": "old",
                            "systemPromptVersion": "1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_install(self, *, dry_run: bool = False) -> dict[str, object]:
        return installer.install(
            prompt_path=self.prompt,
            backend_dir=self.backend,
            agent_id=AGENT_ID,
            conversation_id=CONVERSATION_ID,
            settings_path=self.settings,
            backup_dir=self.backups,
            dry_run=dry_run,
        )

    def test_installs_prompt_and_preserves_memory_and_live_context(self) -> None:
        result = self.run_install()
        self.assertTrue(result["changed"])
        agent = json.loads(self.agent_path.read_text(encoding="utf-8"))
        self.assertIn("Aki base prompt.", agent["system"])
        self.assertNotIn("Old Letta Code prompt", agent["system"])
        self.assertIn(LIVE, agent["system"])
        prompt = json.loads(self.prompt_path.read_text(encoding="utf-8"))
        self.assertEqual(prompt["coreMemory"], CORE)
        self.assertIn("Aki base prompt.", prompt["content"])
        self.assertNotIn("{CORE_MEMORY}", prompt["content"])
        self.assertIn(LIVE, prompt["content"])
        self.assertEqual(
            prompt["rawSystemHash"],
            hashlib.sha256(agent["system"].encode("utf-8")).hexdigest(),
        )
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        record = settings["agents"][0]
        self.assertEqual(record["systemPromptPreset"], "custom")
        self.assertNotIn("systemPromptHash", record)
        self.assertNotIn("systemPromptVersion", record)
        backup = Path(str(result["backup_path"]))
        self.assertTrue((backup / "agent.json").is_file())
        self.assertTrue((backup / "system-prompt.json").is_file())

    def test_second_install_is_idempotent(self) -> None:
        self.assertTrue(self.run_install()["changed"])
        second = self.run_install()
        self.assertFalse(second["changed"])
        self.assertFalse(second["agent_changed"])
        self.assertFalse(second["conversation_changed"])

    def test_preserves_root_memory_block(self) -> None:
        record = json.loads(self.prompt_path.read_text(encoding="utf-8"))
        record["content"] = record["content"].replace(
            "</persona>", "</persona>\n\n" + MEMORY, 1
        )
        self.prompt_path.write_text(json.dumps(record), encoding="utf-8")
        self.run_install()
        prompt = json.loads(self.prompt_path.read_text(encoding="utf-8"))
        self.assertEqual(prompt["content"].count(MEMORY), 1)
        self.assertLess(prompt["content"].index("</persona>"), prompt["content"].index(MEMORY))

    def test_dry_run_does_not_write(self) -> None:
        before_agent = self.agent_path.read_text(encoding="utf-8")
        before_prompt = self.prompt_path.read_text(encoding="utf-8")
        result = self.run_install(dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.agent_path.read_text(encoding="utf-8"), before_agent)
        self.assertEqual(self.prompt_path.read_text(encoding="utf-8"), before_prompt)
        self.assertFalse(self.backups.exists())

    def test_status_reports_sync(self) -> None:
        self.assertFalse(installer.status(self.prompt, self.backend, AGENT_ID)["in_sync"])
        self.run_install()
        self.assertTrue(installer.status(self.prompt, self.backend, AGENT_ID)["in_sync"])

    def test_prompt_requires_core_placeholder(self) -> None:
        self.prompt.write_text("no placeholder\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.run_install()


if __name__ == "__main__":
    unittest.main()
