"""Agent state inspection: profiles, MemFS git log, channel history, solo memories, and context."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from engine.aki_config import discover_profiles, load_config
from engine.akictl.chat import _connect
from engine.akictl.service import _unit_active
from engine.akictl.solo import _read_solo_control_state
from engine.akictl.support import _b64url, _rpc_call
from engine.client import default_socket_path

from .analysis import estimate_tokens
from .state import AgentSnapshot


def get_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def load_agent_snapshots() -> dict[str, AgentSnapshot]:
    """Discover all profiles and populate their current live AgentSnapshot."""
    snapshots: dict[str, AgentSnapshot] = {}
    backend_dir = get_backend_dir()

    for name, path in discover_profiles().items():
        try:
            cfg = load_config(path, profile=name)
        except Exception:
            continue

        agent_id = cfg.agent_id

        # Query live daemon status via socket
        sock_path = default_socket_path(name)
        live_status = None
        try:
            client = _connect(sock_path, json_mode=False)
            try:
                live_status = _rpc_call(client, "aki.status", timeout=1.5)
            finally:
                client.close()
        except Exception:
            live_status = None

        first_model = cfg.models[0].model_id if getattr(cfg, "models", None) else ""
        group_model = first_model or cfg.provider_id or ""
        reasoning_effort = "low"

        if live_status is not None:
            conv_id = str(live_status.get("conversation_id") or "")
            is_solo_mode = bool(
                conv_id.startswith("solo-")
                or live_status.get("channel", {}).get("transcript", {}).get("solo_mode", False)
            )
            status = "solo" if is_solo_mode else "group"
            live_model = str(live_status.get("model") or "")
            if live_model:
                group_model = live_model
            reasoning_effort = str(live_status.get("reasoning") or "low")
        else:
            is_active = _unit_active(f"akid@{name}.service")
            status = "group" if is_active else "offline"

        memfs_path = backend_dir / "memfs" / agent_id / "memory"
        git_commits = []
        if (memfs_path / ".git").exists():
            try:
                out = subprocess.check_output(
                    ["git", "-C", str(memfs_path), "log", "--oneline", "-n", "8", "--format=%h|%cr|%s"],
                    text=True,
                    timeout=2.0,
                    stderr=subprocess.DEVNULL,
                )
                for line in out.splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        git_commits.append({"hash": parts[0], "date": parts[1], "message": parts[2]})
            except Exception:
                pass

        # Letta agent description
        agent_file = backend_dir / "agents" / f"{agent_id}.json"
        desc = ""
        if agent_file.is_file():
            try:
                a_data = json.loads(agent_file.read_text(encoding="utf-8"))
                desc = str(a_data.get("description") or "")
            except Exception:
                pass

        snap = AgentSnapshot(
            profile=name,
            name=cfg.channel.name or name.capitalize(),
            agent_id=agent_id,
            status=status,
            group_model=str(group_model),
            solo_model=str(getattr(cfg, "solo_model", "") or ""),
            reasoning_effort=reasoning_effort,
            description=desc,
            memfs_path=memfs_path if memfs_path.exists() else None,
            git_commits=git_commits,
            live_budget_tokens=int(getattr(cfg, "live_budget_tokens", 65536) or 65536),
            solo_budget_tokens=int(getattr(cfg, "solo_transcript_budget_tokens", 65536) or 65536),
        )
        snapshots[name] = snap

    return snapshots


def load_channel_history(profile: str, budget_tokens: int = 65536, is_solo: bool = False) -> list[dict[str, Any]]:
    """Load local channel history jsonl with token-budget boundary demarcation."""
    history_root = Path.home() / ".aki" / "channel-history"
    if not history_root.is_dir():
        return []

    target_file: Path | None = None
    target_filename = f"{profile}.solo.jsonl" if is_solo else f"{profile}.jsonl"

    for h_dir in history_root.iterdir():
        if not h_dir.is_dir():
            continue
        p_file = h_dir / target_filename
        if p_file.is_file():
            target_file = p_file
            break
        # Fallback if solo file doesn't exist yet
        if is_solo:
            fallback = h_dir / f"{profile}.jsonl"
            if fallback.is_file():
                target_file = fallback
                break

    if not target_file:
        return []

    entries: list[dict[str, Any]] = []
    try:
        lines = target_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    # Read up to last 150 messages
    recent_lines = lines[-150:]
    for line in recent_lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "")
                tokens = estimate_tokens(text)
                entries.append({
                    "seq": item.get("seq", 0),
                    "sender": item.get("sender") or item.get("username") or "user",
                    "timestamp": item.get("timestamp") or item.get("sent_at") or "",
                    "text": text,
                    "tokens": tokens,
                    "in_budget": True,
                })
        except Exception:
            continue

    # Demarcate budget walking backwards
    running_tokens = 0
    for entry in reversed(entries):
        running_tokens += entry["tokens"]
        if running_tokens > budget_tokens:
            entry["in_budget"] = False

    return entries


def load_solo_memories(profile: str, budget_tokens: int = 65536, nsfw_unlocked: bool = False) -> list[dict[str, Any]]:
    """Load solo memory sessions from ~/.aki/solo-memory/{profile}.json."""
    file_path = Path.home() / ".aki" / "solo-memory" / f"{profile}.json"
    if not file_path.is_file():
        return []

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, list):
        return []

    results: list[dict[str, Any]] = []
    for s in raw_sessions:
        if not isinstance(s, dict):
            continue
        is_nsfw = bool(s.get("nsfw", False))
        summary = str(s.get("summary") or "")
        entries = s.get("entries") or []

        if is_nsfw and not nsfw_unlocked:
            display_summary = "⚠️ [包含露骨 NSFW 内容 - 按 N 确认解锁以查看此独处回忆]"
            display_entries = []
        else:
            display_summary = summary
            display_entries = entries

        s_tokens = estimate_tokens(display_summary + " " + " ".join(str(e.get("text", "")) for e in display_entries if isinstance(e, dict)))

        results.append({
            "session_id": s.get("session_id", ""),
            "started_at": s.get("started_at", ""),
            "ended_at": s.get("ended_at", ""),
            "nsfw": is_nsfw,
            "summary": display_summary,
            "entries_count": len(entries),
            "tokens": s_tokens,
            "in_budget": True,
        })

    # Reverse walk for budget
    running_tokens = 0
    for res in reversed(results):
        running_tokens += res["tokens"]
        if running_tokens > budget_tokens:
            res["in_budget"] = False

    return results


def load_conversation_context(agent_id: str, is_solo: bool = False) -> list[dict[str, Any]]:
    """Load and format Letta conversation messages into human-readable cards."""
    backend_dir = get_backend_dir()
    conv_prefix = f"conversation:solo-{agent_id}" if is_solo else f"default:{agent_id}"
    conv_dir = backend_dir / "conversations" / _b64url(conv_prefix)
    if not conv_dir.is_dir():
        return []

    conv_meta_file = conv_dir / "conversation.json"
    in_context_ids: set[str] = set()
    if conv_meta_file.is_file():
        try:
            m_data = json.loads(conv_meta_file.read_text(encoding="utf-8"))
            in_context_ids = set(m_data.get("in_context_message_ids") or [])
        except Exception:
            pass

    msgs_file = conv_dir / "messages.jsonl"
    if not msgs_file.is_file():
        return []

    cards: list[dict[str, Any]] = []
    try:
        lines = msgs_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    for line in lines[-80:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            msg_id = record.get("id", "")
            timestamp = record.get("timestamp", "")
            raw_msg = record.get("message") or {}
            role = raw_msg.get("role") or raw_msg.get("message_type") or "unknown"
            raw_content = raw_msg.get("content") or ""

            # Extract text
            if isinstance(raw_content, list):
                text_parts = []
                for part in raw_content:
                    if isinstance(part, dict) and "text" in part:
                        text_parts.append(str(part["text"]))
                    else:
                        text_parts.append(str(part))
                text = "\n".join(text_parts)
            else:
                text = str(raw_content)

            # Tool calls
            tool_calls = raw_msg.get("tool_calls") or []

            cards.append({
                "id": msg_id,
                "role": role,
                "timestamp": timestamp,
                "in_context": msg_id in in_context_ids,
                "text": text,
                "tool_calls": tool_calls,
            })
        except Exception:
            continue

    return cards
