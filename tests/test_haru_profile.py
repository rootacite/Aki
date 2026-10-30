from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.aki_config import (
    ConfigError,
    ModelRoute,
    atomic_write,
    load_config,
    set_key,
    template_text,
)
from engine.akid import GatewayConfig, build_letta_command
from engine.letta_llm import _compat, render_write_policy_mod

ROOT = Path(__file__).resolve().parent.parent
USERDATA = ROOT / "userdata3"
REQUIRED_FILES = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)


class HaruConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "haru.conf"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_template_has_haru_identity_and_loopback_proxy(self) -> None:
        atomic_write(self.path, template_text("agent-haru", profile="haru"))
        set_key(self.path, "proxy", "token", "loopback-token")
        config = load_config(self.path, profile="haru")
        self.assertEqual(config.agent_id, "agent-haru")
        self.assertEqual(config.provider_id, "aki-haru")
        self.assertEqual(config.username, "哥哥")
        self.assertEqual(config.userdata, "userdata3")
        self.assertEqual(config.channel.name, "Haru")
        self.assertEqual(config.channel.id, "haru")
        self.assertEqual(config.channel_wait_for, ("Aki", "Fuyu"))
        self.assertIn("小春", config.channel.aliases)
        self.assertEqual((config.proxy_host, config.proxy_port), ("127.0.0.1", 7723))
        self.assertFalse(config.mcp_native_tools)
        self.assertTrue(config.camera_enabled)
        self.assertTrue(config.screenshot_enabled)
        self.assertTrue(config.live_enabled)
        self.assertIsNotNone(config.image)
        assert config.image is not None
        self.assertTrue(config.image.enabled)
        self.assertEqual(config.image.model, "image-01")
        self.assertEqual(config.image.path, "/image_generation")
        self.assertEqual(config.image.output_dir, "/tmp/aki/images")
        self.assertEqual(config.image.base_url, "https://api.minimaxi.com/v1")
        self.assertTrue(config.image.require_approval)
        route = next(
            model for model in config.models if model.model_id == "gemini-3.8-flash"
        )
        self.assertEqual(route.upstream_model, "gemini-3.8-flash")
        self.assertEqual(route.path, "/chat/completions")
        self.assertEqual(route.context_window, 1_000_000)
        self.assertEqual(route.max_tokens, 65536)
        self.assertEqual(route.inputs, ("text", "image"))
        self.assertEqual(
            route.extra_body,
            {"reasoning_effort": "low", "temperature": 0.88, "top_p": 0.92},
        )
        self.assertEqual(config.enabled_models, ())

    def test_tool_policy_is_parsed_and_defaults_for_other_profiles(self) -> None:
        atomic_write(self.path, template_text("agent-haru", profile="haru"))
        config = load_config(self.path, profile="haru")
        self.assertTrue(config.tool_policy.explicit)
        self.assertEqual(config.tool_policy.toolset, "letta")
        self.assertIn("Write", config.tool_policy.allow)
        self.assertIn("Grep", config.tool_policy.allow)
        self.assertIn("Glob", config.tool_policy.allow)
        self.assertIn("LS", config.tool_policy.allow)
        self.assertNotIn("ListDir", config.tool_policy.allow)
        self.assertIn("acquire_execution_lock", config.tool_policy.allow)
        self.assertIn("wait_other", config.tool_policy.allow)
        self.assertIn("wait_user", config.tool_policy.allow)
        self.assertIn("search_chat_history", config.tool_policy.allow)
        self.assertIn("desktop_take_screenshot", config.tool_policy.allow)
        self.assertIn("camera_take_photo", config.tool_policy.allow)
        self.assertIn("tokifactor_send_image", config.tool_policy.allow)
        self.assertIn("tokifactor_send_file", config.tool_policy.allow)
        self.assertIn("generate_image", config.tool_policy.allow)
        self.assertNotIn("Bash", config.tool_policy.allow)
        self.assertIn("Task", config.tool_policy.deny)
        self.assertEqual(config.tool_policy.write_roots, ("/tmp", "~/.local/share/tmp"))

        aki_path = self.root / "aki.conf"
        atomic_write(aki_path, template_text())
        fuyu_path = self.root / "fuyu.conf"
        atomic_write(fuyu_path, template_text(profile="fuyu"))
        self.assertFalse(load_config(aki_path).tool_policy.explicit)
        self.assertFalse(load_config(fuyu_path).tool_policy.explicit)

    def test_extra_body_cannot_override_routing_fields(self) -> None:
        atomic_write(self.path, template_text("agent-haru", profile="haru"))
        text = self.path.read_text(encoding="utf-8").replace(
            'extra_body = {"reasoning_effort": "low", "temperature": 0.88, "top_p": 0.92}',
            'extra_body = {"model": "evil"}',
        )
        atomic_write(self.path, text)
        with self.assertRaises(ConfigError):
            load_config(self.path, profile="haru")
        text = text.replace('"model": "evil"', '"messages": []')
        atomic_write(self.path, text)
        with self.assertRaises(ConfigError):
            load_config(self.path, profile="haru")

    def test_minimax_compat_disables_unsupported_chat_fields(self) -> None:
        route = ModelRoute(
            model_id="minimax",
            upstream_name="minimax",
            upstream_model="MiniMax-M3.1-Flash",
            api="openai-completions",
            path="/chat/completions",
            context_window=204800,
            max_tokens=65536,
            inputs=("text",),
            url="https://api.minimaxi.com/v1/chat/completions",
            api_key="secret",
        )
        self.assertEqual(
            _compat(route),
            {
                "supportsStore": False,
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
                "maxTokensField": "max_tokens",
                "supportsStrictMode": False,
            },
        )

    def test_backend_command_restricts_haru_only(self) -> None:
        atomic_write(self.path, template_text("agent-haru", profile="haru"))
        haru = load_config(self.path, profile="haru")
        command = build_letta_command(GatewayConfig(socket_path=Path("/tmp/x"), tool_policy=haru.tool_policy))
        self.assertIn("--toolset", command)
        self.assertEqual(command[command.index("--toolset") + 1], "letta")
        self.assertIn("--tools", command)
        allowed = command[command.index("--tools") + 1].split(",")
        self.assertIn("Write", allowed)
        self.assertIn("Grep", allowed)
        self.assertIn("Glob", allowed)
        self.assertIn("LS", allowed)
        self.assertNotIn("ListDir", allowed)
        self.assertIn("wait_other", allowed)
        self.assertIn("search_chat_history", allowed)
        self.assertNotIn("Bash", allowed)
        self.assertIn("--disallowedTools", command)
        self.assertIn("--no-system-info-reminder", command)
        denied = command[command.index("--disallowedTools") + 1].split(",")
        self.assertIn("Bash", denied)
        self.assertIn("Task", denied)

        aki_path = self.root / "aki.conf"
        atomic_write(aki_path, template_text())
        aki = load_config(aki_path)
        base = build_letta_command(GatewayConfig(socket_path=Path("/tmp/y"), tool_policy=aki.tool_policy))
        self.assertNotIn("--tools", base)
        self.assertNotIn("--toolset", base)
        self.assertNotIn("--disallowedTools", base)
        self.assertIn("--no-system-info-reminder", base)
        self.assertEqual(
            base[base.index("--reflection-trigger") + 1],
            "step-count",
        )
        self.assertEqual(
            base[base.index("--reflection-step-count") + 1],
            "25",
        )


class HaruPersonaTests(unittest.TestCase):
    def test_haru_has_complete_persona_templates(self) -> None:
        for rel in REQUIRED_FILES:
            path = USERDATA / rel
            self.assertTrue(path.is_file(), rel)
            self.assertTrue(path.read_text(encoding="utf-8").strip(), rel)

    def test_persona_renders_username_and_has_limits(self) -> None:
        persona = (USERDATA / "persona.md").read_text(encoding="utf-8")
        self.assertIn("{{USERNAME}}", persona)
        self.assertIn("Haru", persona)
        self.assertIn("鬼画符", persona)
        self.assertIn("画笔", persona)
        self.assertIn("麦克风", persona)
        self.assertNotIn("我是 Aki", persona)
        rendered = persona.replace("{{USERNAME}}", "哥哥")
        self.assertNotIn("{{USERNAME}}", rendered)
        self.assertIn("哥哥", rendered)

    def test_boundaries_only_cover_identity_capability_and_privacy(self) -> None:
        boundaries = (USERDATA / "character" / "boundaries.md").read_text(encoding="utf-8")
        self.assertIn("不冒充", boundaries)
        self.assertIn("不伪造", boundaries)
        self.assertIn("不泄露", boundaries)
        self.assertNotIn("年龄", boundaries)
        self.assertNotIn("性向", boundaries)

    def test_system_prompt_has_one_core_memory_and_explicit_limits(self) -> None:
        prompt = (ROOT / "prompts" / "haru-system.md").read_text(encoding="utf-8")
        self.assertEqual(prompt.count("{CORE_MEMORY}"), 1)
        self.assertNotIn("You are a Letta Code agent", prompt)
        for text in (
            "There is no Bash",
            "There are no subagents",
            "Text-to-image is available through the `generate_image` tool",
            "`generate_image`, `tokifactor_send_image`, and `tokifactor_send_file` do not require the device-wide execution lock",
            "Do not use `Read`",
            "/tmp",
            "~/.local/share/tmp",
            "[NO_REPLY]",
            "@Name",
            "哥哥",
        ):
            self.assertIn(text, prompt)

    def test_model_facing_text_has_no_fixed_label(self) -> None:
        model_facing = [
            *(USERDATA / rel for rel in REQUIRED_FILES),
            ROOT / "prompts" / "haru-system.md",
        ]
        for path in model_facing:
            with self.subTest(path=path.name):
                self.assertNotIn("小恶魔", path.read_text(encoding="utf-8"))
        template = template_text("agent-haru", profile="haru")
        self.assertNotIn("小恶魔", template)


@unittest.skipUnless(shutil.which("node"), "node is required to execute the permission mod")
class HaruWritePolicyModTests(unittest.TestCase):
    def setUp(self) -> None:
        cache = Path.home() / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tempdir = tempfile.TemporaryDirectory(dir=cache)
        self.root = Path(self.tempdir.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.mod = self.root / "aki-write-policy.mjs"
        self.mod.write_text(
            render_write_policy_mod(
                type(
                    "Policy",
                    (),
                    {"write_roots": ("/tmp", "~/.local/share/tmp")},
                )(),
                profile="haru",
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def check(self, cases: list[dict[str, object]]) -> list[object]:
        script = r"""
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[1]).href);
let check;
mod.default({
  capabilities: { permissions: true },
  permissions: { register: (definition) => { check = definition.check; } },
});
const cases = JSON.parse(process.argv[2]);
const results = cases.map((value) => check(value) ?? null);
console.log(JSON.stringify(results));
"""
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["AKI_PROFILE"] = "haru"
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(self.mod), json.dumps(cases)],
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout)

    def test_temporary_roots_allow_and_repository_home_deny(self) -> None:
        cases = [
            {"toolName": "Read", "args": {"file_path": "/tmp/haru-read.txt"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Read", "args": {"file_path": "~/.letta/lc-local-backend/memfs/agent/memory/persona.md"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Read", "args": {"file_path": str(self.repo / "engine" / "akid.py")}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Read", "args": {"file_path": "engine/akid.py"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Read", "args": {"file_path": "~/.aki/haru.conf"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Grep", "args": {"pattern": str(self.repo)}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Write", "args": {"file_path": "/tmp/haru-ok.txt"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Write", "args": {"file_path": "~/.local/share/tmp/ok.txt"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Write", "args": {"file_path": str(self.repo / "bad.txt")}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Write", "args": {"file_path": "../home/bad.txt"}, "workingDirectory": self.repo.as_posix(), "phase": "approval"},
            {"toolName": "Edit", "args": {"path": "/tmp/../home/bad.txt"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "ApplyPatch", "args": {"input": "*** Begin Patch\n*** Update File: /tmp/patched.txt\n*** End Patch"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "ApplyPatch", "args": {"input": "*** Begin Patch\n*** Update File: repo.txt\n*** End Patch"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Bash", "args": {"command": "true"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Task", "args": {"description": "x"}, "workingDirectory": str(self.repo), "phase": "approval"},
        ]
        results = self.check(cases)
        for index in (0, 1):
            self.assertEqual(results[index]["decision"], "allow", index)
        for index in (2, 3, 4, 5):
            self.assertEqual(results[index]["decision"], "deny", index)
        self.assertEqual(results[6]["decision"], "allow")
        self.assertEqual(results[7]["decision"], "allow")
        for index in (8, 9, 10, 12, 13, 14):
            self.assertEqual(results[index]["decision"], "deny", index)
        self.assertEqual(results[11]["decision"], "allow")

    def test_search_tools_use_path_or_working_directory(self) -> None:
        cases = [
            {"toolName": "Grep", "args": {"pattern": "x", "path": "/tmp"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Grep", "args": {"pattern": "x"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Glob", "args": {"pattern": "*.md", "path": "~/.letta/lc-local-backend/memfs/agent/memory"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "LS", "args": {"path": "/tmp"}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "LS", "args": {"path": str(self.repo)}, "workingDirectory": str(self.repo), "phase": "approval"},
            {"toolName": "Glob", "args": {"pattern": "*.md"}, "workingDirectory": "/tmp", "phase": "approval"},
        ]
        results = self.check(cases)
        for index in (0, 2, 3, 5):
            self.assertEqual(results[index]["decision"], "allow", index)
        for index in (1, 4):
            self.assertEqual(results[index]["decision"], "deny", index)


if __name__ == "__main__":
    unittest.main()
