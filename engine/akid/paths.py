"""Default paths for sockets, captures, memory, and video devices."""

from __future__ import annotations

import base64
import os
import stat
import uuid
from datetime import (
    datetime,
    timezone,
)
from pathlib import (
    Path,
    PurePosixPath,
)
from engine.aki_config import (
    default_config_path,
    default_socket_path as profile_default_socket_path,
)
from engine.protocol import (
    ErrorCode,
    RpcError,
)
from .constants import (
    SAFE_TOOLS,
    SAFE_TOOL_PREFIXES,
)



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



def default_socket_path(profile: str = "aki") -> Path:
    return profile_default_socket_path(profile)



def default_camera_dir() -> Path:
    return Path("/tmp") / "aki" / "camera"



def default_screenshot_dir() -> Path:
    return Path("/tmp") / "aki" / "screenshots"



def default_image_generation_dir() -> Path:
    return Path("/tmp") / "aki" / "images"



def unique_capture_path(directory: Path, prefix: str, suffix: str) -> Path:
    """Return a timestamped path with a random suffix to avoid capture collisions."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"



def prepare_private_output_dir(path: Path, label: str) -> None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError(f"{label} output directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        info = path.lstat()
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} output path is not a directory")
        if info.st_uid != os.getuid() or parent_info.st_uid != os.getuid():
            raise ValueError(
                f"{label} output directory is not owned by the current user"
            )
        os.chmod(path, 0o700)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"cannot prepare {label} output directory: {exc}") from exc



def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"



def default_turn_overlay_dir(backend_dir: Path, agent_id: str) -> Path:
    return backend_dir / "aki-turn-overlays" / (agent_id or "agent")



def default_mcp_native_state_path() -> Path:
    return default_config_path()



def default_memory_dir(agent_id: str) -> Path:
    configured = os.environ.get("AKI_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return default_backend_dir() / "memfs" / agent_id / "memory"



def tool_is_safe(name: str) -> bool:
    return name in SAFE_TOOLS or any(name.startswith(p) for p in SAFE_TOOL_PREFIXES)



def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")



def resolve_memory_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RpcError(ErrorCode.INVALID_PATH, "path must be a non-empty string")
    if "\x00" in relative:
        raise RpcError(ErrorCode.INVALID_PATH, "path contains a NUL byte")
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        raise RpcError(ErrorCode.INVALID_PATH, "absolute paths are not allowed")
    for part in pure.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise RpcError(ErrorCode.INVALID_PATH, "parent traversal is not allowed")
        if part.startswith("."):
            raise RpcError(ErrorCode.INVALID_PATH, "hidden paths are not allowed")
    try:
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise RpcError(ErrorCode.BACKEND_UNAVAILABLE, "memory root is unavailable") from exc
    candidate = root_real.joinpath(*pure.parts)
    try:
        real = candidate.resolve(strict=False)
    except OSError as exc:
        raise RpcError(ErrorCode.INVALID_PATH, "invalid path") from exc
    if real != root_real and root_real not in real.parents:
        raise RpcError(ErrorCode.INVALID_PATH, "path escapes the memory root")
    return real



def _is_video_device(path: Path) -> bool:
    name = path.name
    return (
        path.is_char_device()
        and name.startswith("video")
        and name[5:].isdigit()
    )
