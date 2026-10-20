from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.aki_config import (
    ConfigError,
    atomic_write,
    ensure_config,
    load_config,
    parse_upstream_proxy,
    set_key,
    template_text,
)
from engine.letta_llm import render_provider_mod


class AkiConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "aki.conf"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_empty_api_key_disables_model(self) -> None:
        atomic_write(self.path, template_text())
        set_key(self.path, "proxy", "token", "loopback-token")
        config = load_config(self.path)
        self.assertEqual(config.enabled_models, ())
        self.assertEqual(config.upstream_proxy, "http://192.168.4.1:7890")
        self.assertIn("deepseek-flash", {model.model_id for model in config.models})

    def test_upstream_proxy_can_be_cleared(self) -> None:
        self.assertEqual(parse_upstream_proxy(""), "")
        self.assertEqual(parse_upstream_proxy("http://192.168.4.1:7890"), "http://192.168.4.1:7890")
        with self.assertRaises(ConfigError):
            parse_upstream_proxy("socks5://127.0.0.1:1080")

    def test_listen_must_be_loopback(self) -> None:
        atomic_write(self.path, template_text())
        set_key(self.path, "proxy", "listen", "0.0.0.0:7721")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_added_model_appears_in_routes_and_mod(self) -> None:
        atomic_write(self.path, template_text())
        set_key(self.path, "proxy", "token", "loopback-token")
        set_key(self.path, "upstream.deepseek", "api_key", "deepseek-secret")
        text = self.path.read_text(encoding="utf-8")
        text += (
            "\n[model.deepseek-reasoner]\n"
            "upstream = deepseek\n"
            "upstream_model = deepseek-reasoner\n"
            "api = openai-completions\n"
            "path = /chat/completions\n"
            "context_window = 1000\n"
            "max_tokens = 100\n"
            "input = text\n"
        )
        atomic_write(self.path, text)
        config = load_config(self.path)
        ids = [model.model_id for model in config.enabled_models]
        self.assertIn("deepseek-flash", ids)
        self.assertIn("deepseek-reasoner", ids)
        mod = render_provider_mod(config)
        self.assertIn("deepseek-reasoner", mod)
        self.assertIn("thinkingFormat", mod)
        self.assertNotIn("grok-4.7", mod)
        self.assertNotIn("gemini-3.8-flash", mod)
        set_key(self.path, "upstream.openrouter", "api_key", "openrouter-secret")
        routed = load_config(self.path)
        gemini = next(model for model in routed.enabled_models if model.model_id == "gemini-3.8-flash")
        self.assertEqual(gemini.upstream_model, "google/gemini-3.8-flash")
        self.assertEqual(gemini.url, "https://openrouter.ai/api/v1/chat/completions")
        gemini_mod = render_provider_mod(routed)
        self.assertIn('"thinkingFormat": "openrouter"', gemini_mod)
        self.assertIn("gemini-3.8-flash", gemini_mod)

    def test_local_upstream_can_bypass_global_proxy(self) -> None:
        atomic_write(self.path, template_text())
        set_key(self.path, "proxy", "token", "loopback-token")
        text = self.path.read_text(encoding="utf-8")
        text += (
            "\n[upstream.local-llama]\n"
            "base_url = http://127.0.0.1:10001/v1\n"
            "api_key = local\n"
            "use_proxy = false\n"
            "\n[model.gemma-local]\n"
            "upstream = local-llama\n"
            "upstream_model = gemma.gguf\n"
            "api = openai-completions\n"
            "path = /chat/completions\n"
            "context_window = 256000\n"
            "max_tokens = 32768\n"
            "input = text\n"
        )
        atomic_write(self.path, text)
        config = load_config(self.path)
        local = next(model for model in config.enabled_models if model.model_id == "gemma-local")
        self.assertEqual(local.context_window, 256_000)
        self.assertFalse(local.use_proxy)
        self.assertEqual(local.url, "http://127.0.0.1:10001/v1/chat/completions")
        self.assertIn('"maxTokensField": "max_tokens"', render_provider_mod(config))

    def test_legacy_files_move_and_line_edit_keeps_secrets(self) -> None:
        legacy = self.root / "share"
        legacy.mkdir()
        (legacy / "mqtt.json").write_text(
            json.dumps({"host": "10.1.1.1", "port": 1884}), encoding="utf-8"
        )
        (legacy / "writable-dirs.json").write_text(
            json.dumps({"revision": 3, "roots": [{"path": "/tmp/projects"}]}),
            encoding="utf-8",
        )
        (legacy / "mcp-native.json").write_text(
            json.dumps({"excluded": ["local", "demo"]}), encoding="utf-8"
        )
        config = ensure_config(self.path, migrate_from=legacy)
        self.assertEqual(config.mqtt_host, "10.1.1.1")
        self.assertEqual(config.mqtt_port, 1884)
        self.assertEqual(config.writable_dirs, ("/tmp/projects",))
        self.assertEqual(config.writable_revision, 3)
        self.assertEqual(config.mcp_excluded, ("local", "demo"))
        self.assertTrue(config.proxy_token)
        self.assertFalse((legacy / "mqtt.json").exists())
        self.assertTrue((self.root / "migrated" / "mqtt.json").exists())

        set_key(self.path, "upstream.deepseek", "api_key", "deepseek-secret")
        before = self.path.read_text(encoding="utf-8")
        self.assertIn("# 多行，一行一个目录。akictl allow / disallow 只改这一键。", before)
        set_key(self.path, "writable", "dirs", "/tmp/one\n/tmp/two")
        after = self.path.read_text(encoding="utf-8")
        self.assertIn("api_key = deepseek-secret", after)
        self.assertIn("# 多行，一行一个目录。akictl allow / disallow 只改这一键。", after)
        reloaded = load_config(self.path)
        self.assertEqual(reloaded.writable_dirs, ("/tmp/one", "/tmp/two"))
        self.assertEqual(reloaded.upstreams["deepseek"].api_key, "deepseek-secret")


if __name__ == "__main__":
    unittest.main()
