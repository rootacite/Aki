from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.aki_config import atomic_write, load_config, set_key, template_text
from engine.akid import GatewayConfig, GatewayServer, build_config_from_env
from engine.live_context import (
    ChannelMember,
    ChannelTranscriptEntry,
    render_channel_transcript,
)
from engine.member_identity import (
    IdentityRegistry,
    MemberIdentity,
    canonical_name,
    legacy_member_id,
    new_member_id,
    read_manifest,
)
from engine.member_profile import member_name_key
from engine.protocol import ErrorCode, RpcError


ROOT = Path(__file__).resolve().parent.parent


class DummyBackend:
    ready = True

    def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def wait_ready(self, timeout: float) -> bool:
        return True


def factory(on_message, on_ready, on_exit):
    return DummyBackend()


class MemberIdentityVectorTests(unittest.TestCase):
    def test_legacy_ids_match_fixed_vectors(self) -> None:
        self.assertEqual(
            legacy_member_id("Aki"),
            "legacy_8ddfb8585d44e615b2fe8b37846b4ef6",
        )
        self.assertEqual(legacy_member_id(" Aki "), legacy_member_id("Aki"))
        self.assertEqual(
            legacy_member_id("FUYU"),
            "legacy_86a236d147effcfbdfe3cd8b005cdf7b",
        )
        self.assertEqual(
            legacy_member_id(" 秋 "),
            "legacy_c156f64c68b7f6b4347be31185fc8d31",
        )
        self.assertEqual(legacy_member_id("Ａｋｉ"), legacy_member_id("Aki"))

    def test_random_ids_are_well_formed(self) -> None:
        first = new_member_id()
        second = new_member_id()
        self.assertRegex(first, r"^mem_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_unicode_canonicalization(self) -> None:
        self.assertEqual(canonical_name("  ＡＫＩ "), "aki")
        self.assertEqual(member_name_key(" 秋 "), "秋")


class IdentityRegistryTests(unittest.TestCase):
    def test_aliases_are_permanent_and_conflicts_do_not_steal(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            registry = IdentityRegistry(Path(tempdir) / "identities.json")
            aki = legacy_member_id("Aki")
            registry.register(MemberIdentity(id=aki, name="Aki"), source="device-a")
            renamed = registry.rename(aki, "秋")
            self.assertEqual(registry.snapshot()["sources"][aki], "device-a")
            self.assertEqual(renamed.id, aki)
            self.assertEqual(registry.resolve("Aki"), aki)
            self.assertEqual(registry.resolve("秋"), aki)

            claimed, conflicts = registry.observe(
                MemberIdentity(
                    id="mem_0123456789abcdef0123456789abcdef",
                    name="Aki",
                ),
                source="device-b",
            )
            self.assertFalse(claimed)
            self.assertEqual("Aki", conflicts[0]["label"])
            self.assertEqual(registry.resolve("Aki"), aki)
            self.assertTrue(registry.snapshot()["conflicts"])


class ProfileIdentityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.aki_dir = self.home / ".aki"
        self.aki_dir.mkdir()
        self.cwd = self.home / "repo"
        self.cwd.mkdir()
        self.userdata = self.cwd / "userdata"
        self.userdata.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _config(self, profile: str = "aki") -> Path:
        path = self.aki_dir / f"{profile}.conf"
        atomic_write(path, template_text(profile=profile))
        set_key(path, "gateway", "userdata", str(self.userdata))
        return path

    def test_manifest_is_authoritative_and_mirrored_into_config(self) -> None:
        path = self._config()
        env = {
            "AKI_CONFIG": str(path),
            "AKI_CWD": str(self.cwd),
            "XDG_RUNTIME_DIR": str(self.home / "run"),
            "HOME": str(self.home),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("AKI_SOCKET", None)
            first = build_config_from_env()
            second = build_config_from_env()
        manifest = read_manifest(self.userdata / "identity.json")
        self.assertIsNotNone(manifest)
        assert manifest is not None
        expected = legacy_member_id("Aki")
        self.assertEqual(first.channel_member_id, expected)
        self.assertEqual(second.channel_member_id, expected)
        self.assertEqual(manifest["id"], expected)
        self.assertEqual(manifest["profile"], "aki")
        mirrored = load_config(path, profile="aki")
        self.assertEqual(mirrored.channel.id, expected)
        self.assertEqual(mirrored.channel.name, "Aki")
        registry = IdentityRegistry(self.aki_dir / "identities.json")
        self.assertEqual(registry.resolve("Aki"), expected)
        self.assertEqual(
            len(list((self.aki_dir / "backups").glob("identity-migration-*"))),
            1,
        )

    def test_two_profiles_cannot_share_one_manifest(self) -> None:
        aki_path = self._config("aki")
        with mock.patch.dict(
            os.environ,
            {
                "AKI_CONFIG": str(aki_path),
                "AKI_CWD": str(self.cwd),
                "XDG_RUNTIME_DIR": str(self.home / "run"),
                "HOME": str(self.home),
            },
            clear=False,
        ):
            os.environ.pop("AKI_SOCKET", None)
            build_config_from_env()
        fuyu_path = self.aki_dir / "fuyu.conf"
        atomic_write(fuyu_path, template_text(profile="fuyu"))
        set_key(fuyu_path, "gateway", "userdata", str(self.userdata))
        from engine.aki_config import ConfigError

        with mock.patch.dict(
            os.environ,
            {
                "AKI_CONFIG": str(fuyu_path),
                "AKI_CWD": str(self.cwd),
                "XDG_RUNTIME_DIR": str(self.home / "run"),
                "HOME": str(self.home),
            },
            clear=False,
        ):
            with self.assertRaises(ConfigError):
                build_config_from_env()


class IdentityRpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.userdata = self.root / "userdata"
        self.userdata.mkdir()
        self.config_path = self.root / ".aki" / "aki.conf"
        atomic_write(self.config_path, template_text())
        self.member_id = legacy_member_id("Aki")
        self.registry_path = self.root / ".aki" / "identities.json"

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    def make_server(self) -> GatewayServer:
        config = GatewayConfig(
            socket_path=self.root / "akid.sock",
            profile="aki",
            agent_id="agent-aki",
            cwd=ROOT,
            userdata_dir=self.userdata,
            backend_dir=self.root / "backend",
            mqtt_enabled=False,
            channel_id=self.member_id,
            channel_member_id=self.member_id,
            channel_name="Aki",
            channel_aliases=("Aki", "aki"),
            avatar_path=None,
            identity_manifest_path=self.userdata / "identity.json",
            identity_registry_path=self.registry_path,
            profile_config_path=self.config_path,
            live_context_enabled=False,
            screenshot_enabled=False,
            camera_enabled=False,
        )
        return GatewayServer(config, backend_factory=factory)

    async def test_rename_keeps_member_id_and_old_alias(self) -> None:
        server = self.make_server()
        server._identity_registry.register(
            MemberIdentity(id=self.member_id, name="Aki", aliases=("aki",))
        )
        result = await server._identity_rename({"name": "秋"})
        self.assertEqual(result["id"], self.member_id)
        self.assertEqual(result["name"], "秋")
        self.assertIn("Aki", result["aliases"])
        self.assertEqual(server._channel_self_key(), self.member_id)
        manifest = read_manifest(self.userdata / "identity.json")
        self.assertEqual(manifest["id"], self.member_id)
        self.assertEqual(manifest["name"], "秋")
        self.assertEqual(
            IdentityRegistry(self.registry_path).resolve("Aki"), self.member_id
        )
        self.assertEqual(
            IdentityRegistry(self.registry_path).resolve("秋"), self.member_id
        )
        self.assertEqual(
            server._member_key(
                {"agentName": "Aki", "agentId": "aki"},
                "Aki",
                "device-old-name",
            ),
            self.member_id,
        )
        self.assertEqual(
            server._member_key(
                {"agentName": "秋", "agentId": "秋"},
                "秋",
                "device-new-name",
            ),
            self.member_id,
        )

    async def test_rename_to_another_owner_fails(self) -> None:
        server = self.make_server()
        server._identity_registry.register(
            MemberIdentity(id=self.member_id, name="Aki"), source="a"
        )
        fuyu = legacy_member_id("Fuyu")
        server._identity_registry.register(
            MemberIdentity(id=fuyu, name="Fuyu"), source="b"
        )
        with self.assertRaises(RpcError) as caught:
            await server._identity_rename({"name": "Fuyu"})
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_PARAMS)


class HistoryIdentityMigrationTests(unittest.TestCase):
    def test_old_sender_ids_are_normalized_to_member_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = GatewayConfig(
                socket_path=root / "akid.sock",
                profile="aki",
                agent_id="agent-aki",
                cwd=ROOT,
                backend_dir=root / "backend",
                mqtt_enabled=False,
                channel_member_id=legacy_member_id("Aki"),
                channel_name="Aki",
                channel_history_dir=root / "history",
                avatar_path=None,
                live_context_enabled=False,
                screenshot_enabled=False,
                camera_enabled=False,
            )
            server = GatewayServer(config, backend_factory=factory)
            path = server._channel_history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "sender": "Aki",
                        "sender_id": "device-old",
                        "text": "old",
                        "from_agent": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            server._load_channel_history()
            self.assertEqual(server._channel_history[0].sender_id, legacy_member_id("Aki"))
            self.assertEqual(legacy_member_id("Aki"), server._channel_self_key())

    def test_transcript_uses_the_recorded_sender_name(self) -> None:
        member_id = legacy_member_id("Aki")
        entries = [
            ChannelTranscriptEntry(
                seq=1,
                sender="Aki",
                sender_id=member_id,
                text="old message",
            )
        ]
        rendered = render_channel_transcript(entries)
        self.assertIn('Aki (user, at unknown, seq=1): "old message"', rendered)
        self.assertNotIn("<profiles>", rendered)


if __name__ == "__main__":
    unittest.main()
