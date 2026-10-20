from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.aki_config import atomic_write, load_config, set_key, template_text
from engine.aki_config import ModelRoute
from engine.letta_llm import prepare_letta, supported_reasoning_efforts


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


class LettaProviderMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.conf = self.root / "aki.conf"
        atomic_write(self.conf, template_text())
        set_key(self.conf, "proxy", "token", "loopback-token")
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
                        "id": name,
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
        for name in ("one.json", "two.json"):
            agent = json.loads((self.backend / "agents" / name).read_text())
            self.assertEqual(agent["model"], "aki/deepseek-flash")
            self.assertEqual(agent["model_settings"]["provider_type"], "aki")
            self.assertEqual(agent["model_settings"]["reasoning_effort"], "high")
        self.assertTrue((self.letta_home / "mods" / "aki-proxy.ts").is_file())
        backups = list((self.letta_home / "backups").glob("llm-proxy-*/auth.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("deepseek-secret", backups[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
