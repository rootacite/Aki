from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.aki_config import atomic_write, load_config, set_key, template_text
from engine.aki_config import ModelRoute
from engine.letta_llm import (
    EXECUTION_LOCK_MOD_NAME,
    _compat,
    prepare_letta,
    render_execution_lock_mod,
    supported_reasoning_efforts,
)


class ReasoningEffortTests(unittest.TestCase):
    def test_openrouter_gemini_allows_low_medium_high(self) -> None:
        route = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="openrouter",
            upstream_model="google/gemini-3.8-flash",
            api="openai-completions",
            path="/chat/completions",
            context_window=1048576,
            max_tokens=65536,
            inputs=("text", "image"),
            url="https://openrouter.ai/api/v1/chat/completions",
            api_key="secret",
        )
        self.assertEqual(supported_reasoning_efforts(route), ("low", "medium", "high"))

    def test_grok_oauth_responses_route_uses_xai_reasoning_levels(self) -> None:
        route = ModelRoute(
            model_id="grok-4.6",
            upstream_name="xai-oauth",
            upstream_model="grok-4.6",
            api="openai-responses",
            path="/responses",
            context_window=500000,
            max_tokens=65536,
            inputs=("text", "image"),
            url="https://cli-chat-proxy.grok.com/v1/responses",
            api_key="",
            auth="grok-oauth",
        )
        self.assertEqual(
            supported_reasoning_efforts(route),
            ("low", "medium", "high", "xhigh"),
        )

    def test_cli_proxy_compat_avoids_unsupported_chat_fields(self) -> None:
        route = ModelRoute(
            model_id="gemini-3.8-flash",
            upstream_name="cli-proxy-api",
            upstream_model="gemini-3.8-flash",
            api="openai-completions",
            path="/chat/completions",
            context_window=1_000_000,
            max_tokens=65_536,
            inputs=("text", "image"),
            url="http://127.0.0.1:8317/v1/chat/completions",
            api_key="secret",
            use_proxy=False,
        )
        self.assertEqual(
            _compat(route),
            {
                "supportsStore": False,
                "supportsDeveloperRole": False,
                "maxTokensField": "max_tokens",
                "supportsStrictMode": False,
            },
        )


class LettaProviderMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.conf = self.root / "aki.conf"
        atomic_write(self.conf, template_text())
        set_key(self.conf, "proxy", "token", "loopback-token")
        set_key(self.conf, "gateway", "agent_id", "one")
        self.backend = self.root / "backend"
        (self.backend / "providers").mkdir(parents=True)
        (self.backend / "agents").mkdir()
        auth = {
            "version": 1,
            "providers": {
                "deepseek": {
                    "id": "local-provider-deepseek",
                    "name": "deepseek",
                    "provider_type": "deepseek",
                    "auth": {"type": "api", "key": "deepseek-secret"},
                }
            },
        }
        (self.backend / "providers" / "auth.json").write_text(
            json.dumps(auth), encoding="utf-8"
        )
        for name in ("one.json", "two.json"):
            (self.backend / "agents" / name).write_text(
                json.dumps(
                    {
                        "id": name.removesuffix(".json"),
                        "model": "deepseek/deepseek-flash",
                        "model_settings": {
                            "provider_type": "deepseek",
                            "reasoning_effort": "high",
                        },
                    }
                ),
                encoding="utf-8",
            )
        self.letta_home = self.root / "letta-home"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_moves_deepseek_key_and_retargets_agents(self) -> None:
        legacy_mod = self.letta_home / "mods" / "aki-proxy.ts"
        legacy_mod.parent.mkdir(parents=True)
        legacy_mod.write_text("legacy\n", encoding="utf-8")
        config = load_config(self.conf)
        updated = prepare_letta(
            config, backend_dir=self.backend, letta_home=self.letta_home
        )
        self.assertEqual(updated.upstreams["deepseek"].api_key, "deepseek-secret")
        text = self.conf.read_text(encoding="utf-8")
        self.assertIn("api_key = deepseek-secret", text)
        auth = json.loads((self.backend / "providers" / "auth.json").read_text())
        self.assertNotIn("deepseek", auth["providers"])
        aki = auth["providers"]["aki"]
        self.assertEqual(aki["auth"]["key"], "loopback-token")
        self.assertEqual(aki["base_url"], "http://127.0.0.1:7721/v1")
        self.assertNotIn("deepseek-secret", json.dumps(auth))
        one = json.loads((self.backend / "agents" / "one.json").read_text())
        self.assertEqual(one["model"], "aki/deepseek-flash")
        self.assertEqual(one["model_settings"]["provider_type"], "aki")
        self.assertEqual(one["model_settings"]["reasoning_effort"], "high")
        two = json.loads((self.backend / "agents" / "two.json").read_text())
        self.assertEqual(two["model"], "deepseek/deepseek-flash")
        self.assertEqual(two["model_settings"]["provider_type"], "deepseek")
        self.assertTrue((self.letta_home / "mods" / "aki-proxy-aki.ts").is_file())
        self.assertTrue(
            (self.letta_home / "mods" / EXECUTION_LOCK_MOD_NAME).is_file()
        )
        self.assertFalse(legacy_mod.exists())
        backups = list((self.letta_home / "backups").glob("llm-proxy-*/auth.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("deepseek-secret", backups[0].read_text(encoding="utf-8"))


    def test_two_profiles_keep_separate_providers_and_agents(self) -> None:
        fuyu_conf = self.root / "fuyu.conf"
        atomic_write(fuyu_conf, template_text(profile="fuyu"))
        set_key(fuyu_conf, "gateway", "agent_id", "two")
        set_key(fuyu_conf, "proxy", "token", "fuyu-token")
        set_key(fuyu_conf, "proxy", "listen", "127.0.0.1:7722")
        set_key(fuyu_conf, "upstream.deepseek", "api_key", "fuyu-secret")
        two_path = self.backend / "agents" / "two.json"
        two_data = json.loads(two_path.read_text())
        two_data["model"] = "aki/aki/deepseek-flash"
        two_path.write_text(json.dumps(two_data), encoding="utf-8")
        aki = load_config(self.conf, profile="aki")
        fuyu = load_config(fuyu_conf, profile="fuyu")
        prepare_letta(aki, backend_dir=self.backend, letta_home=self.letta_home)
        prepare_letta(fuyu, backend_dir=self.backend, letta_home=self.letta_home)
        auth = json.loads((self.backend / "providers" / "auth.json").read_text())
        self.assertIn("aki", auth["providers"])
        self.assertIn("aki-fuyu", auth["providers"])
        self.assertEqual(auth["providers"]["aki-fuyu"]["auth"]["key"], "fuyu-token")
        self.assertEqual(
            auth["providers"]["aki-fuyu"]["base_url"], "http://127.0.0.1:7722/v1"
        )
        one = json.loads((self.backend / "agents" / "one.json").read_text())
        two = json.loads((self.backend / "agents" / "two.json").read_text())
        self.assertEqual(one["model"], "aki/deepseek-flash")
        self.assertEqual(one["model_settings"]["provider_type"], "aki")
        self.assertEqual(two["model"], "aki-fuyu/deepseek-flash")
        self.assertEqual(two["model_settings"]["provider_type"], "aki-fuyu")
        self.assertTrue((self.letta_home / "mods" / "aki-proxy-aki.ts").is_file())
        self.assertTrue((self.letta_home / "mods" / "aki-proxy-fuyu.ts").is_file())

    def test_execution_lock_mod_is_generated_fail_closed(self) -> None:
        mod = render_execution_lock_mod()
        self.assertIn("letta.permissions.register", mod)
        self.assertIn("EXECUTION_LOCK_REQUIRED", mod)
        self.assertIn("mcp__tavily__tavily_search", mod)
        self.assertIn("acquire_execution_lock", mod)
        self.assertNotIn("mcp__local__", mod)
        self.assertIn("desktop_take_screenshot", mod)
        self.assertIn("camera_take_photo", mod)
        self.assertIn("generate_image", mod)
        self.assertIn(
            'const LOCK_EXEMPT = new Set(["generate_image", "tokifactor_send_file", "tokifactor_send_image"])',
            mod,
        )
        self.assertIn("LOCK_EXEMPT.has(toolName)", mod)
        self.assertIn("phase === \"approval\" && isExternalTool(toolName)", mod)
        self.assertIn("denies(event.toolName, event.phase)", mod)
        self.assertNotIn("if (LOCK_TOOLS.has(toolName)) return null", mod)
        self.assertNotIn("pidAlive(holder.pid)) return null", mod)


if __name__ == "__main__":
    unittest.main()
