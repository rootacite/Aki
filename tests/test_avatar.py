from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.avatar import AvatarError, load_avatar

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AvatarAssetTests(unittest.TestCase):
    def test_loads_and_hashes_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "avatar.png"
            path.write_bytes(PNG_1X1)
            asset = load_avatar(path)
            self.assertEqual(asset.data, PNG_1X1)
            self.assertEqual(asset.mime, "image/png")
            self.assertEqual(asset.sha256, hashlib.sha256(PNG_1X1).hexdigest())
            self.assertTrue(asset.status()["loaded"])

    def test_rejects_non_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "avatar.txt"
            path.write_text("not an image", encoding="utf-8")
            with self.assertRaises(AvatarError):
                load_avatar(path)

    def test_missing_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AvatarError):
                load_avatar(Path(tmp) / "missing.jpg")


if __name__ == "__main__":
    unittest.main()
