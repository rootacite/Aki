from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from engine.akid import _b64url


class ClientCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.memory = self.root / "memory"
        self.memory.mkdir()
        (self.memory / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
        self.socket = self.root / "akid.sock"
        self.writable_state = self.root / "writable.json"
        self.repo_cwd = self.root / "repo"
        shutil.copytree(ROOT / "userdata", self.repo_cwd / "userdata")
        (self.repo_cwd / "userdata" / "username.txt").write_text("OldName\n", encoding="utf-8")
        (self.repo_cwd / "scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "sync-persona.py", self.repo_cwd / "scripts" / "sync-persona.py")
        env = os.environ.copy()
        env["AKI_REPO_CWD"] = str(self.repo_cwd)
        env["AKI_MCP_NATIVE_STATE"] = str(self.root / "mcp-native.json")
        env["AKI_PROFILE_CONFIG"] = str(self.root / "aki.conf")
        env["AKI_USERNAME"] = "OldName"
        self.server = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests" / "serve_fake.py"),
                "--socket",
                str(self.socket),
                "--memory",
                str(self.memory),
                "--writable-state",
                str(self.writable_state),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.server.stdout is not None
        ready, _, _ = select.select([self.server.stdout], [], [], 10)
        if not ready:
            self.fail("fake gateway did not become ready")
        line = self.server.stdout.readline().strip()
        if not line.startswith("READY"):
            stderr = self.server.stderr.read() if self.server.stderr else ""
            self.fail(f"fake gateway failed to start: {line}\n{stderr}")

    def tearDown(self) -> None:
        self.server.terminate()
        try:
            self.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        if self.server.stdout is not None:
            self.server.stdout.close()
        if self.server.stderr is not None:
            self.server.stderr.close()
        self.tempdir.cleanup()

    def run_client(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AKI_SOCKET"] = str(self.socket)
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "aki"), "--socket", str(self.socket), *args],
            cwd=str(ROOT),
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def run_akictl(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AKI_SOCKET"] = str(self.socket)
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "akictl"), *args],
            cwd=str(ROOT),
            env=env,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_one_shot_streaming(self) -> None:
        result = self.run_client("-p", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("echo: hello", result.stdout)

    def test_json_ndjson_output(self) -> None:
        result = self.run_client("--json", "-p", "hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        messages = [json.loads(line) for line in lines]
        self.assertTrue(all(isinstance(message, dict) for message in messages))
        event_types = [
            message["params"]["type"]
            for message in messages
            if message.get("method") == "aki.event"
        ]
        self.assertIn("assistant_delta", event_types)
        self.assertIn("turn_end", event_types)

    def test_non_interactive_approval_denies(self) -> None:
        result = self.run_client("-p", "dangerous", stdin="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("denying tool", result.stderr)
        self.assertIn("tool denied", result.stdout)

    def test_akictl_chat_and_alias(self) -> None:
        chat = self.run_akictl("-p", "hello", "--socket", str(self.socket))
        self.assertEqual(chat.returncode, 0, chat.stderr)
        self.assertIn("echo: hello", chat.stdout)
        alias = self.run_client("-p", "hello")
        self.assertEqual(alias.returncode, 0, alias.stderr)
        self.assertIn("echo: hello", alias.stdout)

    def test_akictl_help_and_command_help(self) -> None:
        help_result = self.run_akictl("help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage: akictl", help_result.stdout)
        self.assertIn("akictl help [command]", help_result.stdout)

        model_help = self.run_akictl("help", "model")
        self.assertEqual(model_help.returncode, 0, model_help.stderr)
        self.assertIn("usage: akictl model", model_help.stdout)

        unknown = self.run_akictl("help", "missing")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown command: missing", unknown.stderr)

    def test_akictl_status_and_writable_management(self) -> None:
        allowed = Path(tempfile.mkdtemp(prefix="akid-cli-allow-", dir=str(Path.home() / ".local" / "share")))
        try:
            status = self.run_akictl("status", "--socket", str(self.socket))
            self.assertEqual(status.returncode, 0, status.stderr)
            status_data = json.loads(status.stdout)
            self.assertIn("sandbox", status_data)

            added = self.run_akictl("allow", str(allowed), "--socket", str(self.socket))
            self.assertEqual(added.returncode, 0, added.stderr)
            added_data = json.loads(added.stdout)
            self.assertTrue(added_data["added"])

            listing = self.run_akictl("allowlist", "--socket", str(self.socket))
            self.assertEqual(listing.returncode, 0, listing.stderr)
            list_data = json.loads(listing.stdout)
            self.assertIn(str(allowed.resolve()), [entry["path"] for entry in list_data["registered"]])

            removed = self.run_akictl("disallow", str(allowed), "--socket", str(self.socket))
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(json.loads(removed.stdout)["removed"])
        finally:
            shutil.rmtree(allowed, ignore_errors=True)

    def test_akictl_mcp_status_and_local_toggle(self) -> None:
        status = self.run_akictl("mcp", "status", "--socket", str(self.socket))
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertIn("local", payload["excluded_servers"])

        enabled = self.run_akictl(
            "mcp", "local", "on", "--socket", str(self.socket)
        )
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        enabled_payload = json.loads(enabled.stdout)
        self.assertTrue(enabled_payload["changed"])
        self.assertNotIn("local", enabled_payload["status"]["excluded_servers"])
        from engine.aki_config import read_mcp_excluded

        state_path = self.root / "mcp-native.json"
        self.assertEqual(read_mcp_excluded(state_path), ())

        disabled = self.run_akictl(
            "mcp", "local", "off", "--socket", str(self.socket)
        )
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        disabled_payload = json.loads(disabled.stdout)
        self.assertTrue(disabled_payload["changed"])
        self.assertIn("local", disabled_payload["status"]["excluded_servers"])
        self.assertEqual(read_mcp_excluded(state_path), ("local",))

    def test_akictl_reset_clears_default_conversation(self) -> None:
        conversation_dir = (
            self.root / "backend" / "conversations" / _b64url("default:test-agent")
        )
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "conversation.json").write_text(
            json.dumps(
                {
                    "id": "default",
                    "agent_id": "test-agent",
                    "archived": False,
                    "archived_at": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "last_message_at": "2026-01-01T00:00:00Z",
                    "summary": None,
                    "in_context_message_ids": ["m1"],
                }
            ),
            encoding="utf-8",
        )
        (conversation_dir / "messages.jsonl").write_text(
            '{"message":"old"}\n', encoding="utf-8"
        )
        result = self.run_akictl("reset", "--socket", str(self.socket))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["backend_ready"])
        self.assertEqual(data["conversation_id"], "default")
        self.assertEqual((conversation_dir / "messages.jsonl").read_bytes(), b"")
        self.assertTrue((self.root / "backups").is_dir())

    def test_akictl_username_get_and_set(self) -> None:
        (self.memory / "human.md").write_text(
            "---\nname: Human\ndescription: test human\n---\nHuman profile.\n",
            encoding="utf-8",
        )
        conversation_dir = (
            self.root / "backend" / "conversations" / _b64url("default:test-agent")
        )
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "system-prompt.json").write_text(
            json.dumps(
                {
                    "content": (
                        "<human>\n<description>old human</description>\nold\n</human>\n\n"
                        "<memory-policy>\n<description>old policy</description>\nold OldName\n</memory-policy>\n\n"
                        "<persona>\n<description>old persona</description>\nold OldName\n</persona>\n\n"
                        "<memory>\n# Memory\nold\n</memory>"
                    ),
                    "coreMemory": (
                        "<human>\n<description>old human</description>\nold\n</human>\n\n"
                        "<memory-policy>\n<description>old policy</description>\nold OldName\n</memory-policy>\n\n"
                        "<persona>\n<description>old persona</description>\nold OldName\n</persona>\n"
                    ),
                    "compiledAt": "2026-01-01T00:00:00Z",
                    "rawSystemHash": "test",
                    "memfsRevision": "old",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.memory)], check=True)
        subprocess.run(["git", "-C", str(self.memory), "config", "user.name", "test"], check=True)
        subprocess.run(
            ["git", "-C", str(self.memory), "config", "user.email", "test@example.com"],
            check=True,
        )

        before = self.run_akictl("username", "--socket", str(self.socket))
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(json.loads(before.stdout)["username"], "OldName")

        changed = self.run_akictl("set-username", "Master", "--socket", str(self.socket))
        self.assertEqual(changed.returncode, 0, changed.stderr)
        data = json.loads(changed.stdout)
        self.assertEqual(data["username"], "Master")
        self.assertTrue(data["changed"])
        self.assertTrue(data["recompiled"])
        prompt = json.loads((conversation_dir / "system-prompt.json").read_text())
        self.assertIn("Master", prompt["content"])
        self.assertNotIn("OldName", prompt["content"])

        after = self.run_akictl("username", "--socket", str(self.socket))
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(json.loads(after.stdout)["username"], "Master")
        self.assertIn("Master", (self.memory / "persona.md").read_text(encoding="utf-8"))

        recompiled = self.run_akictl("recompile", "--socket", str(self.socket))
        self.assertEqual(recompiled.returncode, 0, recompiled.stderr)
        self.assertTrue(json.loads(recompiled.stdout)["recompiled"])
        self.assertIn(
            "username = Master",
            (self.root / "aki.conf").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (self.repo_cwd / "userdata" / "username.txt").read_text(encoding="utf-8"),
            "OldName\n",
        )

    def test_akictl_mqtt_status_uses_lan_default(self) -> None:
        result = self.run_akictl("mqtt", "--socket", str(self.socket))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["host"], "192.168.3.1")
        self.assertEqual(data["port"], 1883)


if __name__ == "__main__":
    unittest.main()
