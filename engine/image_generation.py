"""MiniMax text-to-image client used by akid external tools.

The API returns generated images as base64 when ``response_format=base64`` is
requested.  Keeping that contract here avoids a second network fetch and keeps
the model/tool result small: only local paths and metadata are returned to
Letta.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from engine.tokifactor import detect_image_mime

MAX_PROMPT_CHARS = 1500
MAX_IMAGES = 4
DEFAULT_ASPECT_RATIO = "1:1"
ASPECT_RATIOS = (
    "1:1",
    "16:9",
    "4:3",
    "3:2",
    "2:3",
    "3:4",
    "9:16",
    "21:9",
)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024


class ImageGenerationError(RuntimeError):
    """A model-facing image generation failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward the bearer token to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _prepare_output_dir(path: Path) -> None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ImageGenerationError("image output directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ImageGenerationError("image output path is not a directory")
        if info.st_uid != os.getuid():
            raise ImageGenerationError("image output directory is not owned by the current user")
        os.chmod(path, 0o700)
    except ImageGenerationError:
        raise
    except OSError as exc:
        raise ImageGenerationError(f"cannot prepare image output directory: {exc}") from exc


def _write_image(directory: Path, data: bytes, mime: str, index: int) -> Path:
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[mime]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"image-{stamp}-{uuid.uuid4().hex[:10]}-{index + 1}{extension}"
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def _error_message(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        base = payload.get("base_resp")
        if isinstance(base, dict):
            message = base.get("status_msg") or base.get("status_message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
    return fallback


def _read_response(opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: float) -> bytes:
    try:
        with opener.open(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise ImageGenerationError("image API response is too large")
                except ValueError:
                    pass
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        payload: object = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            pass
        raise ImageGenerationError(
            f"image API HTTP {exc.code}: {_error_message(payload, 'request failed')}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason or "network error")
        raise ImageGenerationError(f"image API request failed: {reason}") from exc
    except OSError as exc:
        raise ImageGenerationError(f"image API request failed: {exc}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise ImageGenerationError("image API response is too large")
    return body


def generate_minimax_images(
    *,
    base_url: str,
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    output_dir: Path,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    n: int = 1,
    timeout: float = 180.0,
    max_bytes: int = 32 * 1024 * 1024,
    prompt_optimizer: bool = False,
    aigc_watermark: bool = False,
    use_proxy: bool = True,
    upstream_proxy: str = "",
) -> dict[str, object]:
    """Generate one or more images and write them to ``output_dir``.

    The returned structure contains only local paths, byte counts, MIME types,
    and provider metadata.  It never includes image bytes, the API key, or the
    original prompt.
    """
    if not api_key:
        raise ImageGenerationError("image generation API key is empty")
    if not model.strip():
        raise ImageGenerationError("image generation model is empty")
    if not endpoint.startswith("/"):
        raise ImageGenerationError("image generation endpoint must start with /")
    text = prompt.strip()
    if not text:
        raise ImageGenerationError("prompt must not be empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise ImageGenerationError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
    if aspect_ratio not in ASPECT_RATIOS:
        raise ImageGenerationError("unsupported aspect ratio")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_IMAGES:
        raise ImageGenerationError(f"n must be between 1 and {MAX_IMAGES}")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ImageGenerationError("max_bytes must be positive")
    if timeout <= 0:
        raise ImageGenerationError("timeout must be positive")

    body = json.dumps(
        {
            "model": model,
            "prompt": text,
            "aspect_ratio": aspect_ratio,
            "response_format": "base64",
            "n": n,
            "prompt_optimizer": bool(prompt_optimizer),
            "aigc_watermark": bool(aigc_watermark),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Aki-image-generation/1.0",
        },
    )
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if use_proxy and upstream_proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {"http": upstream_proxy, "https": upstream_proxy}
            )
        )
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    raw = _read_response(opener, request, timeout)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImageGenerationError("image API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ImageGenerationError("image API returned an invalid response")

    base = payload.get("base_resp")
    if isinstance(base, dict):
        code = base.get("status_code")
        if code not in (None, 0, "0"):
            raise ImageGenerationError(
                f"image API error: {_error_message(payload, 'generation failed')}"
            )

    data = payload.get("data")
    encoded = data.get("image_base64") if isinstance(data, dict) else None
    if not isinstance(encoded, list) or not encoded:
        raise ImageGenerationError("image API response did not contain image_base64")
    if len(encoded) > MAX_IMAGES:
        raise ImageGenerationError("image API returned too many images")

    _prepare_output_dir(output_dir)
    images: list[dict[str, object]] = []
    total_bytes = 0
    for index, item in enumerate(encoded):
        if not isinstance(item, str) or not item.strip():
            raise ImageGenerationError("image API returned an empty image")
        value = item.strip()
        value = "".join(value.split())
        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]
        try:
            image_bytes = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageGenerationError("image API returned invalid base64 data") from exc
        if not image_bytes:
            raise ImageGenerationError("image API returned an empty image")
        if len(image_bytes) > max_bytes:
            raise ImageGenerationError("generated image exceeds the configured size limit")
        total_bytes += len(image_bytes)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ImageGenerationError("generated images exceed the total size limit")
        mime = detect_image_mime(image_bytes)
        if mime is None:
            raise ImageGenerationError("generated image has an unsupported format")
        path = _write_image(output_dir, image_bytes, mime, index)
        images.append(
            {
                "path": str(path),
                "bytes": len(image_bytes),
                "mime": mime,
            }
        )

    metadata = payload.get("metadata")
    success_count = metadata.get("success_count") if isinstance(metadata, dict) else None
    return {
        "id": str(payload.get("id") or ""),
        "model": model,
        "aspect_ratio": aspect_ratio,
        "requested": n,
        "count": len(images),
        "success_count": success_count,
        "images": images,
    }
