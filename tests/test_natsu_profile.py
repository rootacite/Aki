from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.aki_config import atomic_write, load_config, template_text
from engine.tool_ownership import render_tool_ownership_prompt


ROOT = Path(__file__).resolve().parent.parent
USERDATA = ROOT / "userdata4"
REQUIRED_FILES = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)


class NatsuConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "natsu.conf"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_template_has_natsu_identity_and_deepseek_default(self) -> None:
        atomic_write(self.path, template_text("agent-natsu", profile="natsu"))
        config = load_config(self.path, profile="natsu")
        self.assertEqual(config.userdata, "userdata4")
        self.assertEqual(config.username, "人类")
        self.assertEqual(config.channel.name, "Natsu")
        self.assertEqual(config.channel.id, "natsu")
        self.assertIn("夏", config.channel.aliases)
        self.assertEqual(config.channel_wait_for, ())
        self.assertTrue(config.channel_mention_only)
        self.assertEqual(config.proxy_port, 7724)
        self.assertTrue(config.mcp_native_tools)
        self.assertEqual(config.mcp_excluded, ())
        self.assertFalse(config.tool_policy.explicit)
        models = {route.model_id: route for route in config.models}
        self.assertIn("deepseek-flash", models)
        self.assertIn("grok-4.6", models)
        deepseek = models["deepseek-flash"]
        self.assertEqual(deepseek.upstream_name, "deepseek")
        self.assertEqual(deepseek.api, "openai-completions")
        self.assertEqual(deepseek.context_window, 1_000_000)
        self.assertEqual(deepseek.max_tokens, 384_000)
        grok = models["grok-4.6"]
        self.assertEqual(grok.auth, "grok-oauth")

    def test_natsu_owns_security_domain_and_hands_off_other_work(self) -> None:
        block = render_tool_ownership_prompt("natsu")
        self.assertIn('profile="natsu"', block)
        self.assertIn("binary reverse engineering", block)
        self.assertIn("security incident response", block)
        self.assertIn("@Fuyu", block)
        self.assertIn("@Aki", block)
        self.assertIn("@Haru", block)


class NatsuTemplateTests(unittest.TestCase):
    def test_natsu_has_complete_persona_templates(self) -> None:
        for rel in REQUIRED_FILES:
            path = USERDATA / rel
            self.assertTrue(path.is_file(), rel)
            self.assertTrue(path.read_text(encoding="utf-8").strip(), rel)

    def test_natsu_persona_is_independent_and_security_focused(self) -> None:
        persona = (USERDATA / "persona.md").read_text(encoding="utf-8")
        self.assertIn("name: Natsu", persona)
        self.assertIn("你是 Natsu", persona)
        self.assertIn("红队", persona)
        self.assertIn("逆向", persona)
        self.assertIn("eBPF", persona)
        self.assertIn("{{USERNAME}}", persona)
        self.assertNotIn("我是 Natsu", persona)
        self.assertNotIn("我是 Aki", persona)

    def test_natsu_voice_and_boundaries_match_security_role(self) -> None:
        voice = (USERDATA / "character" / "voice.md").read_text(encoding="utf-8")
        self.assertIn("冷冽", voice)
        self.assertIn("软萌语气词", voice)
        boundaries = (USERDATA / "character" / "boundaries.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("只处理本地、沙箱内或", boundaries)
        self.assertIn("不攻击外部系统", boundaries)

    def test_natsu_base_prompt_is_custom_and_has_solo_waiver(self) -> None:
        prompt = (ROOT / "prompts" / "natsu-system.md").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith("You are Natsu"))
        self.assertEqual(prompt.count("{CORE_MEMORY}"), 1)
        self.assertIn("Solo mode is the exception", prompt)
        self.assertIn("does not need to @mention you", prompt)
        self.assertIn("No authorization, no action", prompt)
        self.assertIn("Local MCP recon tools", prompt)

    def test_local_mcp_source_is_vendored(self) -> None:
        vendored = ROOT / "vendor" / "mcps"
        self.assertTrue((vendored / "src" / "mcps" / "server.py").is_file())
        self.assertTrue((vendored / "config" / "recon.yaml").is_file())
        self.assertTrue((vendored / "uv.lock").is_file())
        self.assertTrue((ROOT / "scripts" / "install-local-mcp.py").is_file())


@unittest.skipUnless(shutil.which("git"), "git is required for MemFS bootstrap")
class NatsuBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "userdata4"
        shutil.copytree(USERDATA, self.source)
        (self.source / "identity.json").unlink(missing_ok=True)
        self.backend = self.home / ".letta" / "lc-local-backend"
        self.config = self.home / ".aki" / "natsu.conf"
        self.settings = self.home / ".letta" / "settings.json"
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.home),
                "LETTA_HOME": str(self.home / ".letta"),
                "LETTA_LOCAL_BACKEND_DIR": str(self.backend),
                "XDG_RUNTIME_DIR": str(self.root / "run"),
            }
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_bootstrap(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootstrap-member.py"),
                "--profile",
                "natsu",
                "--config",
                str(self.config),
                "--source",
                str(self.source),
                "--json",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_bootstrap_uses_natsu_defaults_and_is_idempotent(self) -> None:
        created = self.run_bootstrap()
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["display_name"], "Natsu")
        self.assertEqual(created["username"], "人类")
        self.assertEqual(created["identity"]["name"], "Natsu")
        self.assertTrue(Path(created["memory_dir"], "persona.md").is_file())
        agent = json.loads(Path(created["agent_file"]).read_text(encoding="utf-8"))
        self.assertEqual(agent["model"], "aki-natsu/deepseek-flash")
        self.assertEqual(agent["model_settings"]["provider_type"], "aki-natsu")
        self.assertEqual(agent["tags"][-1], "profile:natsu")
        self.assertTrue(agent["system"].startswith("You are Natsu"))
        config = load_config(self.config, profile="natsu")
        self.assertEqual(config.userdata, "userdata4")
        self.assertEqual(config.channel.name, "Natsu")
        self.assertEqual(config.proxy_port, 7724)

        config_text = self.config.read_text(encoding="utf-8")
        second = self.run_bootstrap()
        self.assertEqual(second["status"], "exists")
        self.assertFalse(second["changed"])
        self.assertEqual(self.config.read_text(encoding="utf-8"), config_text)


if __name__ == "__main__":
    unittest.main()
