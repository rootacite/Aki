from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engine.emoji_catalog import EmojiEntry, load_emoji_entries
from engine.live_context import (
    ChannelTranscriptEntry,
    render_channel_policy,
    render_channel_transcript,
)


class EmojiCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_dir = self.root / "aki"
        self.profile_dir.mkdir()
        (self.profile_dir / "aki_cheer.png").write_bytes(b"png")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_index(self, entries: object) -> None:
        (self.profile_dir / "index.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def test_loads_only_valid_profile_entries(self) -> None:
        self.write_index(
            [
                {
                    "file": "aki_cheer.png",
                    "name": "aki_cheer",
                    "desc": "开心时使用",
                },
                {
                    "file": "aki_cheer.png",
                    "name": "aki_cheer",
                    "desc": "重复项",
                },
                {
                    "file": "../outside.png",
                    "name": "aki_bad",
                    "desc": "路径穿越",
                },
                {"file": "missing.png", "name": "aki_missing", "desc": "缺失"},
                {"file": "aki_cheer.png", "name": "Aki Upper", "desc": "非法名称"},
            ]
        )
        self.assertEqual(
            load_emoji_entries(self.root, "aki"),
            [EmojiEntry(name="aki_cheer", description="开心时使用")],
        )
        self.assertEqual(load_emoji_entries(self.root, "fuyu"), [])

    def test_policy_contains_catalog_without_asset_paths(self) -> None:
        entries = [
            EmojiEntry(name="aki_cheer", description="开心 <得意> & 庆祝"),
        ]
        policy = render_channel_policy(user_name="Master", emoji_entries=entries)
        self.assertIn('<emoji-catalog version="1">', policy)
        self.assertIn('<emoji name="aki_cheer"', policy)
        self.assertIn("开心 &lt;得意&gt; &amp; 庆祝", policy)
        self.assertIn("`#[name:desc]`", policy)
        self.assertIn("do NOT wrap it in backticks, code fences, or quotes", policy)
        self.assertIn("You MAY use multiple emoji tags at once", policy)
        self.assertIn("Master may use any member's emoji", policy)
        self.assertNotIn("`#[name]`", policy)
        self.assertNotIn("aki_cheer.png", policy)
        self.assertNotIn(self.temp.name, policy)

    def test_policy_without_entries_has_no_catalog(self) -> None:
        policy = render_channel_policy(user_name="Master")
        self.assertNotIn("<emoji-catalog", policy)
        self.assertNotIn("`#[name:desc]`", policy)

    def test_transcript_keeps_raw_emoji_token(self) -> None:
        token = "#[aki_cheer:开心] 你们在干什么？"
        transcript = render_channel_transcript(
            [
                ChannelTranscriptEntry(
                    seq=1,
                    sender="Fuyu",
                    sender_id="fuyu",
                    text=token,
                    from_agent=True,
                )
            ]
        )
        self.assertIn(token, transcript)
        self.assertNotIn("data:image", transcript)


if __name__ == "__main__":
    unittest.main()
