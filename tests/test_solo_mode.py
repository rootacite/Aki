from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from engine import akictl
from engine.akid import GatewayConfig, GatewayServer, NullConnection, Turn
from engine.aki_config import atomic_write, load_config, set_key, template_text
from engine.live_context import (
    NSFW_SOLO_PLACEHOLDER,
    ChannelMember,
    ChannelTranscriptEntry,
    apply_channel_transcript,
    apply_solo_mode,
    apply_solo_transcript,
    render_channel_transcript,
    render_solo_format_guard,
    render_turn_control,
    render_solo_mode,
    render_solo_transcript,
    strip_channel_mentions,
)
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_START,
    SoloSession,
    derive_solo_magic,
    load_solo_scene,
)
from engine.solo_backend import SoloRoute
from engine.tokifactor import TextEnvelope


class DummyBackend:
    ready = True

    def __init__(self) -> None:
        self.switched: list[str] = []
        self.reloaded: list[str] = []
        self.switch_error: str | None = None

    def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def wait_ready(self, timeout: float) -> bool:
        return True

    async def switch_conversation(self, conversation_id: str) -> dict[str, object]:
        if self.switch_error:
            raise RuntimeError(self.switch_error)
        self.switched.append(conversation_id)
        return {"ok": True, "conversation_id": conversation_id}

    async def reload_conversation(self, **kwargs) -> dict[str, object]:
        conversation_id = str(kwargs.get("conversation_id") or "")
        self.reloaded.append(conversation_id)
        return {"ok": True, "conversation_id": conversation_id}


def backend_factory(_on_message, _on_ready, _on_exit):
    return DummyBackend()


class FakeChannel:
    def __init__(self) -> None:
        self.connected = True
        self.magic = "group-magic"
        self.controls: list[dict[str, str]] = []
        self.published: list[tuple[str, dict[str, str]]] = []

    def set_active_magic(self, magic: str) -> None:
        self.magic = magic

    def discard_inbox(self) -> int:
        return 0

    async def publish_control(self, _text, metadata, **kwargs) -> None:
        self.controls.append(dict(metadata))

    async def publish_text(
        self,
        text: str,
        metadata: dict[str, str] | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        self.published.append((text, dict(metadata or {})))

    async def publish_file(self, path, *, name="", source_path="", message_id=""):
        return {
            "transfer_id": message_id or "solo-file-transfer",
            "bytes": path.stat().st_size,
            "chunks": 1,
            "name": name or path.name,
            "path": source_path or str(path),
            "topic": "tokifactor/file",
            "message_id": message_id or "solo-file-transfer",
        }


class SoloModeTests(unittest.TestCase):
    def test_magic_is_stable_and_member_specific(self) -> None:
        self.assertEqual(
            derive_solo_magic("group", "legacy_abc"),
            "e002defc2c9cb6a7f1f38c5c1120a82fde05a411f803832f9ae99bbfa82bd773",
        )
        self.assertNotEqual(
            derive_solo_magic("group", "legacy_abc"),
            derive_solo_magic("group", "legacy_def"),
        )
        self.assertNotEqual(
            derive_solo_magic("group", "legacy_abc"),
            derive_solo_magic("other-group", "legacy_abc"),
        )

    def test_scene_prompts_are_profile_specific(self) -> None:
        self.assertIn("群聊静音", load_solo_scene("aki", username="Master"))
        self.assertIn("封闭控制台", load_solo_scene("fuyu", username="Master"))
        self.assertIn("秘密画室", load_solo_scene("haru", username="哥哥"))
        self.assertIn("深夜暗房", load_solo_scene("natsu", username="Master"))

    def test_solo_channel_transcript_stays_within_16k_tokens(self) -> None:
        from engine.live_context import (
            SOLO_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
            estimate_tokens,
            render_channel_transcript,
        )

        entries = [
            ChannelTranscriptEntry(
                seq=index,
                sender="Master",
                sender_id="master",
                text="群聊里的一长段话。" * 40,
            )
            for index in range(1, 81)
        ]
        rendered = render_channel_transcript(
            entries,
            budget_tokens=SOLO_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
            frozen=True,
        )
        self.assertLessEqual(
            estimate_tokens(rendered), SOLO_CHANNEL_TRANSCRIPT_BUDGET_TOKENS
        )
        self.assertIn("seq=80):", rendered)
        self.assertNotIn("seq=1):", rendered)

    def test_latest_solo_memory_is_marked_just_ended(self) -> None:
        from engine.live_context import latest_solo_session_id, render_solo_memories
        from engine.solo_memory import SoloMemoryEntry, SoloMemorySession

        older = SoloMemorySession(
            session_id="old",
            started_at="2026-10-27T00:00:00Z",
            ended_at="2026-10-27T00:10:00Z",
            summary="更早的一次",
            entries=[
                SoloMemoryEntry(
                    id="e1",
                    seq=1,
                    role="user",
                    sender="Master",
                    timestamp="2026-10-27T00:01:00Z",
                    text="旧内容",
                    tokens=2,
                )
            ],
        )
        newer = SoloMemorySession(
            session_id="new",
            started_at="2026-10-31T12:36:00Z",
            ended_at="2026-10-31T12:43:00Z",
            summary="刚才的一次",
            entries=[
                SoloMemoryEntry(
                    id="e2",
                    seq=1,
                    role="user",
                    sender="Master",
                    timestamp="2026-10-31T12:36:00Z",
                    text="刚才的秘密",
                    tokens=2,
                )
            ],
        )
        rendered = render_solo_memories(
            [older, newer],
            just_ended_session_id=latest_solo_session_id([older, newer]),
        )
        self.assertEqual(latest_solo_session_id([older, newer]), "new")
        self.assertEqual(rendered.count('just_ended="true"'), 1)
        self.assertIn("刚才的秘密", rendered)
        self.assertLess(rendered.index("旧内容"), rendered.index("刚才的秘密"))

    def test_group_and_solo_transcripts_are_separate_blocks(self) -> None:
        group = [ChannelTranscriptEntry(1, "Master", "master", "群聊旧消息")]
        solo = [ChannelTranscriptEntry(1, "Master", "master", "独处私密消息")]
        system = apply_channel_transcript(
            "BASE\n",
            render_channel_transcript(group, frozen=True),
        )
        system = apply_solo_mode(
            system,
            render_solo_mode(
                member_name="Aki",
                user_name="Master",
                session_id="solo-test",
                scene=load_solo_scene("aki", username="Master"),
            ),
        )
        system = apply_solo_transcript(
            system,
            render_solo_transcript(solo),
        )
        self.assertIn('# channel-transcript', system)
        self.assertIn("frozen when solo mode began", system)
        self.assertIn("群聊旧消息", system)
        self.assertIn("# solo-mode", system)
        self.assertIn("当下你正在与Master独处", system)
        self.assertIn("不要写 @成员", system)
        self.assertIn("# solo-transcript", system)
        self.assertIn("独处私密消息", system)
        self.assertLess(system.index("群聊旧消息"), system.index("独处私密消息"))
        self.assertNotIn("<aki-channel-transcript", system)

    def test_solo_prompt_authorizes_execution_lock_without_mention(self) -> None:
        members = [ChannelMember(id="fuyu", name="Fuyu", aliases=("Fuyu",))]
        block = render_solo_mode(
            member_name="Fuyu",
            user_name="Master",
            session_id="solo-lock",
            scene="封闭控制台。",
        )
        self.assertIn("不需要Master @ 指派", block)
        self.assertIn("直接获取整机执行锁", block)
        turn = render_turn_control(
            self_member=members[0],
            members=members,
            message="继续",
            user_name="Master",
            solo=True,
        )
        self.assertIn("may call `acquire_execution_lock` directly", turn)
        self.assertIn("without needing an @mention from Master", turn)

    def test_old_framework_tags_are_migrated(self) -> None:
        old = (
            "<aki-channel-transcript>old</aki-channel-transcript>\n"
            "<aki-channel-policy>old</aki-channel-policy>\n"
            "<aki-live-context>old</aki-live-context>\n"
        )
        new_block = render_channel_transcript(
            [ChannelTranscriptEntry(1, "Master", "master", "hello")]
        )
        migrated = apply_channel_transcript(old, new_block)
        self.assertNotIn("<aki-channel-transcript", migrated)
        self.assertIn("# channel-transcript", migrated)

    def test_strip_mentions_removes_only_known_member_aliases(self) -> None:
        members = [
            ChannelMember(id="aki", name="Aki", aliases=("Aki", "秋")),
            ChannelMember(id="fuyu", name="Fuyu", aliases=("Fuyu",)),
        ]
        self.assertEqual(
            strip_channel_mentions("@秋 你好 @Fuyu，今天见", members),
            "你好 ，今天见",
        )
        self.assertEqual(
            strip_channel_mentions("邮箱 foo@aki.com", members),
            "邮箱 foo@aki.com",
        )

    def test_akictl_solo_parser(self) -> None:
        parser = akictl.build_parsers()["solo"]
        args = parser.parse_args(["aki", "--json", "--timeout", "3"])
        self.assertEqual(args.solo_command, "aki")
        self.assertTrue(args.json)
        self.assertEqual(args.timeout, 3.0)

    def test_akictl_solo_status_uses_latest_retained_event(self) -> None:
        start = {"kind": SOLO_START, "soloId": "solo-1"}
        active = {"kind": SOLO_ACTIVE, "soloId": "solo-1"}
        ended = {"kind": SOLO_END, "soloId": "solo-1"}

        self.assertEqual(akictl._latest_solo_event([start]), start)
        self.assertEqual(akictl._latest_solo_event([start, active]), active)
        self.assertEqual(akictl._latest_solo_event([active, ended]), ended)
        self.assertEqual(akictl._latest_solo_event([ended, start]), start)
        self.assertIsNone(akictl._latest_solo_event([]))


class SoloGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def overlay_text(self, server: GatewayServer) -> str:
        turn = Turn(
            turn_id="turn-overlay-test",
            conn=NullConnection(),
            message="probe",
            include_reasoning=False,
            client_message_id=None,
            origin="mqtt",
        )
        stable, mid = await server._build_turn_overlay(turn)
        return "\n\n".join([*stable, *mid])

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = GatewayConfig(
            socket_path=self.root / "akid.sock",
            profile="aki",
            agent_id="agent-aki",
            conversation_id="default",
            cwd=Path(__file__).resolve().parent.parent,
            backend_dir=self.root / "backend",
            mqtt_enabled=True,
            mqtt_host="127.0.0.1",
            mqtt_port=1883,
            mqtt_topic="tokifactor/text",
            mqtt_magic="group-magic",
            mqtt_sender_name="Aki",
            mqtt_sender_id="legacy_aki",
            channel_id="legacy_aki",
            channel_member_id="legacy_aki",
            channel_name="Aki",
            channel_aliases=("Aki", "aki"),
            channel_history_dir=self.root / "history",
            channel_media_dir=self.root / "media",
            solo_state=self.root / "solo.json",
            solo_memory_path=self.root / "solo-memory.json",
            live_context_enabled=False,
            screenshot_enabled=False,
            camera_enabled=False,
            schedule_enabled=False,
            presence_enabled=False,
        )
        (self.config.backend_dir / "agents").mkdir(parents=True)
        self.agent_path = self.config.backend_dir / "agents" / "agent.json"
        self.agent_path.write_text(
            json.dumps(
                {
                    "id": "agent-aki",
                    "system": "BASE\n",
                }
            ),
            encoding="utf-8",
        )
        conversation_key = base64.urlsafe_b64encode(
            b"default:agent-aki"
        ).decode().rstrip("=")
        self.conversation_dir = (
            self.config.backend_dir / "conversations" / conversation_key
        )
        self.conversation_dir.mkdir(parents=True)
        self.conversation_prompt_path = self.conversation_dir / "system-prompt.json"
        self.conversation_prompt_path.write_text(
            json.dumps(
                {
                    "content": "BASE\n"
                }
            ),
            encoding="utf-8",
        )
        self.server = GatewayServer(self.config, backend_factory=backend_factory)
        self.channel = FakeChannel()
        self.server.channel = self.channel

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_start_keeps_group_history_and_applies_solo_prompt(self) -> None:
        self.server.solo_channel_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.server.solo_channel_history_path.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "sender": "Master",
                    "sender_id": "master",
                    "text": "solo old",
                    "from_agent": False,
                    "timestamp": "2026-10-25T00:00:01Z",
                    "sent_at": 2,
                    "phase": "final",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.server._channel_history = [
            ChannelTranscriptEntry(1, "Master", "master", "group old")
        ]
        self.server._channel_history_seq = 1
        self.server._persist_channel_history()
        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo start",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_start",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-1",
                    "soloTargetId": "legacy_aki",
                    "soloTargetName": "Aki",
                    "soloStartedAt": "2026-10-25T00:00:00Z",
                },
            )
        )
        self.assertIsNotNone(self.server._solo_session)
        self.assertEqual(
            self.channel.magic,
            derive_solo_magic("group-magic", "legacy_aki"),
        )
        self.assertTrue(
            str(self.server.channel_history_path).endswith("aki.solo.jsonl")
        )
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-mode", system)
        self.assertNotIn("<channel-transcript", system)
        self.assertTrue(any(item.get("kind") == "solo_ready" for item in self.channel.controls))
        overlay = await self.overlay_text(self.server)
        self.assertIn("# solo-mode", overlay)
        self.assertIn("当下你正在与Master独处", overlay)
        self.assertIn("group old", overlay)
        self.assertIn("frozen when solo mode began", overlay)
        self.assertIn("<solo-policy", overlay)
        self.assertIn("#[name:desc]", overlay)
        self.assertIn("#[/absolute/path/to/image]", overlay)
        self.assertIn("`~[text]`", overlay)
        self.assertIn("# wait-user", overlay)
        self.assertNotIn("# solo-transcript", overlay)
        self.assertNotIn("<channel-context>", overlay)
        self.assertNotIn("<channel-wake-routing", overlay)
        self.assertNotIn("<channel-policy", overlay)
        self.assertNotIn("# turn-control", overlay)
        self.assertNotIn("# format-guard", overlay)
        self.assertIn("# solo-format-guard", overlay)
        self.assertIn("SHORT SENTENCES BY DEFAULT (短句为主)", overlay)
        self.assertLess(
            overlay.index("# solo-mode"), overlay.index("# channel-transcript")
        )
        self.assertLess(
            overlay.index("# channel-transcript"),
            overlay.index("# solo-format-guard"),
        )
        self.assertLess(
            overlay.index("# solo-format-guard"), overlay.index("# wait-user")
        )
        self.assertEqual(self.server.backend.switched, ["solo-agent-aki"])

        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo end",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_end",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-1",
                    "soloTargetId": "legacy_aki",
                },
            )
        )
        self.assertIsNone(self.server._solo_session)
        self.assertEqual(self.channel.magic, "group-magic")
        self.assertTrue(str(self.server.channel_history_path).endswith("aki.jsonl"))
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-mode", system)
        self.assertNotIn("<channel-transcript", system)
        conversation_prompt = json.loads(
            self.conversation_prompt_path.read_text(encoding="utf-8")
        )["content"]
        self.assertNotIn("<solo-mode", conversation_prompt)
        self.assertNotIn("<solo-transcript", conversation_prompt)
        self.assertNotIn("<channel-transcript", conversation_prompt)

    async def test_non_target_tracks_solo_target_without_switching(self) -> None:
        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo start",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_start",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-2",
                    "soloTargetId": "legacy_fuyu",
                    "soloTargetName": "Fuyu",
                    "soloStartedAt": "2026-10-25T00:00:00Z",
                },
            )
        )
        self.assertIsNone(self.server._solo_session)
        self.assertEqual(self.channel.magic, "group-magic")
        self.assertIn("legacy_fuyu", self.server._solo_targets)

    async def test_solo_target_cannot_mention_other_members(self) -> None:
        self.server._solo_session = self.server._solo_session  # keep type visible
        from engine.solo_mode import SoloSession

        self.server._solo_session = SoloSession(
            session_id="solo-3",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-25T00:00:00Z",
            group_magic="group-magic",
        )
        self.assertEqual(self.server._channel_message_mentions("@Fuyu 你好"), set())

    async def test_restart_during_solo_restores_group_history(self) -> None:
        from engine.solo_mode import SoloSession, save_solo_session

        group_path = self.server.group_channel_history_path
        group_path.parent.mkdir(parents=True, exist_ok=True)
        group_path.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "sender": "Master",
                    "sender_id": "master",
                    "text": "must survive",
                    "from_agent": False,
                    "timestamp": "2026-10-25T00:00:00Z",
                    "sent_at": 1,
                    "phase": "final",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        solo_path = self.server.solo_channel_history_path
        solo_path.write_text("", encoding="utf-8")
        save_solo_session(
            self.config.solo_state,
            SoloSession(
                session_id="solo-restart",
                target_id="legacy_aki",
                target_name="Aki",
                profile="aki",
                started_at="2026-10-25T00:00:00Z",
                group_magic="group-magic",
            ),
        )
        restarted = GatewayServer(self.config, backend_factory=backend_factory)
        restarted.channel = FakeChannel()
        self.assertEqual(len(restarted._frozen_group_history), 1)
        before = group_path.read_bytes()
        await restarted._deactivate_solo(session_id="solo-restart")
        self.assertGreater(group_path.stat().st_size, 1)
        self.assertEqual(group_path.read_bytes(), before)
        rows = group_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(rows), 1)
        self.assertIn("must survive", rows[0])
        self.assertEqual(len(restarted._channel_history), 1)

    async def test_empty_group_snapshot_cannot_overwrite_nonempty_file(self) -> None:
        path = self.server.group_channel_history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"seq":1}\n', encoding="utf-8")
        self.server._channel_history = []
        self.server.channel_history_path = path
        self.server._persist_channel_history()
        self.assertIn('"seq":1', path.read_text(encoding="utf-8"))

    async def test_solo_file_tool_records_sender_and_path_in_solo_history(self) -> None:
        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo start",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_start",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-file-1",
                    "soloTargetId": "legacy_aki",
                    "soloTargetName": "Aki",
                    "soloStartedAt": "2026-10-27T10:00:00Z",
                },
            )
        )
        self.assertIsNotNone(self.server._solo_session)
        file_path = self.root / "solo-notes.txt"
        file_path.write_text("private notes", encoding="utf-8")
        await self.server._execute_tokifactor_send_file({"path": str(file_path)})
        solo_rows = self.server.solo_channel_history_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertTrue(any(str(file_path) in row for row in solo_rows))
        group_path = self.server.group_channel_history_path
        if group_path.is_file():
            self.assertNotIn(
                str(file_path), group_path.read_text(encoding="utf-8")
            )
        self.assertIsNone(self.server.active)
        self.assertFalse(self.server.queue)

    async def test_solo_session_is_summarized_and_kept_out_of_group_memory(self) -> None:
        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo start",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_start",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-memory-1",
                    "soloTargetId": "legacy_aki",
                    "soloTargetName": "Aki",
                    "soloStartedAt": "2026-10-27T10:00:00Z",
                },
            )
        )
        self.assertIsNotNone(self.server._solo_session)
        self.server._record_channel_message(
            sender="Master",
            sender_id="master",
            text="这是独处里说的秘密",
            from_agent=False,
        )
        self.server._record_channel_message(
            sender="Aki",
            sender_id="legacy_aki",
            text="独处里的私密回复",
            from_agent=True,
        )
        active_prompt = await self.overlay_text(self.server)
        # The in-progress session belongs to <solo-transcript>, not the
        # historical <solo-memories> block.
        self.assertNotIn("# solo-memories", active_prompt)

        await self.server._on_channel_control(
            TextEnvelope(
                sender="Master",
                text="solo end",
                sender_id="master",
                metadata={
                    "schema": "akid.v1",
                    "kind": "solo_end",
                    "protocolVersion": "1",
                    "memberKind": "user",
                    "soloId": "solo-memory-1",
                    "soloTargetId": "legacy_aki",
                },
            )
        )
        self.assertIsNone(self.server._solo_session)
        self.assertEqual(len(self.server.solo_memory.sessions), 1)
        session = self.server.solo_memory.sessions[0]
        self.assertEqual(session.session_id, "solo-memory-1")
        self.assertEqual(len(session.entries), 2)
        self.assertEqual(session.summary_status, "fallback")
        self.assertTrue(session.nsfw)
        self.assertEqual(session.nsfw_status, "error")
        self.assertTrue(session.summary)

        memories = dict(
            json.loads(
                (self.root / "solo-memory.json").read_text(encoding="utf-8")
            )
        )
        self.assertEqual(len(memories["sessions"]), 1)
        self.assertTrue(memories["sessions"][0]["nsfw"])
        self.assertEqual(memories["sessions"][0]["nsfw_status"], "error")
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-memories", system)
        self.assertNotIn("<solo-mode", system)
        overlay = await self.overlay_text(self.server)
        self.assertIn("# solo-memories", overlay)
        self.assertIn('just_ended="true"', overlay)
        self.assertIn("刚刚结束", overlay)
        self.assertIn(NSFW_SOLO_PLACEHOLDER, overlay)
        self.assertNotIn("这是独处里说的秘密", overlay)
        self.assertNotIn("独处里的私密回复", overlay)

        self.server._solo_session = SoloSession(
            session_id="solo-next",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-28T10:00:00Z",
            group_magic="group-magic",
        )
        solo_overlay = await self.overlay_text(self.server)
        self.server._solo_session = None
        self.assertIn("这是独处里说的秘密", solo_overlay)
        self.assertIn("独处里的私密回复", solo_overlay)
        group_path = self.server.group_channel_history_path
        if group_path.is_file():
            group_text = group_path.read_text(encoding="utf-8")
            self.assertNotIn("这是独处里说的秘密", group_text)
            self.assertNotIn("独处里的私密回复", group_text)

    async def test_proactive_turn_queues_in_active_solo(self) -> None:
        from engine.solo_mode import SoloSession

        self.server._solo_session = SoloSession(
            session_id="solo-proactive",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-31T00:00:00Z",
            group_magic="group-magic",
        )
        self.server.backend.ready = False
        self.server._queue_proactive("schedule", {"schedules": [{"id": "later"}]})
        await self.server._maybe_enqueue_proactive_turn()
        self.assertEqual(len(self.server.queue), 1)
        self.assertIsNone(self.server.active)
        self.assertEqual(self.server._proactive_pending, {})
        turn = self.server.queue[0]
        self.assertEqual(turn.origin, "proactive")
        self.assertEqual(turn.trigger_kind, "schedule")
        stable, mid = await self.server._build_turn_overlay(turn)
        overlay = "\n\n".join([*stable, *mid])
        self.assertIn("# solo-mode", overlay)
        self.assertIn('<wait-user allowed="false"', overlay)
        self.assertNotIn("# channel-wake-routing", overlay)

    async def test_solo_session_is_not_visible_until_summary_and_nsfw_finish(
        self,
    ) -> None:
        from engine.solo_mode import SoloSession

        summary_started = asyncio.Event()
        classification_started = asyncio.Event()
        release_classification = asyncio.Event()

        async def fake_summarize(session, **_kwargs):
            summary_started.set()
            session.summary = "secret summary"
            session.summary_status = "ready"
            return session

        async def fake_classify(session, **_kwargs):
            classification_started.set()
            await release_classification.wait()
            session.nsfw = True
            session.nsfw_status = "ready"
            return session

        self.server._solo_memory_llm_config = lambda: {
            "base_prompt": "BASE",
            "proxy_base_url": "http://127.0.0.1:1/v1",
            "proxy_token": "token",
            "model": "model",
            "api": "openai-completions",
            "route_path": "/chat/completions",
        }
        self.server._solo_nsfw_llm_config = lambda: {
            "proxy_base_url": "http://127.0.0.1:1/v1",
            "proxy_token": "token",
            "model": "model",
            "api": "openai-completions",
            "route_path": "/chat/completions",
        }
        self.server.solo_memory.summarize = fake_summarize
        self.server.solo_memory.classify_nsfw = fake_classify
        history = [
            ChannelTranscriptEntry(
                seq=1,
                sender="Master",
                sender_id="master",
                text="secret user body",
            ),
            ChannelTranscriptEntry(
                seq=2,
                sender="Aki",
                sender_id="legacy_aki",
                text="secret agent body",
                from_agent=True,
            ),
        ]
        session = SoloSession(
            session_id="solo-timing",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-31T00:00:00Z",
            group_magic="group-magic",
        )
        task = asyncio.create_task(
            self.server._finalize_solo_memory_session(session, history)
        )
        await asyncio.wait_for(summary_started.wait(), timeout=1.0)
        await asyncio.wait_for(classification_started.wait(), timeout=1.0)

        during = await self.overlay_text(self.server)
        self.assertEqual(self.server.solo_memory.sessions, [])
        self.assertNotIn("secret summary", during)
        self.assertNotIn("secret user body", during)
        self.assertNotIn("secret agent body", during)

        release_classification.set()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(len(self.server.solo_memory.sessions), 1)
        after = await self.overlay_text(self.server)
        self.assertIn(NSFW_SOLO_PLACEHOLDER, after)
        self.assertNotIn("secret summary", after)
        self.assertNotIn("secret user body", after)
        self.assertNotIn("secret agent body", after)

    async def test_nsfw_classifier_never_falls_back_to_group_model(self) -> None:
        from engine.solo_mode import SoloSession

        classifier_calls: list[str] = []

        async def fake_summarize(session, **_kwargs):
            session.summary = "summary"
            session.summary_status = "ready"
            return session

        async def fake_classify(session, **_kwargs):
            classifier_calls.append(session.session_id)
            session.nsfw = False
            session.nsfw_status = "ready"
            return session

        self.server._solo_memory_llm_config = lambda: {
            "base_prompt": "BASE",
            "proxy_base_url": "http://127.0.0.1:1/v1",
            "proxy_token": "group-token",
            "model": "group-model",
            "api": "openai-completions",
            "route_path": "/chat/completions",
        }
        self.server._solo_nsfw_llm_config = lambda: None
        self.server.solo_memory.summarize = fake_summarize
        self.server.solo_memory.classify_nsfw = fake_classify
        history = [
            ChannelTranscriptEntry(
                seq=1,
                sender="Master",
                sender_id="master",
                text="private body",
            )
        ]
        session = SoloSession(
            session_id="solo-no-route",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-31T00:00:00Z",
            group_magic="group-magic",
        )

        await self.server._finalize_solo_memory_session(session, history)

        self.assertEqual(classifier_calls, [])
        stored = self.server.solo_memory.sessions[0]
        self.assertTrue(stored.nsfw)
        self.assertEqual(stored.nsfw_status, "error")

    async def test_proactive_text_is_recorded_in_solo_history(self) -> None:
        from engine.solo_mode import SoloSession

        self.server._solo_session = SoloSession(
            session_id="solo-proactive-publish",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-31T00:00:00Z",
            group_magic="group-magic",
        )
        self.server.channel_history_path = self.server.solo_channel_history_path
        turn = Turn(
            turn_id="turn-proactive-publish",
            conn=NullConnection(),
            message="trigger",
            include_reasoning=False,
            client_message_id=None,
            origin="proactive",
            trigger_kind="presence",
        )
        await self.server._publish_proactive_text(turn, "已经很晚了，该休息了。")
        self.assertEqual(self.channel.published[-1][0], "已经很晚了，该休息了。")
        solo_text = self.server.solo_channel_history_path.read_text(encoding="utf-8")
        self.assertIn("已经很晚了，该休息了。", solo_text)
        self.assertFalse(self.server.group_channel_history_path.exists())

    async def test_bridge_keeps_solo_rules_and_memories_in_system_prompt(self) -> None:
        from engine.solo_memory import SoloMemoryEntry, SoloMemorySession
        from engine.solo_mode import SoloSession

        self.server.backend.supports_aki_bridge = True
        self.server._solo_session = SoloSession(
            session_id="solo-active",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-28T00:00:00Z",
            group_magic="group-magic",
        )
        self.server._solo_scene = "私密房间。"
        self.server.solo_memory.add_session(
            SoloMemorySession(
                session_id="solo-old",
                started_at="2026-10-27T00:00:00Z",
                ended_at="2026-10-27T00:10:00Z",
                summary="旧独处摘要",
                entries=[
                    SoloMemoryEntry(
                        id="sme_old",
                        seq=1,
                        role="user",
                        sender="Master",
                        timestamp="2026-10-27T00:01:00Z",
                        text="旧独处秘密",
                        tokens=8,
                    )
                ],
            )
        )
        overlay = await self.overlay_text(self.server)
        self.assertIn("# solo-mode", overlay)
        self.assertIn("# solo-memories", overlay)
        self.assertLess(
            overlay.index("# solo-mode"), overlay.index("# solo-memories")
        )
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-mode", system)
        self.assertNotIn("<solo-memories", system)
        self.assertIn("旧独处秘密", overlay)
        self.assertNotIn("solo-active", overlay.split("# solo-memories", 1)[1])

    async def test_first_solo_turn_installs_discovered_model(self) -> None:
        conf = self.root / "aki.conf"
        atomic_write(conf, template_text())
        set_key(conf, "proxy", "token", "loopback-token")
        set_key(conf, "upstream.deepseek", "api_key", "secret")
        set_key(
            conf,
            "solo",
            "startup_script",
            "llama-server --host 127.0.0.1 --port 10000 -m /tmp/model.gguf",
        )
        self.config.aki = load_config(conf)
        route = SoloRoute(
            model_id="Discovered.Model",
            upstream_model="/tmp/model.gguf",
            context_window=150000,
            max_tokens=8192,
            inputs=("text",),
            endpoint="http://127.0.0.1:10000/v1",
        )

        class FakeSoloBackend:
            configured = True

            def __init__(self) -> None:
                self.started = 0
                self.activity = 0

            async def start(self, **_kwargs):
                self.started += 1
                return route

            def mark_activity(self) -> None:
                self.activity += 1

        fake = FakeSoloBackend()
        self.server.solo_backend = fake
        self.server._solo_session = SoloSession(
            session_id="solo-model",
            target_id="legacy_aki",
            target_name="Aki",
            profile="aki",
            started_at="2026-10-31T00:00:00Z",
            group_magic="group-magic",
        )
        self.server._backend_conversation_id = "solo-agent-aki"
        await self.server._prepare_managed_solo_turn()
        self.assertEqual(fake.started, 1)
        self.assertEqual(self.server.backend.reloaded, ["solo-agent-aki"])
        records = list(
            (self.config.backend_dir / "conversations").glob(
                "*/conversation.json"
            )
        )
        solo = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in records
            if json.loads(path.read_text(encoding="utf-8")).get("id")
            == "solo-agent-aki"
        )
        self.assertEqual(solo["model"], "aki/Discovered.Model")
        self.assertEqual(solo["context_window_limit"], 150000)
        self.assertEqual(solo["model_settings"]["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()
