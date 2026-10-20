from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from engine.akid import build_config_from_env
from engine.live_context import (
    HistoryEntry,
    LiveContextSnapshot,
    ProcessInfo,
    ScreenshotInfo,
    WindowInfo,
    apply_live_context,
    clear_live_context,
    desktop_environment,
    estimate_tokens,
    parse_ps_output,
    parse_window_info,
    parse_zsh_history_text,
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

    def test_window_info_parser(self) -> None:
        window = parse_window_info(
            "caption: A: B\nresourceClass: firefox\npid: 42\n"
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.title, "A: B")
        self.assertEqual(window.app, "firefox")
        self.assertEqual(window.pid, 42)
        self.assertIsNone(parse_window_info("caption: \npid: 42\n"))


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
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "zsh_history"
            with mock.patch.dict(
                os.environ,
                {
                    "AKI_LIVE_CONTEXT_ENABLED": "0",
                    "AKI_LIVE_CONTEXT_BUDGET_TOKENS": "999999",
                    "AKI_ZSH_HISTORY": str(history),
                    "AKI_PS_BIN": "/bin/ps",
                    "AKI_QDBUS_BIN": "/bin/qdbus6",
                    "AKI_LIVE_CONTEXT_TIMEOUT": "1.5",
                    "AKI_MQTT_STATE": str(Path(directory) / "mqtt.json"),
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
