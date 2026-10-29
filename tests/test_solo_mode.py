from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path

from engine import akictl
from engine.akid import GatewayConfig, GatewayServer, NullConnection, Turn
from engine.live_context import (
    ChannelMember,
    ChannelTranscriptEntry,
    apply_channel_transcript,
    apply_solo_mode,
    apply_solo_transcript,
    render_channel_transcript,
    render_channel_turn,
    render_solo_mode,
    render_solo_transcript,
    strip_channel_mentions,
)
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_START,
    derive_solo_magic,
    load_solo_scene,
)
from engine.tokifactor import TextEnvelope


class DummyBackend:
    ready = True

    def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def wait_ready(self, timeout: float) -> bool:
        return True


def backend_factory(_on_message, _on_ready, _on_exit):
    return DummyBackend()


class FakeChannel:
    def __init__(self) -> None:
        self.connected = True
        self.magic = "group-magic"
        self.controls: list[dict[str, str]] = []

    def set_active_magic(self, magic: str) -> None:
        self.magic = magic

    def discard_inbox(self) -> int:
        return 0

    async def publish_control(self, _text, metadata, **kwargs) -> None:
        self.controls.append(dict(metadata))


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
        self.assertIn('<channel-transcript', system)
        self.assertIn('frozen="true"', system)
        self.assertIn("群聊旧消息", system)
        self.assertIn("<solo-mode", system)
        self.assertIn("当下你正在与Master独处", system)
        self.assertIn("不要写 @成员", system)
        self.assertIn("<solo-transcript", system)
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
        turn = render_channel_turn(
            self_member=members[0],
            members=members,
            message="继续",
            user_name="Master",
            solo=True,
        )
        self.assertIn("直接获取整机执行锁", turn)
        self.assertIn("不需要Master先 @ 指派", turn)

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
        self.assertIn("<channel-transcript", migrated)

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
        return "\n\n".join(await server._build_turn_overlay(turn))

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
        self.assertIn("<solo-mode", overlay)
        self.assertIn("当下你正在与Master独处", overlay)
        self.assertIn("group old", overlay)
        self.assertIn('frozen="true"', overlay)
        self.assertIn("<solo-transcript", overlay)

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
        self.assertNotIn("<solo-memories", active_prompt)

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
        self.assertTrue(session.summary)

        memories = dict(
            json.loads(
                (self.root / "solo-memory.json").read_text(encoding="utf-8")
            )
        )
        self.assertEqual(len(memories["sessions"]), 1)
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-memories", system)
        self.assertNotIn("<solo-mode", system)
        overlay = await self.overlay_text(self.server)
        self.assertIn("<solo-memories", overlay)
        self.assertIn("这是独处里说的秘密", overlay)
        self.assertIn("独处里的私密回复", overlay)
        group_path = self.server.group_channel_history_path
        if group_path.is_file():
            group_text = group_path.read_text(encoding="utf-8")
            self.assertNotIn("这是独处里说的秘密", group_text)
            self.assertNotIn("独处里的私密回复", group_text)

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
        self.assertIn("<solo-mode", overlay)
        self.assertIn("<solo-memories", overlay)
        system = json.loads(self.agent_path.read_text(encoding="utf-8"))["system"]
        self.assertNotIn("<solo-mode", system)
        self.assertNotIn("<solo-memories", system)
        self.assertIn("旧独处秘密", overlay)
        self.assertNotIn("solo-active", overlay.split("<solo-memories", 1)[1])


if __name__ == "__main__":
    unittest.main()
