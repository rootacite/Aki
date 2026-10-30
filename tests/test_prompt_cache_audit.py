from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prompt_cache_audit.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("prompt_cache_audit", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load prompt_cache_audit")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromptCacheAuditTests(unittest.TestCase):
    def test_reports_prefix_overlap_for_stable_system(self) -> None:
        audit = load_audit_module()
        system = (
            "<tool-ownership></tool-ownership>\n"
            "<channel-policy></channel-policy>\n"
            "<channel-wake-routing></channel-wake-routing>\n"
            "<channel-identity></channel-identity>\n"
            "<channel-context></channel-context>\n"
            "# Format Guard"
        )
        payloads = [
            (
                f"CACHE-TEST-run-aki-{index}",
                {
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"user-{index}"},
                        {"role": "system", "content": f"<turn-control>{index}</turn-control>"},
                    ]
                },
            )
            for index in range(1, 4)
        ]
        report = audit._report_profile("aki", payloads)
        self.assertEqual(report["profile"], "aki")
        self.assertEqual(len(report["pairs"]), 3)
        self.assertGreater(report["median_lcp_tokens"], 0)

    def test_rejects_dynamic_block_in_system_prompt(self) -> None:
        audit = load_audit_module()
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "<tool-ownership></tool-ownership>"
                        "<channel-policy></channel-policy>"
                        "<channel-wake-routing></channel-wake-routing>"
                        "<channel-identity></channel-identity>"
                        "<channel-context></channel-context>"
                        "# Format Guard"
                        "<turn-control></turn-control>"
                    ),
                }
            ]
        }
        with self.assertRaises(RuntimeError):
            audit._report_profile("aki", [("CACHE-TEST-run-aki-1", payload)] * 3)

    def test_normalizes_responses_input_into_system_and_mid_messages(self) -> None:
        audit = load_audit_module()
        content = (
            "<tool-ownership></tool-ownership>"
            "<channel-policy></channel-policy>"
            "<channel-wake-routing></channel-wake-routing>"
            "<channel-identity></channel-identity>"
            "<channel-context></channel-context>"
            "# Format Guard"
            "<turn-control></turn-control>"
        )
        payload = {
            "input": [
                {"role": "developer", "content": content},
                {"role": "user", "content": "hello"},
            ]
        }
        normalized = audit._normalize_debug_payload(payload)
        self.assertEqual(normalized["messages"][0]["role"], "system")
        self.assertIn("# Format Guard", normalized["messages"][0]["content"])
        self.assertNotIn("<turn-control", normalized["messages"][0]["content"])
        self.assertIn("<turn-control", normalized["messages"][1]["content"])

    def test_accepts_turn_control_in_current_user_prefix(self) -> None:
        audit = load_audit_module()
        system = (
            "<tool-ownership></tool-ownership>"
            "<channel-policy></channel-policy>"
            "<channel-wake-routing></channel-wake-routing>"
            "<channel-identity></channel-identity>"
            "<channel-context></channel-context>"
            "# Format Guard"
        )
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "<turn-control>dynamic</turn-control>\n\nhello",
                },
            ]
        }
        report = audit._report_profile("aki", [("marker", payload)] * 3)
        self.assertEqual(report["profile"], "aki")
        self.assertTrue(report["system_stable"])


if __name__ == "__main__":
    unittest.main()
