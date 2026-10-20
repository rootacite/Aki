"""Interface-neutral avatar asset for Aki.

The gateway owns one avatar per agent process.  Transport adapters (currently
tokifactor, later other chat or web interfaces) can reuse the same path, bytes,
MIME type, and stable SHA-256 hash instead of maintaining their own copies.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

from engine.tokifactor import detect_image_mime, encrypt_bytes

MAX_AVATAR_BYTES = 4 * 1024 * 1024


class AvatarError(ValueError):
    pass


@dataclass(frozen=True)
class AvatarAsset:
    path: Path
    data: bytes
    sha256: str
    mime: str

    def status(self) -> dict:
        return {
            "configured": True,
            "loaded": True,
            "path": str(self.path),
            "bytes": len(self.data),
            "sha256": self.sha256,
            "mime": self.mime,
        }

    def encrypted_base64(self, magic: str) -> str:
        return base64.b64encode(encrypt_bytes(self.data, magic)).decode("ascii")


def default_avatar_path() -> Path:
    return Path(__file__).resolve().parent.parent / "aki_avi.jpg"


def load_avatar(path: Path) -> AvatarAsset:
    try:
        real = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AvatarError(f"avatar file is unavailable: {path}") from exc
    if not real.is_file():
        raise AvatarError(f"avatar path is not a file: {real}")
    try:
        size = real.stat().st_size
    except OSError as exc:
        raise AvatarError(f"cannot stat avatar file: {real}") from exc
    if size <= 0:
        raise AvatarError("avatar file is empty")
    if size > MAX_AVATAR_BYTES:
        raise AvatarError(f"avatar exceeds {MAX_AVATAR_BYTES} bytes")
    try:
        data = real.read_bytes()
    except OSError as exc:
        raise AvatarError(f"cannot read avatar file: {real}") from exc
    mime = detect_image_mime(data)
    if mime is None:
        raise AvatarError("unsupported avatar format (png/jpeg/gif/webp only)")
    return AvatarAsset(
        path=real,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        mime=mime,
    )
