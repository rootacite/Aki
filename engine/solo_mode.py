"""Shared helpers for Aki's private solo channel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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
# llama-server reads these on the chat-completions body. The Qwen template
# does not map reasoning_effort to a token cap, so the budget has to be sent
# as its own field. medium is the effort name shown to clients.
SOLO_REASONING_EFFORT = "medium"
SOLO_REASONING_BUDGET_TOKENS = 1536
def solo_conversation_id(agent_id: str) -> str:
    """Per-agent id. Letta stores non-default conversations under a global key."""
    if (
        not agent_id
        or agent_id == "default"
        or any(char in agent_id for char in "/:\\")
    ):
        raise ValueError("agent id cannot be used as a solo conversation id")
    return f"solo-{agent_id}"


def letta_conversation_key(conversation_id: str, agent_id: str) -> str:
    """Match Letta's on-disk conversation directory key."""
    if conversation_id == "default":
        return f"default:{agent_id}"
    return f"conversation:{conversation_id}"


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


def conversation_directory(
    backend_dir: Path, conversation_id: str, agent_id: str
) -> Path:
    token = base64.urlsafe_b64encode(
        letta_conversation_key(conversation_id, agent_id).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return backend_dir / "conversations" / token


def ensure_solo_conversation(
    *,
    backend_dir: Path,
    agent_id: str,
    cwd: Path,
    model: str = "",
    provider_type: str = "",
    context_window: int | None = None,
    max_tokens: int | None = None,
) -> Path:
    """Create the persistent solo conversation and set or clear its model."""
    conversation_id = solo_conversation_id(agent_id)
    target = conversation_directory(backend_dir, conversation_id, agent_id)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    timestamp = now_iso()
    manifest = target / "manifest.json"
    if not manifest.exists():
        atomic_write(
            manifest,
            json.dumps(
                {
                    "schema_version": 2,
                    "message_format": "pi-session-entry-jsonl",
                    "provider_stack": "pi-ai",
                    "created_at": timestamp,
                },
                indent=2,
            )
            + "\n",
        )
    conversation_path = target / "conversation.json"
    if conversation_path.is_file():
        try:
            record = json.loads(conversation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if not isinstance(record, dict):
            record = {}
    else:
        record = {
            "id": conversation_id,
            "agent_id": agent_id,
            "archived": False,
            "archived_at": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_message_at": None,
            "summary": None,
            "in_context_message_ids": [],
        }
    record["id"] = conversation_id
    record["agent_id"] = agent_id
    if model:
        record["model"] = model
        settings = record.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
        if provider_type:
            settings["provider_type"] = provider_type
        if max_tokens is not None:
            settings["max_tokens"] = max_tokens
        if context_window is not None:
            settings["context_window_limit"] = context_window
            record["context_window_limit"] = context_window
        record["model_settings"] = settings
    else:
        record.pop("model", None)
        record.pop("model_settings", None)
        record.pop("context_window_limit", None)
    record["updated_at"] = timestamp
    atomic_write(
        conversation_path,
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    )
    messages = target / "messages.jsonl"
    if not messages.exists():
        session = {
            "type": "session",
            "version": 3,
            "id": conversation_id,
            "timestamp": timestamp,
            "cwd": str(cwd),
        }
        atomic_write(
            messages,
            json.dumps(session, separators=(",", ":")) + "\n",
        )
    for path in (manifest, conversation_path, messages):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return target
