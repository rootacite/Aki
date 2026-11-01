"""Shrink channel images so they fit the tokifactor picture limit."""

from __future__ import annotations

import io
import math

from engine.tokifactor import MAX_IMAGE_BYTES, detect_image_mime


class ImageCompressionError(ValueError):
    """Raised when an image cannot be compressed below the wire limit."""


_FORMAT_BY_MIME = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
_MIME_BY_FORMAT = {value: key for key, value in _FORMAT_BY_MIME.items()}
_LOSSY_FORMATS = {"JPEG", "WEBP"}
_QUALITIES = (90, 80, 70, 60, 50, 40, 30)
IMAGE_COMPRESS_THRESHOLD_BYTES = 2 * 1024 * 1024


def _resize(image, scale: float):
    if scale >= 1.0:
        return image
    width = max(1, int(round(image.width * scale)))
    height = max(1, int(round(image.height * scale)))
    from PIL import Image

    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode_static(image, image_format: str, quality: int | None) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    if image_format == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(
            output,
            "JPEG",
            quality=quality or 85,
            optimize=True,
            progressive=True,
        )
    elif image_format == "PNG":
        image.save(output, "PNG", compress_level=6)
    elif image_format == "WEBP":
        image.save(output, "WEBP", quality=quality or 85, method=4)
    elif image_format == "GIF":
        if image.mode not in ("P", "L"):
            image = image.convert("P", palette=Image.Palette.ADAPTIVE)
        image.save(output, "GIF", optimize=True)
    else:  # pragma: no cover - caller only passes known formats
        raise ImageCompressionError(f"unsupported image format: {image_format}")
    return output.getvalue()


def _initial_scale(max_bytes: int, source_bytes: int) -> float:
    ratio = max_bytes / max(1, source_bytes)
    return min(1.0, max(0.05, math.sqrt(ratio) * 0.9))


def _next_scale(scale: float, encoded_bytes: int, max_bytes: int) -> float:
    if encoded_bytes <= 0:
        return scale * 0.85
    ratio = max_bytes / encoded_bytes
    return min(scale * 0.95, max(0.05, scale * math.sqrt(ratio) * 0.95))


def _compress_static(
    image,
    image_format: str,
    max_bytes: int,
    source_bytes: int,
) -> bytes | None:
    if image_format in _LOSSY_FORMATS:
        scale = 1.0
        qualities = (
            (80, 60, 40, 30)
            if source_bytes > MAX_IMAGE_BYTES
            else _QUALITIES
        )
        last_size = 0
        for _ in range(12):
            candidate = _resize(image, scale)
            for quality in qualities:
                encoded = _encode_static(candidate, image_format, quality)
                last_size = len(encoded)
                if last_size <= max_bytes:
                    return encoded
            qualities = (80, 60, 40)
            next_scale = _next_scale(scale, last_size, max_bytes)
            if next_scale >= scale * 0.999:
                next_scale = scale * 0.85
            scale = next_scale
    else:
        scale = _initial_scale(max_bytes, source_bytes)
        for _ in range(12):
            candidate = _resize(image, scale)
            encoded = _encode_static(candidate, image_format, None)
            if len(encoded) <= max_bytes:
                return encoded
            next_scale = _next_scale(scale, len(encoded), max_bytes)
            if next_scale >= scale * 0.999:
                next_scale = scale * 0.85
            scale = next_scale
    return None


def _compress_animated(
    opened,
    image_format: str,
    max_bytes: int,
    source_bytes: int,
) -> bytes | None:
    from PIL import Image, ImageOps, ImageSequence

    frames = []
    durations = []
    for frame in ImageSequence.Iterator(opened):
        frames.append(ImageOps.exif_transpose(frame.copy()))
        durations.append(int(frame.info.get("duration") or opened.info.get("duration") or 100))
    if not frames:
        return None
    loop = int(opened.info.get("loop") or 0)
    scale = (
        1.0
        if image_format == "WEBP" and source_bytes <= MAX_IMAGE_BYTES
        else _initial_scale(max_bytes, source_bytes)
    )
    if image_format == "WEBP":
        qualities: tuple[int | None, ...] = (
            _QUALITIES if source_bytes <= MAX_IMAGE_BYTES else (80, 60, 40, 30)
        )
    else:
        qualities = (None,)
    for _ in range(12):
        resized = [_resize(frame, scale) for frame in frames]
        for quality in qualities:
            output = io.BytesIO()
            if image_format == "GIF":
                palette_frames = [
                    frame
                    if frame.mode in ("P", "L")
                    else frame.convert("P", palette=Image.Palette.ADAPTIVE)
                    for frame in resized
                ]
                palette_frames[0].save(
                    output,
                    "GIF",
                    save_all=True,
                    append_images=palette_frames[1:],
                    duration=durations,
                    loop=loop,
                    optimize=True,
                    disposal=2,
                )
            elif image_format == "WEBP":
                resized[0].save(
                    output,
                    "WEBP",
                    save_all=True,
                    append_images=resized[1:],
                    duration=durations,
                    loop=loop,
                    quality=quality or 85,
                    method=4,
                )
            else:  # pragma: no cover - animated output is only GIF/WEBP
                return None
            encoded = output.getvalue()
            if len(encoded) <= max_bytes:
                return encoded
        if image_format == "WEBP":
            qualities = (80, 60, 40)
        next_scale = _next_scale(scale, len(encoded), max_bytes)
        if next_scale >= scale * 0.999:
            next_scale = scale * 0.85
        scale = next_scale
    return None


def compress_image_to_limit(
    data: bytes,
    mime: str,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[bytes, str]:
    """Return image bytes no larger than ``max_bytes``, preserving the format."""
    if len(data) <= max_bytes:
        return data, mime
    image_format = _FORMAT_BY_MIME.get(mime)
    if image_format is None:
        detected = detect_image_mime(data)
        image_format = _FORMAT_BY_MIME.get(detected or "")
    if image_format is None:
        raise ImageCompressionError("unsupported image format")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - Pillow is a runtime dependency
        raise ImageCompressionError("Pillow is required to compress images") from exc
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            animated = bool(
                getattr(opened, "is_animated", False)
                and int(getattr(opened, "n_frames", 1)) > 1
            )
            if animated and image_format in {"GIF", "WEBP"}:
                encoded = _compress_animated(
                    opened,
                    image_format,
                    max_bytes,
                    len(data),
                )
            else:
                encoded = _compress_static(
                    ImageOps.exif_transpose(opened),
                    image_format,
                    max_bytes,
                    len(data),
                )
    except ImageCompressionError:
        raise
    except Exception as exc:
        raise ImageCompressionError(f"cannot decode image: {exc}") from exc
    if encoded is None:
        raise ImageCompressionError(f"cannot compress image below {max_bytes} bytes")
    detected = detect_image_mime(encoded)
    if detected is None:
        raise ImageCompressionError("compressed image has an unsupported format")
    return encoded, detected


def compress_channel_image(
    data: bytes,
    mime: str,
    max_bytes: int = MAX_IMAGE_BYTES,
    threshold_bytes: int = IMAGE_COMPRESS_THRESHOLD_BYTES,
) -> tuple[bytes, str]:
    """Compress oversized channel images, preferring the 2 MiB target."""
    if len(data) <= threshold_bytes:
        return data, mime
    try:
        return compress_image_to_limit(data, mime, threshold_bytes)
    except ImageCompressionError:
        if len(data) <= max_bytes:
            return data, mime
        return compress_image_to_limit(data, mime, max_bytes)
