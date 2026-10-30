from __future__ import annotations

import unittest

from engine.letta_llm import render_execution_lock_mod, render_tool_ownership_mod
from engine.tool_ownership import (
    EXCLUSIVE_TOOL_NAMES,
    apply_tool_ownership,
    authorize_exclusive_tool,
    clear_tool_ownership,
    ownership_denied_message,
    render_tool_ownership_prompt,
    tools_for_profile,
)


class ToolOwnershipTests(unittest.TestCase):
    def test_exclusive_tools_are_profile_scoped(self) -> None:
        aki_names = {tool["name"] for tool in tools_for_profile("aki")}
        haru_names = {tool["name"] for tool in tools_for_profile("haru")}
        self.assertEqual(haru_names, {"generate_image"})
        self.assertNotIn("generate_image", aki_names)
        self.assertTrue(aki_names)
        self.assertEqual(tools_for_profile("fuyu"), [])

    def test_authorization_reports_owner(self) -> None:
        allowed, owner = authorize_exclusive_tool("aki", "aki_schedule_create")
        self.assertTrue(allowed)
        self.assertEqual(owner, "Aki")
        allowed, owner = authorize_exclusive_tool("fuyu", "aki_schedule_create")
        self.assertFalse(allowed)
        self.assertEqual(owner, "Aki")
        message = ownership_denied_message("aki_schedule_create")
        self.assertIn("TOOL_OWNERSHIP_DENIED", message)
        self.assertIn("Aki", message)
        self.assertIn("@Aki", message)
        allowed, owner = authorize_exclusive_tool("haru", "generate_image")
        self.assertTrue(allowed)
        self.assertEqual(owner, "Haru")
        allowed, owner = authorize_exclusive_tool("aki", "generate_image")
        self.assertFalse(allowed)
        self.assertEqual(owner, "Haru")
        image_message = ownership_denied_message("generate_image")
        self.assertIn("Haru", image_message)
        self.assertIn("@Haru", image_message)

    def test_non_owner_prompt_lists_hidden_tools_and_handoff(self) -> None:
        block = render_tool_ownership_prompt("fuyu")
        self.assertIn('profile="fuyu"', block)
        self.assertIn("aki_schedule_create", block)
        self.assertIn("aki_zero_reply_budget", block)
        self.assertIn("aki_ledger_add", block)
        self.assertIn("[NO_REPLY]", block)
        self.assertIn("@Aki", block)
        self.assertIn("generate_image", block)
        self.assertIn("@Haru", block)
        self.assertIn("image generation", block)
        self.assertIn("NEVER try to recreate", block)

    def test_owner_prompt_has_self_budget_guard(self) -> None:
        block = render_tool_ownership_prompt("aki")
        self.assertIn('profile="aki"', block)
        self.assertIn("NEVER use aki_zero_reply_budget on yourself", block)
        self.assertIn("Fuyu", block)

    def test_haru_owner_prompt_has_image_tool_without_lock(self) -> None:
        block = render_tool_ownership_prompt("haru")
        self.assertIn('profile="haru"', block)
        self.assertIn("generate_image", block)
        self.assertIn("You OWN these exclusive tools", block)
        self.assertIn("do not need the device-wide execution lock", block)
        self.assertIn("@Aki for schedules", block)

    def test_block_is_idempotent_and_precedes_channel_blocks(self) -> None:
        block = render_tool_ownership_prompt("fuyu")
        system = (
            "BASE mentions <tool-ownership> as policy.\n\n"
            "<channel-identity>identity</channel-identity>\n\n"
            "<live-context>live</live-context>\n"
        )
        once = apply_tool_ownership(system, block)
        twice = apply_tool_ownership(once, block)
        self.assertEqual(once, twice)
        self.assertIn("BASE mentions <tool-ownership> as policy.", twice)
        self.assertLess(once.index("<tool-ownership"), once.index("<channel-identity"))
        cleared = clear_tool_ownership(once)
        self.assertNotIn("</tool-ownership>", cleared)
        self.assertIn("BASE mentions <tool-ownership> as policy.", cleared)

    def test_generated_letta_mod_contains_all_exclusive_tools(self) -> None:
        mod = render_tool_ownership_mod()
        for name in EXCLUSIVE_TOOL_NAMES:
            self.assertIn(name, mod)
        self.assertIn("TOOL_OWNERSHIP_DENIED", mod)
        self.assertIn("entry.label", mod)

    def test_execution_lock_mod_exempts_exclusive_tools(self) -> None:
        mod = render_execution_lock_mod()
        self.assertIn("const EXCLUSIVE_TOOLS = new Set(", mod)
        self.assertIn("if (EXCLUSIVE_TOOLS.has(toolName)) return;", mod)
        for name in EXCLUSIVE_TOOL_NAMES:
            self.assertIn(name, mod)


if __name__ == "__main__":
    unittest.main()
