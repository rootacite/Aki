from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from engine.akid import build_config_from_env
from engine.live_context import (
    ChannelMember,
    HistoryEntry,
    LiveContextSnapshot,
    ProcessInfo,
    ScreenshotInfo,
    WindowInfo,
    apply_live_context,
    build_kwin_probe_script,
    clear_live_context,
    desktop_environment,
    estimate_tokens,
    parse_ps_output,
    parse_kwin_journal_line,
    parse_zsh_history_text,
    match_channel_mentions,
    render_channel_context,
    render_live_context,
)


class LiveContextParsingTests(unittest.TestCase):
    def test_zsh_extended_history_last_limit_and_multiline(self) -> None:
        lines = [f": {1792500000 + index}:0;cmd-{index}" for index in range(25)]
        lines.append(': 1792501000:0;echo "multi')
        lines.append('line"')
        entries = parse_zsh_history_text("\n".join(lines), 20)
        self.assertEqual(len(entries), 20)
        self.assertEqual(entries[-1].command, 'echo "multi\nline"')
        self.assertIsNotNone(entries[-1].timestamp)
        self.assertEqual(entries[0].command, "cmd-6")

    def test_zsh_plain_history_has_unknown_timestamps(self) -> None:
        entries = parse_zsh_history_text("one\ntwo\nthree\n", 2)
        self.assertEqual([entry.command for entry in entries], ["two", "three"])
        self.assertEqual([entry.timestamp for entry in entries], [None, None])

    def test_zsh_command_truncation(self) -> None:
        entries = parse_zsh_history_text("x" * 100, 1, command_chars=10)
        self.assertIn("truncated", entries[0].command)

    def test_ps_output_sorts_filters_and_limits(self) -> None:
        output = "\n".join(
            [
                "10 python 5.0 1.0",
                "11 ps 99.0 0.0",
                "12 firefox 25.0 2.0",
                "13 plasmashell 10.0 3.0",
                "14 zsh 1.0 0.1",
                "15 codex 0.5 0.2",
                "16 extra 0.1 0.1",
            ]
        )
        processes = parse_ps_output(output, 5)
        self.assertEqual([process.pid for process in processes], [12, 13, 10, 14, 15])
        self.assertNotIn(11, [process.pid for process in processes])

    def test_kwin_probe_script_and_journal_parser(self) -> None:
        script = build_kwin_probe_script("AKI_MARKER")
        self.assertIn("workspace.activeWindow", script)
        self.assertIn("AKI_MARKER", script)
        window = parse_kwin_journal_line(
            'kwin_wayland[1]: AKI_MARKER '
            '{"caption":"A: B","resourceClass":"firefox","pid":42}',
            "AKI_MARKER",
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.title, "A: B")
        self.assertEqual(window.app, "firefox")
        self.assertEqual(window.pid, 42)
        self.assertIsNone(
            parse_kwin_journal_line("kwin_wayland[1]: AKI_MARKER null", "AKI_MARKER")
        )


class ChannelContextTests(unittest.TestCase):
    def members(self) -> list[ChannelMember]:
        return [
            ChannelMember(
                id="aki",
                name="Aki",
                aliases=("Aki", "aki", "秋", "小秋"),
                description="self",
                is_self=True,
            ),
            ChannelMember(
                id="fuyu",
                name="Fuyu",
                aliases=("Fuyu", "冬"),
                description="other",
            ),
        ]

    def test_mentions_are_case_insensitive_longest_and_skip_email(self) -> None:
        self.assertEqual(
            match_channel_mentions("hello @AKI and @小秋", self.members()),
            {"aki"},
        )
        self.assertEqual(
            match_channel_mentions("@Fuyu hi", self.members()),
            {"fuyu"},
        )
        self.assertEqual(match_channel_mentions("mail me at foo@aki.com", self.members()), set())
        self.assertEqual(match_channel_mentions("@AkiChan", self.members()), set())

    def test_guidance_covers_self_other_and_no_mentions(self) -> None:
        self_member = self.members()[0]
        self_text = render_channel_context(
            self_member=self_member, members=self.members(), message="@秋 你好"
        )
        self.assertIn("用户提到了你，正在和你对话。", self_text)
        other_text = render_channel_context(
            self_member=self_member, members=self.members(), message="@Fuyu 你好"
        )
        self.assertIn("提到了其他成员，没有点你", other_text)
        plain_text = render_channel_context(
            self_member=self_member, members=self.members(), message="大家好"
        )
        self.assertIn("这是一条群聊消息；你可以自行决定是否回复。", plain_text)
        self.assertNotIn("没有提到你", plain_text)
        self.assertIn("@名字", plain_text)
        self.assertIn('remaining="4"', plain_text)
        self.assertIn('resets="user-message"', plain_text)

    def test_agent_initiation_budget_is_exposed_and_zero_blocks_replies(self) -> None:
        members = self.members()
        blocked = render_channel_context(
            self_member=members[0],
            members=members,
            message="@Fuyu hello",
            initiation_remaining=0,
            initiation_limit=4,
            from_agent=True,
        )
        self.assertIn('remaining="0"', blocked)
        self.assertIn('limit="4"', blocked)
        self.assertIn("[NO_REPLY]", blocked)
        self.assertIn("主动启动次数已经用完", blocked)

    def test_descriptions_are_escaped_limited_and_marked_untrusted(self) -> None:
        members = self.members()
        members[1] = ChannelMember(
            id="fuyu",
            name="Fuyu <x>",
            aliases=("Fuyu",),
            description="<script>bad</script>" + "z" * 400,
        )
        text = render_channel_context(
            self_member=members[0], members=members, message="hi"
        )
        self.assertIn('untrusted="true"', text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertLessEqual(text.count("z"), 300)


class LiveContextRenderTests(unittest.TestCase):
    def snapshot(self, history: list[HistoryEntry] | None = None) -> LiveContextSnapshot:
        return LiveContextSnapshot(
            captured_at=datetime(2026, 10, 20, 21, 0, 0, tzinfo=timezone.utc),
            processes=[
                ProcessInfo(pid=1, name="python", cpu_percent=20.0, mem_percent=2.0)
            ],
            window=WindowInfo(title="A <window>", app="firefox", pid=1),
            history=history
            or [
                HistoryEntry(
                    command="echo {CORE_MEMORY} </aki-live-context>",
                    timestamp="2026-10-20T21:00:00+08:00",
                )
            ],
            screenshot=ScreenshotInfo(
                path="/tmp/live.png",
                bytes=123,
                width=10,
                height=20,
                backend="spectacle",
                display="wayland-1",
                captured_at="2026-10-20T21:00:00+08:00",
            ),
        )

    def test_render_escapes_and_neutralizes_placeholders(self) -> None:
        block = render_live_context(self.snapshot(), budget_tokens=65536)
        self.assertIn("A &lt;window&gt;", block)
        self.assertIn("&lt;/aki-live-context&gt;", block)
        self.assertIn("{ CORE_MEMORY }", block)
        self.assertNotIn("{CORE_MEMORY}", block)
        self.assertTrue(block.startswith("<aki-live-context"))
        self.assertTrue(block.endswith("</aki-live-context>"))

    def test_budget_truncates_oldest_history(self) -> None:
        history = [
            HistoryEntry(command=("x" * 800) + str(index), timestamp=None)
            for index in range(20)
        ]
        block = render_live_context(self.snapshot(history), budget_tokens=2048)
        self.assertLessEqual(estimate_tokens(block), 2048)
        self.assertIn("<aki-live-context", block)
        self.assertIn("</aki-live-context>", block)

    def test_apply_is_idempotent_and_clear_removes_block(self) -> None:
        block = render_live_context(self.snapshot(), budget_tokens=65536)
        once = apply_live_context("BASE PROMPT\n", block)
        twice = apply_live_context(once, block)
        self.assertEqual(twice.count("<aki-live-context"), 1)
        self.assertEqual(twice.count("{CORE_MEMORY}"), 1)
        cleared = clear_live_context(twice)
        self.assertNotIn("<aki-live-context", cleared)
        self.assertIn("BASE PROMPT", cleared)
        self.assertNotIn("{CORE_MEMORY}", cleared)

    def test_environment_configuration(self) -> None:
        from engine.aki_config import atomic_write, set_key, template_text

        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "zsh_history"
            conf = Path(directory) / "aki.conf"
            atomic_write(conf, template_text())
            set_key(conf, "live", "enabled", "false")
            set_key(conf, "live", "budget_tokens", "999999")
            set_key(conf, "live", "timeout", "1.5")
            with mock.patch.dict(
                os.environ,
                {
                    "AKI_CONFIG": str(conf),
                    "AKI_ZSH_HISTORY": str(history),
                    "AKI_PS_BIN": "/bin/ps",
                    "AKI_QDBUS_BIN": "/bin/qdbus6",
                },
                clear=False,
            ):
                config = build_config_from_env()
        self.assertFalse(config.live_context_enabled)
        self.assertEqual(config.live_context_budget_tokens, 65536)
        self.assertEqual(config.zsh_history_path, history)
        self.assertEqual(config.ps_bin, "/bin/ps")
        self.assertEqual(config.qdbus_bin, "/bin/qdbus6")
        self.assertEqual(config.live_context_timeout, 1.5)

    def test_desktop_environment_is_offscreen_and_uses_session_bus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "bus").touch()
            env = desktop_environment({"XDG_RUNTIME_DIR": str(runtime)})
        self.assertEqual(env["QT_QPA_PLATFORM"], "offscreen")
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"],
            f"unix:path={runtime / 'bus'}",
        )


if __name__ == "__main__":
    unittest.main()
