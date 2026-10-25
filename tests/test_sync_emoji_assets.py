from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sync-emoji-assets.py"
spec = importlib.util.spec_from_file_location("sync_emoji_assets", SCRIPT)
assert spec and spec.loader
sync_emoji_assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_emoji_assets)


class SyncEmojiAssetsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "Aki" / "assets" / "emoji"
        self.tokifactor = self.root / "tokifactor"
        profile = self.source / "aki"
        profile.mkdir(parents=True)
        Image.new("RGBA", (1024, 768), (255, 0, 0, 128)).save(
            profile / "aki_cheer.png"
        )
        (profile / "index.json").write_text(
            json.dumps(
                [
                    {
                        "file": "aki_cheer.png",
                        "name": "aki_cheer",
                        "desc": "开心时使用",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_generates_resized_resources_and_catalog(self) -> None:
        result = sync_emoji_assets.sync_emoji_assets(
            self.source, self.tokifactor, max_dimension=512
        )
        self.assertEqual(result["count"], 1)
        target = self.tokifactor / sync_emoji_assets.RESOURCE_SUFFIX
        image_path = target / "aki" / "aki_cheer.png"
        with Image.open(image_path) as image:
            self.assertEqual(image.size, (512, 384))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 128)
        catalog = json.loads((target / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(
            catalog,
            {
                "version": 1,
                "emojis": [
                    {
                        "name": "aki_cheer",
                        "file": "emoji/aki/aki_cheer.png",
                        "desc": "开心时使用",
                    }
                ],
            },
        )

    def test_removes_stale_resources(self) -> None:
        target = self.tokifactor / sync_emoji_assets.RESOURCE_SUFFIX
        (target / "old").mkdir(parents=True)
        (target / "old" / "stale.png").write_bytes(b"stale")
        sync_emoji_assets.sync_emoji_assets(self.source, self.tokifactor)
        self.assertFalse((target / "old").exists())


if __name__ == "__main__":
    unittest.main()
