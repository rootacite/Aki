from __future__ import annotations

import unittest
from types import SimpleNamespace

from engine.model_privacy import is_local_model_route
from engine.solo_backend import SoloRoute


class ModelPrivacyTests(unittest.TestCase):
    def test_cloud_proxy_on_loopback_stays_cloud(self) -> None:
        route = SimpleNamespace(
            url="http://127.0.0.1:8317/v1/chat/completions",
            upstream_name="cli-proxy-api",
            upstream_model="gemini-3.8-flash",
            solo=False,
            local=None,
        )
        self.assertFalse(is_local_model_route(route))

    def test_local_model_file_on_loopback_is_local(self) -> None:
        route = SimpleNamespace(
            url="http://127.0.0.1:10000/v1/chat/completions",
            upstream_name="qwen-local",
            upstream_model="/opt/models/language/qwen.gguf",
            solo=False,
            local=None,
        )
        self.assertTrue(is_local_model_route(route))

    def test_managed_solo_route_is_local(self) -> None:
        route = SoloRoute(
            model_id="Qwen-Local",
            upstream_model="/opt/models/qwen.gguf",
            context_window=128000,
            max_tokens=32768,
            inputs=("text",),
            endpoint="http://127.0.0.1:10000/v1",
        )
        self.assertTrue(is_local_model_route(route))

    def test_managed_solo_route_without_file_name_is_local(self) -> None:
        route = SimpleNamespace(
            url="http://127.0.0.1:10000/v1/chat/completions",
            upstream_name="solo-local",
            upstream_model="local-model",
            solo=True,
            local=None,
        )
        self.assertTrue(is_local_model_route(route))

    def test_explicit_flag_wins(self) -> None:
        local = SimpleNamespace(
            url="https://api.example.com/v1/chat/completions",
            upstream_name="cloud",
            upstream_model="remote-model",
            solo=False,
            local=True,
        )
        cloud = SimpleNamespace(
            url="http://127.0.0.1:10000/v1/chat/completions",
            upstream_name="local",
            upstream_model="/tmp/model.gguf",
            solo=True,
            local=False,
        )
        self.assertTrue(is_local_model_route(local))
        self.assertFalse(is_local_model_route(cloud))

    def test_private_lan_model_is_local(self) -> None:
        route = SimpleNamespace(
            url="http://192.168.3.20:8080/v1/chat/completions",
            upstream_name="gpu-box",
            upstream_model="/models/uncensored.Q4_K_M.gguf",
            solo=False,
            local=None,
        )
        self.assertTrue(is_local_model_route(route))

    def test_unknown_route_is_treated_as_cloud(self) -> None:
        self.assertFalse(is_local_model_route(None))


if __name__ == "__main__":
    unittest.main()
