from __future__ import annotations

import io
import os
import unittest

from PIL import Image

from engine.image_compress import (
    IMAGE_COMPRESS_THRESHOLD_BYTES,
    compress_channel_image,
    compress_image_to_limit,
)
from engine.tokifactor import MAX_IMAGE_BYTES


class ImageCompressionTests(unittest.TestCase):
    def test_small_image_is_returned_unchanged(self) -> None:
        image = Image.new("RGB", (32, 32), (12, 34, 56))
        output = io.BytesIO()
        image.save(output, "PNG")
        data = output.getvalue()
        compressed, mime = compress_image_to_limit(data, "image/png")
        self.assertIs(compressed, data)
        self.assertEqual(mime, "image/png")

    def test_oversized_png_is_compressed_below_threshold(self) -> None:
        width = 2048
        height = 2048
        image = Image.frombytes(
            "RGB",
            (width, height),
            os.urandom(width * height * 3),
        )
        output = io.BytesIO()
        image.save(output, "PNG", compress_level=0)
        data = output.getvalue()
        self.assertGreater(len(data), MAX_IMAGE_BYTES)

        compressed, mime = compress_channel_image(data, "image/png")
        self.assertEqual(mime, "image/png")
        self.assertLessEqual(len(compressed), IMAGE_COMPRESS_THRESHOLD_BYTES)
        with Image.open(io.BytesIO(compressed)) as result:
            self.assertEqual(result.format, "PNG")
            self.assertGreater(result.width, 0)
            self.assertGreater(result.height, 0)

    def test_compression_target_can_be_overridden(self) -> None:
        image = Image.new("RGB", (64, 64), (1, 2, 3))
        output = io.BytesIO()
        image.save(output, "PNG")
        data = output.getvalue()
        compressed, mime = compress_image_to_limit(data, "image/png", MAX_IMAGE_BYTES)
        self.assertIs(compressed, data)
        self.assertEqual(mime, "image/png")


if __name__ == "__main__":
    unittest.main()
