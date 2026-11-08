"""Presence, reflection progress, execution lock, and tools/MCP inspection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_presence_status() -> dict[str, Any]:
    """Load user presence and late night activity from ~/.aki/presence/aki.json."""
    path = Path.home() / ".aki" / "presence" / "aki.json"
    if not path.is_file():
        return {
            "active_seconds": 0,
            "last_active": False,
            "last_locked": False,
            "last_return_at": "",
            "idle_seconds": 0,
            "late_night_dates": [],
            "next_work_threshold": 7200,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        trigger = data.get("last_trigger") or {}
        facts = trigger.get("facts") or {} if isinstance(trigger, dict) else {}
        return {
            "active_seconds": float(data.get("active_seconds") or 0),
            "last_active": bool(data.get("last_active", False)),
            "last_active_at": str(data.get("last_active_at") or ""),
            "last_locked": bool(data.get("last_locked", False)),
            "last_return_at": str(facts.get("returned_at") or data.get("last_return_at") or ""),
            "idle_seconds": int(facts.get("idle_seconds") or 0),
            "late_night_dates": list(data.get("late_night_dates") or []),
            "next_work_threshold": float(data.get("next_work_threshold") or 7200),
        }
    except Exception:
        return {}


def load_execution_lock_status() -> dict[str, Any]:
    """Inspect cross-process execution lock at /run/user/<uid>/aki/execution.lock."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    lock_file = runtime / "aki" / "execution.lock"
    status_file = lock_file.with_name(f"{lock_file.name}.json")

    held = False
    holder = "None"
    since = ""
    if status_file.is_file():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            held = bool(data.get("held", False))
            holder = str(data.get("holder") or "None")
            since = str(data.get("acquired_at") or "")
        except Exception:
            pass

    return {
        "lock_path": str(lock_file),
        "held": held,
        "holder": holder,
        "acquired_at": since,
    }


def load_reflection_states() -> list[dict[str, Any]]:
    """Load reflection transcript states from ~/.letta/transcripts/*/*/state.json."""
    from engine.aki_config import discover_profiles, load_config

    agent_map: dict[str, dict[str, str]] = {}
    for p, path in discover_profiles().items():
        try:
            cfg = load_config(path, profile=p)
            agent_map[cfg.agent_id] = {
                "profile": p,
                "name": cfg.channel.name or p.capitalize(),
            }
        except Exception:
            continue

    transcripts_dir = Path.home() / ".letta" / "transcripts"
    if not transcripts_dir.is_dir():
        return []

    results = []
    for agent_dir in transcripts_dir.iterdir():
        if not agent_dir.is_dir() or agent_dir.name == "background":
            continue
        agent_id = agent_dir.name
        agent_info = agent_map.get(agent_id)
        if not agent_info:
            continue

        for conv_dir in agent_dir.iterdir():
            state_file = conv_dir / "state.json"
            if state_file.is_file():
                try:
                    st = json.loads(state_file.read_text(encoding="utf-8"))
                    conv_name = conv_dir.name
                    is_solo = (conv_name != "default")
                    results.append({
                        "agent_id": agent_id,
                        "profile": agent_info["profile"],
                        "agent_name": agent_info["name"],
                        "conversation": conv_name,
                        "is_solo": is_solo,
                        "conv_type": "独处" if is_solo else "群聊",
                        "total_steps": int(st.get("total_completed_steps") or 0),
                        "reflected_steps": int(st.get("reflected_completed_steps") or 0),
                        "pending_steps": int(st.get("steps_since_last_successful_reflection") or 0),
                        "anchor_id": str(st.get("reflected_through_message_id") or ""),
                    })
                except Exception:
                    continue

    profile_order = {"aki": 0, "fuyu": 1, "haru": 2, "natsu": 3}
    results.sort(key=lambda r: (profile_order.get(r["profile"], 99), 1 if r["is_solo"] else 0))
    return results


def load_tool_matrix() -> dict[str, Any]:
    """Return configured tool ownership and exclusive assignments."""
    return {
        "Haru": {
            "exclusive": ["generate_image", "comfyui_startup", "comfyui_generate", "comfyui_end", "desktop_take_screenshot", "camera_take_photo", "tokifactor_send_file"],
            "disallowed": ["bash", "shell", "subagent", "write_outside_tmp"],
            "role": "视觉与创意策源 / 文生图 / 实况控制",
        },
        "Natsu": {
            "exclusive": ["recon_edr_mcp", "ebpf_probes", "sandbox_audit"],
            "disallowed": [],
            "role": "红队安全架构师 / 本地渗透与逆向 / EDR探针",
        },
        "Fuyu": {
            "exclusive": ["code_review", "git_audit", "system_diagnostics"],
            "disallowed": [],
            "role": "DevOps 审查员 / 代码审计 / 自动化流水线",
        },
        "Aki": {
            "exclusive": ["memory_steward", "reflection_merger"],
            "disallowed": [],
            "role": "名誉队长 / 长期陪伴 / 自主反思与记忆维护",
        },
    }
