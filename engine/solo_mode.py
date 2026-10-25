"""Shared helpers for Aki's private solo channel."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from engine.aki_config import atomic_write, state_dir

SOLO_START = "solo_start"
SOLO_READY = "solo_ready"
SOLO_FAILED = "solo_failed"
SOLO_ACTIVE = "solo_active"
SOLO_END = "solo_end"
SOLO_CONTROL_KINDS = frozenset(
    {SOLO_START, SOLO_READY, SOLO_FAILED, SOLO_ACTIVE, SOLO_END}
)
SOLO_MAGIC_INFO = b"aki-solo-v1\x00"
SOLO_PROTOCOL_VERSION = 1


def derive_solo_magic(group_magic: str, member_id: str) -> str:
    """Derive the per-member channel secret without changing the MQTT session."""
    if not group_magic:
        raise ValueError("group magic must not be empty")
    if not member_id:
        raise ValueError("member id must not be empty")
    return hmac.new(
        group_magic.encode("utf-8"),
        SOLO_MAGIC_INFO + member_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SoloSession:
    session_id: str
    target_id: str
    target_name: str
    profile: str
    started_at: str
    group_magic: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": "1",
            "session_id": self.session_id,
            "target_id": self.target_id,
            "target_name": self.target_name,
            "profile": self.profile,
            "started_at": self.started_at,
            "group_magic": self.group_magic,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "SoloSession":
        return cls(
            session_id=str(data.get("session_id") or ""),
            target_id=str(data.get("target_id") or ""),
            target_name=str(data.get("target_name") or ""),
            profile=str(data.get("profile") or ""),
            started_at=str(data.get("started_at") or ""),
            group_magic=str(data.get("group_magic") or ""),
        )


def default_solo_state_path(profile: str, home: Path | None = None) -> Path:
    return state_dir(home) / "solo" / f"{profile}.json"


def load_solo_session(path: Path) -> SoloSession | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session = SoloSession.from_mapping(data)
    if not all(
        (session.session_id, session.target_id, session.profile, session.group_magic)
    ):
        return None
    return session


def save_solo_session(path: Path, session: SoloSession) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(
        path,
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )


def clear_solo_session(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def prompt_path(
    profile: str, explicit: str = "", repo_root: Path | None = None
) -> Path:
    if explicit.strip():
        return Path(explicit).expanduser()
    root = repo_root or Path(__file__).resolve().parent.parent
    return root / "prompts" / f"{profile}-solo.md"


def load_solo_scene(
    profile: str,
    *,
    username: str,
    explicit: str = "",
    repo_root: Path | None = None,
) -> str:
    path = prompt_path(profile, explicit, repo_root)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"solo prompt is empty: {path}")
    return text.replace("{{USERNAME}}", username.strip() or "Master")
