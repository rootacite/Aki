#!/usr/bin/env python3
"""Send a production-shaped Aki turn to the local Qwen server.

The request mirrors akid's current prompt architecture:

  system = compiled Letta system prompt + stable system_append
  user   = dynamic user_message_prefix + "\n\n" + MQTT-enveloped user text

The dynamic prefix is assembled in the same order as
``engine/akid/turns.py::_build_turn_overlay``:

  solo-memories, channel-transcript, solo-mode, solo-transcript,
  live-context, turn-control, format-guard

Clock values are pinned to 22:00 local time so repeated runs are comparable.
By default the turn starts from the real Letta conversation and the real group
channel transcript. Pass ``--fresh`` for a clean, in-memory-only conversation.
Qwen thinking is enabled by default with a 1536-token reasoning budget;
pass ``--no-thinking`` to disable it. ``--deep-think`` adds a separate
minimal-prompt analysis pass when the single-pass reply is too shallow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import configparser
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.emoji_catalog import EmojiEntry, load_emoji_entries  # noqa: E402
from engine.life_ledger import LifeLedger, render_ledger_block  # noqa: E402
from engine.live_context import (  # noqa: E402
    ChannelImageAttachment,
    ChannelMember,
    ChannelTranscriptEntry,
    LiveContextOptions,
    LiveContextSnapshot,
    collect_live_context,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_transcript,
    render_channel_wake_routing,
    render_live_context,
    render_solo_memories,
    render_solo_mode,
    render_solo_transcript,
    render_turn_control,
    render_user_format_guard,
    estimate_tokens,
)
from engine.member_identity import legacy_member_id  # noqa: E402
from engine.member_profile import (  # noqa: E402
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    normalize_member_kind,
    truncate_member_description,
)
from engine.solo_memory import (  # noqa: E402
    SOLO_MEMORY_BUDGET_TOKENS,
    SoloMemorySession,
)
from engine.solo_mode import load_solo_scene  # noqa: E402
from engine.tool_ownership import (  # noqa: E402
    render_tool_ownership_prompt,
    tools_for_profile,
)


FIXED_HOUR = 22
FIXED_MINUTE = 0
DEFAULT_BASE_URL = "http://127.0.0.1:10000/v1"
DEFAULT_MAX_TOKENS = 8192
# Mirrors the reasoning fields used in ~/.aki/aki.conf extra_body.
DEFAULT_REASONING_BUDGET_TOKENS = 1536
DEFAULT_REASONING_BUDGET_MESSAGE = (
    "Reasoning budget exhausted. Stop thinking and answer the user directly now."
)
ANALYSIS_SYSTEM_PROMPT = """You are a careful reasoning assistant. Analyze the user's message before answering:
1. Restate the literal question and the user's implicit goal.
2. Identify the object, person, or state that must actually change to achieve that goal.
3. Test the obvious answer against that requirement.
4. If the obvious answer does not change the required object/state, reject it and choose the answer that does.
Return a concise analysis only. Do not write the final conversational reply."""
ANALYSIS_INJECTION = """# precomputed analysis (test-only, not user content)

Use this analysis to answer correctly. Do not quote it, summarize it, or mention that it exists.

<analysis>
{analysis}
</analysis>"""
CHANNEL_TRANSCRIPT_BUDGET_TOKENS = 32 * 1024
LIVE_CONTEXT_BUDGET_TOKENS = 64 * 1024
MAX_ANALYSIS_CHARS = 6000


@dataclass(frozen=True)
class AkiRuntime:
    profile: str
    username: str
    channel_name: str
    channel_id: str
    channel_aliases: tuple[str, ...]
    channel_description: str
    agent_id: str
    conversation_id: str
    state_dir: Path
    backend_dir: Path
    memory_dir: Path
    mcp_servers: tuple[str, ...]


@dataclass
class Turn:
    system_prompt: str
    system_append: str
    user_prefix: str
    user_content: str
    message: str
    seq: int
    sent_at_ms: int
    session_id: str
    payload: dict[str, Any]
    user_entry: ChannelTranscriptEntry
    conversation_user_content: str
    assistant_entry_template: ChannelTranscriptEntry | None = None


@dataclass
class ApiResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fixed_clock() -> datetime:
    """Return today's 22:00 in the machine's local timezone."""
    current = datetime.now().astimezone()
    return current.replace(
        hour=FIXED_HOUR, minute=FIXED_MINUTE, second=0, microsecond=0
    )


def append_block(base: str, block: str) -> str:
    """Match the bridge's appendBlock(): trim before choosing a separator."""
    current = str(base or "").strip()
    text = str(block or "").strip()
    if not text:
        return current
    if not current:
        return text
    return f"{current}\n\n{text}"


def prefix_user_content(content: str, prefix: str) -> str:
    """Match the bridge's prefixUserContent() for text-only user messages."""
    text = str(prefix or "").strip()
    body = str(content or "")
    if not text:
        return body
    return f"{text}\n\n{body}" if body.strip() else text


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    if len(parts) != 3:
        return text.strip()
    return parts[2].strip()


def frontmatter_description(text: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) != 3:
        return ""
    for line in parts[1].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "description":
            return value.strip()
    return ""


def load_config(profile: str, config_path: Path | None) -> AkiRuntime:
    path = config_path or Path(
        os.environ.get("AKI_CONFIG", Path.home() / ".aki" / f"{profile}.conf")
    ).expanduser()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass

    username = parser.get("gateway", "username", fallback="Master").strip() or "Master"
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    conversation_id = parser.get("gateway", "conversation", fallback="default").strip()
    channel_name = parser.get("channel", "name", fallback=profile.title()).strip()
    channel_id = parser.get("channel", "id", fallback="").strip()
    channel_id = channel_id or legacy_member_id(channel_name)
    aliases = tuple(
        item.strip()
        for item in parser.get(
            "channel", "aliases", fallback=channel_name
        ).split(",")
        if item.strip()
    )
    channel_description = parser.get(
        "channel", "description", fallback=""
    ).strip()

    backend_dir = Path(
        os.environ.get("AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend")
    ).expanduser()
    memory_dir = Path(
        os.environ.get(
            "AKI_MEMORY_DIR",
            backend_dir / "memfs" / agent_id / "memory",
        )
    ).expanduser()
    state_dir = Path(os.environ.get("AKI_STATE_DIR", Path.home() / ".aki")).expanduser()
    mcp_servers = tuple(
        item.strip()
        for item in os.environ.get("AKI_MCP_SERVERS", "tavily,local").split(",")
        if item.strip()
    )
    return AkiRuntime(
        profile=profile,
        username=username,
        channel_name=channel_name,
        channel_id=channel_id,
        channel_aliases=aliases,
        channel_description=channel_description,
        agent_id=agent_id,
        conversation_id=conversation_id or "default",
        state_dir=state_dir,
        backend_dir=backend_dir,
        memory_dir=memory_dir,
        mcp_servers=mcp_servers,
    )


def find_agent_record(runtime: AkiRuntime) -> dict[str, Any] | None:
    for path in sorted((runtime.backend_dir / "agents").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == runtime.agent_id:
            return data
    return None


def render_core_memory_fallback(memory_dir: Path) -> str:
    """Approximate Letta's core-memory projection when no compiled prompt exists."""
    if not memory_dir.is_dir():
        return ""
    order = {"human.md": 0, "memory-policy.md": 1, "persona.md": 2}
    files = [
        path
        for path in memory_dir.glob("*.md")
        if path.name != "MEMORY.md" and path.is_file()
    ]
    files.sort(key=lambda path: (order.get(path.name, 50), path.name))
    blocks: list[str] = []
    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        tag = path.stem.replace("-", "_")
        description = frontmatter_description(raw)
        body = strip_frontmatter(raw)
        lines = [f"<{tag}>"]
        if description:
            lines.append(f"<description>{description}</description>")
        lines.append(body)
        lines.append(f"</{tag}>")
        blocks.append("\n".join(lines))

    index = memory_dir / "MEMORY.md"
    if index.is_file():
        lines = ["<memory>", index.read_text(encoding="utf-8").strip()]
        children = [
            directory
            for directory in sorted(memory_dir.iterdir())
            if directory.is_dir()
            and not directory.name.startswith(".")
            and (directory / "MEMORY.md").is_file()
        ]
        if children:
            lines.append("<deferred-memory>")
            for directory in children:
                lines.append(
                    f'<directory path="{directory.name}/" '
                    f'index="{directory.name}/MEMORY.md" />'
                )
            lines.append("</deferred-memory>")
        lines.append("</memory>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def conversation_dir(runtime: AkiRuntime) -> Path:
    key = base64.b64encode(
        f"{runtime.conversation_id}:{runtime.agent_id}".encode("utf-8")
    ).decode("ascii").rstrip("=")
    return runtime.backend_dir / "conversations" / key


def message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def load_conversation_messages(
    runtime: AkiRuntime,
    *,
    max_turns: int,
    budget_tokens: int,
) -> list[dict[str, str]]:
    """Load real user/assistant turns in in_context_message_ids order."""
    directory = conversation_dir(runtime)
    try:
        conversation = json.loads(
            (directory / "conversation.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    ids = conversation.get("in_context_message_ids")
    if not isinstance(ids, list):
        return []

    by_id: dict[str, Mapping[str, Any]] = {}
    try:
        lines = (directory / "messages.jsonl").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        message = row.get("message")
        if isinstance(message, dict) and message.get("id"):
            by_id[str(message["id"])] = message

    turns: list[dict[str, str]] = []
    for message_id in ids:
        message = by_id.get(str(message_id))
        if message is None:
            continue
        role = str(message.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = message_text(message).strip()
        if text:
            turns.append({"role": role, "content": text})

    if max_turns == 0 or budget_tokens <= 0:
        return []
    selected: list[dict[str, str]] = []
    used = 0
    for message in reversed(turns):
        cost = estimate_tokens(message["content"]) + 8
        if selected and used + cost > budget_tokens:
            break
        selected.append(message)
        used += cost
        if max_turns > 0 and len(selected) >= max_turns:
            break
    selected.reverse()
    while selected and selected[0]["role"] != "user":
        selected.pop(0)
    return selected


def last_history_seq(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    highest = 0
    for line in lines[-128:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            try:
                highest = max(highest, int(row.get("seq") or 0))
            except (TypeError, ValueError):
                pass
    return highest


def load_system_prompt(runtime: AkiRuntime) -> str:
    agent = find_agent_record(runtime)
    raw_base = str((agent or {}).get("system") or "")
    base = raw_base.strip()
    compiled_path = conversation_dir(runtime) / "system-prompt.json"
    compiled: dict[str, Any] = {}
    if compiled_path.is_file():
        try:
            payload = json.loads(compiled_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                compiled = payload
        except (OSError, ValueError):
            pass

    content = compiled.get("content")
    if isinstance(content, str) and content.strip():
        raw_hash = hashlib.sha256(raw_base.encode("utf-8")).hexdigest()
        if raw_hash == str(compiled.get("rawSystemHash") or ""):
            return content.strip()

    core = compiled.get("coreMemory")
    if base and "{CORE_MEMORY}" in base and isinstance(core, str) and core.strip():
        return base.replace("{CORE_MEMORY}", core)

    core = render_core_memory_fallback(runtime.memory_dir)
    if "{CORE_MEMORY}" in base:
        return base.replace("{CORE_MEMORY}", core)
    if core:
        return append_block(base, core)
    return base


def read_channel_description(conf_path: Path) -> str:
    if not conf_path.is_file():
        return ""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
        return parser.get("channel", "description", fallback="").strip()
    except (OSError, configparser.Error):
        return ""


def load_identity_members(runtime: AkiRuntime) -> list[ChannelMember]:
    registry_path = runtime.state_dir / "identities.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    members = payload.get("members") if isinstance(payload, dict) else None
    if not isinstance(members, dict):
        return []

    views: list[ChannelMember] = []
    for item in members.values():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        member_id = str(item.get("id") or "").strip()
        if not name or not member_id:
            continue
        if member_id == runtime.channel_id or name.casefold() == runtime.channel_name.casefold():
            continue
        if normalize_member_kind(item.get("kind")) != MEMBER_KIND_AGENT:
            continue
        conf_path = runtime.state_dir / f"{name.casefold()}.conf"
        views.append(
            ChannelMember(
                id=member_id,
                name=name,
                aliases=tuple(str(alias) for alias in item.get("aliases") or ()),
                description=truncate_member_description(
                    read_channel_description(conf_path)
                ),
                kind=MEMBER_KIND_AGENT,
            )
        )
    return views


def self_member(runtime: AkiRuntime) -> ChannelMember:
    return ChannelMember(
        id=runtime.channel_id,
        name=runtime.channel_name,
        aliases=runtime.channel_aliases,
        description=truncate_member_description(runtime.channel_description),
        is_self=True,
        kind=MEMBER_KIND_AGENT,
    )


def find_latest_history(runtime: AkiRuntime, filename: str) -> Path | None:
    candidates = [
        path
        for path in (runtime.state_dir / "channel-history").glob(f"*/{filename}")
        if path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def transcript_entry(row: Mapping[str, Any]) -> ChannelTranscriptEntry | None:
    try:
        seq = int(row.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    text = str(row.get("text") or "")
    if seq <= 0 or not text:
        return None
    attachments: list[ChannelImageAttachment] = []
    raw_attachments = row.get("attachments")
    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            attachments.append(
                ChannelImageAttachment(
                    name=str(item.get("name") or ""),
                    path=str(item.get("path") or ""),
                    mime=str(item.get("mime") or ""),
                    size=int(item.get("size") or 0),
                    index=int(item.get("index") or 0),
                    missing=bool(item.get("missing")),
                )
            )
    return ChannelTranscriptEntry(
        seq=seq,
        sender=str(row.get("sender") or ""),
        sender_id=str(row.get("sender_id") or ""),
        text=text,
        from_agent=bool(row.get("from_agent")),
        timestamp=str(row.get("timestamp") or ""),
        sent_at=int(row.get("sent_at") or 0),
        message_id=str(row.get("message_id") or ""),
        phase=str(row.get("phase") or "final"),
        arrived_while_busy=bool(row.get("arrived_while_busy")),
        attachments=tuple(attachments),
    )


def read_history(path: Path | None, limit: int) -> list[ChannelTranscriptEntry]:
    if path is None or limit == 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[ChannelTranscriptEntry] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        entry = transcript_entry(row)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: (item.sent_at or 0, item.seq))
    return entries[-limit:] if limit > 0 else entries


def last_user_sender(entries: Sequence[ChannelTranscriptEntry]) -> tuple[str, str]:
    for entry in reversed(entries):
        if not entry.from_agent:
            return entry.sender, entry.sender_id
    return "", ""


def load_user_identity(runtime: AkiRuntime) -> tuple[str, str]:
    registry_path = runtime.state_dir / "identities.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return runtime.username, ""
    members = payload.get("members") if isinstance(payload, dict) else None
    if not isinstance(members, dict):
        return runtime.username, ""
    for item in members.values():
        if not isinstance(item, dict):
            continue
        if normalize_member_kind(item.get("kind")) != MEMBER_KIND_USER:
            continue
        name = str(item.get("name") or "").strip()
        member_id = str(item.get("id") or "").strip()
        if name:
            return name, member_id
    return runtime.username, ""


def load_solo_sessions(runtime: AkiRuntime, limit: int) -> list[SoloMemorySession]:
    path = runtime.state_dir / "solo-memory" / f"{runtime.profile}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw_sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(raw_sessions, list):
        return []
    sessions = [
        SoloMemorySession.from_dict(item)
        for item in raw_sessions
        if isinstance(item, dict)
    ]
    sessions.sort(key=lambda item: (item.started_at, item.session_id))
    if limit == 0:
        return []
    if limit < 0:
        return sessions
    return sessions[-limit:]


def render_envelope(
    *,
    sender: str,
    sender_id: str,
    text: str,
    seq: int,
    sent_at_ms: int,
) -> str:
    when = datetime.fromtimestamp(sent_at_ms / 1000).strftime("%Y-%m-%d %H:%M")
    lines = [
        "[akid MQTT channel message]",
        f"message-time: {when}",
        f"seq: {seq}",
    ]
    if sender and sender_id:
        lines.append(f"sender: {sender} ({sender_id})")
    elif sender:
        lines.append(f"sender: {sender}")
    if sender_id:
        lines.append(f"sender_id: {sender_id}")
    lines.extend(["source: user", "phase: final", "", text])
    return "\n".join(lines)


def load_ledger(runtime: AkiRuntime) -> str:
    path = runtime.state_dir / "ledger" / f"{runtime.profile}.json"
    ledger = LifeLedger(path)
    try:
        return render_ledger_block(ledger.records())
    except Exception:
        return ""


async def collect_snapshot(scan: bool, fixed_at: datetime) -> LiveContextSnapshot:
    if not scan:
        return LiveContextSnapshot(captured_at=fixed_at)
    options = LiveContextOptions(
        history_limit=20,
        process_limit=5,
        timeout=2.5,
        screenshot_timeout=0.0,
        include_screenshot=False,
    )
    try:
        return await collect_live_context(options, now=fixed_at)
    except Exception:
        return LiveContextSnapshot(captured_at=fixed_at)


def build_overlay(
    runtime: AkiRuntime,
    *,
    mode: str,
    message: str,
    group_entries: Sequence[ChannelTranscriptEntry],
    solo_entries: Sequence[ChannelTranscriptEntry],
    solo_sessions: Sequence[SoloMemorySession],
    snapshot: LiveContextSnapshot,
    session_id: str,
    username: str,
) -> tuple[str, str]:
    self_view = self_member(runtime)
    members = [self_view, *load_identity_members(runtime)]
    emoji_entries: list[EmojiEntry] = load_emoji_entries(
        ROOT / "assets" / "emoji", runtime.profile
    )

    stable_parts = [
        render_channel_identity(self_view),
        render_channel_roster(self_member=self_view, members=members),
        render_tool_ownership_prompt(runtime.profile),
        render_channel_policy(
            user_name=username,
            emoji_entries=emoji_entries,
        ),
        render_channel_wake_routing(username),
    ]
    system_append = "\n\n".join(part for part in stable_parts if part.strip())

    mid_parts: list[str] = []
    solo_memories_block = render_solo_memories(
        solo_sessions,
        budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
    )
    if solo_memories_block:
        mid_parts.append(solo_memories_block)

    group_block = render_channel_transcript(
        group_entries,
        budget_tokens=CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    )
    if group_block:
        mid_parts.append(group_block)

    if mode == "solo":
        scene = load_solo_scene(
            runtime.profile,
            username=username,
            repo_root=ROOT,
        )
        mid_parts.append(
            render_solo_mode(
                member_name=runtime.channel_name,
                user_name=username,
                session_id=session_id,
                scene=scene,
            )
        )
        solo_block = render_solo_transcript(
            solo_entries,
            budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
        )
        if solo_block:
            mid_parts.append(solo_block)

    mid_parts.append(
        render_live_context(
            snapshot,
            budget_tokens=LIVE_CONTEXT_BUDGET_TOKENS,
            ledger_block=load_ledger(runtime),
            memory_dir=runtime.memory_dir,
            mcp_servers=runtime.mcp_servers,
            mcp_tools=(),
            mcp_native_tools_enabled=True,
        )
    )

    mid_parts.append(
        render_turn_control(
            self_member=self_view,
            members=members,
            message=message,
            initiation_remaining=3,
            initiation_limit=3,
            from_agent=False,
            user_name=username,
            wait_labels=(),
            solo=mode == "solo",
        )
    )
    mid_parts.append(render_user_format_guard(username))
    user_prefix = "\n\n".join(part for part in mid_parts if part.strip())
    return system_append, user_prefix


def openai_tools(profile: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in tools_for_profile(profile):
        parameters = tool.get("parameters")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": parameters if isinstance(parameters, dict) else {},
                },
            }
        )
    return [tool for tool in tools if tool["function"]["name"]]


def build_turn(
    runtime: AkiRuntime,
    *,
    message: str,
    mode: str,
    conversation_messages: Sequence[Mapping[str, str]],
    group_entries: Sequence[ChannelTranscriptEntry],
    solo_entries: Sequence[ChannelTranscriptEntry],
    solo_sessions: Sequence[SoloMemorySession],
    snapshot: LiveContextSnapshot,
    session_id: str,
    model: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    tools: bool,
    thinking: bool,
    reasoning_budget_tokens: int,
    reasoning_budget_message: str,
    stream: bool,
    seq_base: int = 0,
) -> Turn:
    fixed_at = fixed_clock()
    sent_at_ms = int(fixed_at.timestamp() * 1000)
    active_entries = solo_entries if mode == "solo" else group_entries
    seq = max(
        (entry.seq for entry in active_entries),
        default=int(seq_base or 0),
    ) + 1
    sender, sender_id = last_user_sender(active_entries)
    if not sender:
        sender, sender_id = load_user_identity(runtime)
    sender = sender or runtime.username

    system_append, user_prefix = build_overlay(
        runtime,
        mode=mode,
        message=message,
        group_entries=group_entries,
        solo_entries=solo_entries,
        solo_sessions=solo_sessions,
        snapshot=snapshot,
        session_id=session_id,
        username=runtime.username,
    )
    system_prompt = append_block(load_system_prompt(runtime), system_append)
    envelope = render_envelope(
        sender=sender,
        sender_id=sender_id,
        text=message,
        seq=seq,
        sent_at_ms=sent_at_ms,
    )
    user_content = prefix_user_content(envelope, user_prefix)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *[dict(item) for item in conversation_messages],
            {"role": "user", "content": user_content},
        ],
        "stream": bool(stream),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    if thinking:
        payload["reasoning_budget_tokens"] = max(0, int(reasoning_budget_tokens))
        payload["reasoning_budget_message"] = reasoning_budget_message
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = openai_tools(runtime.profile)

    user_entry = ChannelTranscriptEntry(
        seq=seq,
        sender=sender,
        sender_id=sender_id,
        text=message,
        from_agent=False,
        timestamp=fixed_at.isoformat(timespec="seconds"),
        sent_at=sent_at_ms,
        phase="final",
    )
    assistant_entry = ChannelTranscriptEntry(
        seq=seq + 1,
        sender=runtime.channel_name,
        sender_id=runtime.channel_id,
        text="",
        from_agent=True,
        timestamp=fixed_at.isoformat(timespec="seconds"),
        sent_at=sent_at_ms,
        phase="final",
    )
    return Turn(
        system_prompt=system_prompt,
        system_append=system_append,
        user_prefix=user_prefix,
        user_content=user_content,
        message=message,
        seq=seq,
        sent_at_ms=sent_at_ms,
        session_id=session_id,
        payload=payload,
        user_entry=user_entry,
        conversation_user_content=envelope,
        assistant_entry_template=assistant_entry,
    )


def request_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    api_key: str,
) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("API returned a non-object JSON response")
    return data


def resolve_model(base_url: str, requested: str, timeout: float) -> str:
    if requested.strip():
        return requested.strip()
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError(f"cannot list models at {url}: {exc}") from exc
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    raise RuntimeError(f"no model id in {url} response")


def parse_sse_line(line: str) -> Mapping[str, Any] | None:
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def merge_tool_delta(
    calls: dict[int, dict[str, Any]], delta: Any
) -> None:
    if not isinstance(delta, list):
        return
    for item in delta:
        if not isinstance(item, dict):
            continue
        index = int(item.get("index") or 0)
        call = calls.setdefault(
            index,
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if item.get("id"):
            call["id"] = str(item["id"])
        function = item.get("function")
        if isinstance(function, dict):
            if function.get("name"):
                call["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                call["function"]["arguments"] += str(function["arguments"])


def stream_chat(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    api_key: str,
    show_reasoning: bool,
) -> ApiResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    result = ApiResult()
    calls: dict[int, dict[str, Any]] = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace")
            event = parse_sse_line(line)
            if event is None:
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                result.usage = usage
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                result.reasoning += reasoning
                if show_reasoning:
                    print(reasoning, end="", flush=True)
            content = delta.get("content")
            if isinstance(content, str) and content:
                result.text += content
                print(content, end="", flush=True)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        result.text += part["text"]
                        print(part["text"], end="", flush=True)
            merge_tool_delta(calls, delta.get("tool_calls"))
    if result.text or result.reasoning:
        print()
    result.tool_calls = [calls[index] for index in sorted(calls)]
    return result


def non_stream_chat(
    url: str,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    api_key: str,
) -> ApiResult:
    data = request_json(url, payload, timeout=timeout, api_key=api_key)
    result = ApiResult()
    usage = data.get("usage")
    if isinstance(usage, dict):
        result.usage = usage
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    result.text = content
                elif isinstance(content, list):
                    result.text = "".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                if isinstance(reasoning, str):
                    result.reasoning = reasoning
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    result.tool_calls = [
                        call for call in calls if isinstance(call, dict)
                    ]
    return result


def api_url(base_url: str, path: str) -> str:
    root = base_url.rstrip("/")
    if path.startswith("/"):
        return root + path
    return root + "/" + path


def print_turn_summary(turn: Turn, result: ApiResult) -> None:
    history_messages = max(0, len(turn.payload.get("messages") or []) - 2)
    thinking_budget = turn.payload.get("reasoning_budget_tokens")
    thinking_label = (
        f"thinking {thinking_budget}"
        if isinstance(thinking_budget, int)
        else "thinking off"
    )
    print(
        f"\n[turn {turn.seq} | history {history_messages} msgs | "
        f"{thinking_label} | "
        f"system {len(turn.system_prompt):,} chars | "
        f"stable {len(turn.system_append):,} chars | "
        f"overlay prefix {len(turn.user_prefix):,} chars]",
        file=sys.stderr,
    )
    if result.tool_calls:
        print("[tool calls returned, not executed]", file=sys.stderr)
        for call in result.tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            name = str((function or {}).get("name") or "")
            arguments = str((function or {}).get("arguments") or "")
            print(f"  - {name} {arguments}", file=sys.stderr)
    if result.reasoning:
        print(
            f"[reasoning {len(result.reasoning):,} chars "
            f"(~{estimate_tokens(result.reasoning):,} tokens)]",
            file=sys.stderr,
        )
    usage = result.usage or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        if not isinstance(total_tokens, int):
            total_tokens = prompt_tokens + completion_tokens
        details = usage.get("prompt_tokens_details")
        cached = (
            details.get("cached_tokens")
            if isinstance(details, dict)
            else None
        )
        parts = [
            f"context {total_tokens:,} tokens",
            f"prompt {prompt_tokens:,}",
            f"completion {completion_tokens:,}",
        ]
        if isinstance(cached, int):
            parts.append(f"cached {cached:,}")
        print("[" + " | ".join(parts) + "]", file=sys.stderr)
    else:
        estimated_prompt = sum(
            estimate_tokens(str(message.get("content") or ""))
            for message in turn.payload.get("messages") or []
        )
        estimated_completion = estimate_tokens(result.text + result.reasoning)
        print(
            f"[context ~{estimated_prompt + estimated_completion:,} tokens "
            f"(estimated) | prompt ~{estimated_prompt:,} | "
            f"completion ~{estimated_completion:,}]",
            file=sys.stderr,
        )


def run_turn(
    runtime: AkiRuntime,
    turn: Turn,
    *,
    base_url: str,
    api_key: str,
    timeout: float,
    stream: bool,
    show_reasoning: bool,
) -> ApiResult:
    url = api_url(base_url, "chat/completions")
    if stream:
        return stream_chat(
            url,
            turn.payload,
            timeout=timeout,
            api_key=api_key,
            show_reasoning=show_reasoning,
        )
    result = non_stream_chat(url, turn.payload, timeout=timeout, api_key=api_key)
    if result.text:
        print(result.text)
    if show_reasoning and result.reasoning:
        print(result.reasoning)
    return result


def run_analysis_pass(
    message: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    thinking: bool,
    reasoning_budget_tokens: int,
    reasoning_budget_message: str,
    timeout: float,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "stream": False,
        "max_tokens": min(max(256, max_tokens), 4096),
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "chat_template_kwargs": {"enable_thinking": bool(thinking)},
    }
    if thinking:
        payload["reasoning_budget_tokens"] = max(0, int(reasoning_budget_tokens))
        payload["reasoning_budget_message"] = reasoning_budget_message
    result = non_stream_chat(
        api_url(base_url, "chat/completions"),
        payload,
        timeout=timeout,
        api_key=api_key,
    )
    analysis = (result.text or result.reasoning).strip()
    if len(analysis) > MAX_ANALYSIS_CHARS:
        analysis = analysis[:MAX_ANALYSIS_CHARS].rstrip() + "\n...[truncated]"
    return analysis


def inject_analysis(turn: Turn, analysis: str) -> None:
    messages = turn.payload.get("messages")
    if not analysis or not isinstance(messages, list) or not messages:
        return
    system = messages[0]
    if not isinstance(system, dict) or system.get("role") != "system":
        return
    base = str(system.get("content") or "").rstrip()
    system["content"] = append_block(
        base,
        ANALYSIS_INJECTION.format(analysis=analysis),
    )


def append_exchange(
    entries: list[ChannelTranscriptEntry],
    conversation_messages: list[dict[str, str]],
    turn: Turn,
    result: ApiResult,
) -> None:
    entries.append(turn.user_entry)
    conversation_messages.append(
        {"role": "user", "content": turn.conversation_user_content}
    )
    if turn.assistant_entry_template is None:
        return
    text = result.text.strip()
    if not text:
        return
    entries.append(
        ChannelTranscriptEntry(
            seq=turn.assistant_entry_template.seq,
            sender=turn.assistant_entry_template.sender,
            sender_id=turn.assistant_entry_template.sender_id,
            text=text,
            from_agent=True,
            timestamp=turn.assistant_entry_template.timestamp,
            sent_at=turn.assistant_entry_template.sent_at,
            phase="final",
        )
    )
    conversation_messages.append({"role": "assistant", "content": text})


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="aki")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=os.environ.get("AKI_LOCAL_API_KEY", "local"))
    parser.add_argument("--model", default="")
    parser.add_argument("--message", "-m", default="")
    parser.add_argument("--mode", choices=("group", "solo"), default="solo")
    parser.add_argument(
        "--history",
        type=int,
        default=20,
        help="Real group transcript entries to include; 0 disables",
    )
    parser.add_argument(
        "--solo-memories",
        type=int,
        default=2,
        help="Recent solo memory sessions; 0 disables, -1 includes all",
    )
    parser.add_argument(
        "--no-live-scan",
        action="store_true",
        help="Only render the fixed 22:00 clock, without probing processes/windows",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Ignore the real conversation and group history; start clean "
            "(solo memories still follow --solo-memories)"
        ),
    )
    parser.add_argument(
        "--conversation-turns",
        type=int,
        default=64,
        help="Maximum real conversation messages to load; 0 disables",
    )
    parser.add_argument(
        "--conversation-budget-tokens",
        type=int,
        default=48000,
        help="Token budget for loaded real conversation messages",
    )
    parser.add_argument("--no-tools", action="store_true", help="Omit Aki's tool schemas")
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable Qwen thinking; default is on with a 1536-token budget",
    )
    parser.add_argument(
        "--reasoning-budget-tokens",
        type=int,
        default=DEFAULT_REASONING_BUDGET_TOKENS,
        help="Thinking budget passed as reasoning_budget_tokens",
    )
    parser.add_argument(
        "--reasoning-budget-message",
        default=DEFAULT_REASONING_BUDGET_MESSAGE,
        help="Message injected when the thinking budget is exhausted",
    )
    parser.add_argument(
        "--deep-think",
        action="store_true",
        help=(
            "Run a separate minimal-prompt analysis pass and inject its result "
            "into the Aki reply pass"
        ),
    )
    parser.add_argument(
        "--show-analysis",
        action="store_true",
        help="Print the --deep-think analysis to stderr",
    )
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--show-reasoning", action="store_true")
    parser.add_argument("--show-prefix", action="store_true")
    parser.add_argument("--dump-request", type=Path)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runtime = load_config(args.profile, args.config)
    try:
        model = resolve_model(args.base_url, args.model, min(args.timeout, 10.0))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fixed_at = fixed_clock()
    snapshot = asyncio.run(collect_snapshot(not args.no_live_scan, fixed_at))
    if args.fresh:
        conversation_messages: list[dict[str, str]] = []
        group_entries: list[ChannelTranscriptEntry] = []
        solo_entries: list[ChannelTranscriptEntry] = []
    else:
        conversation_messages = load_conversation_messages(
            runtime,
            max_turns=args.conversation_turns,
            budget_tokens=args.conversation_budget_tokens,
        )
        group_entries = read_history(
            find_latest_history(runtime, "aki.jsonl"), args.history
        )
        # The live solo transcript starts empty for this process; prior solo
        # content is already represented by the real conversation messages.
        solo_entries = []
    solo_sessions = load_solo_sessions(runtime, args.solo_memories)
    session_id = f"solo-{uuid.uuid4().hex}"
    solo_seq_base = (
        0 if args.fresh else last_history_seq(
            find_latest_history(runtime, "aki.solo.jsonl")
        )
    )
    group_seq_base = (
        0 if args.fresh else last_history_seq(
            find_latest_history(runtime, "aki.jsonl")
        )
    )

    def make_turn(message: str) -> Turn:
        return build_turn(
            runtime,
            message=message,
            mode=args.mode,
            conversation_messages=conversation_messages,
            group_entries=group_entries,
            solo_entries=solo_entries,
            solo_sessions=solo_sessions,
            snapshot=snapshot,
            session_id=session_id,
            model=model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            top_p=args.top_p,
            top_k=args.top_k,
            tools=not args.no_tools,
            thinking=not args.no_thinking,
            reasoning_budget_tokens=args.reasoning_budget_tokens,
            reasoning_budget_message=args.reasoning_budget_message,
            stream=not args.no_stream,
            seq_base=(
                solo_seq_base if args.mode == "solo" else group_seq_base
            ),
        )

    def execute(message: str) -> None:
        turn = make_turn(message)
        if args.deep_think:
            try:
                analysis = run_analysis_pass(
                    message,
                    base_url=args.base_url,
                    api_key=args.api_key,
                    model=model,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    top_k=args.top_k,
                    thinking=not args.no_thinking,
                    reasoning_budget_tokens=args.reasoning_budget_tokens,
                    reasoning_budget_message=args.reasoning_budget_message,
                    timeout=args.timeout,
                )
            except Exception as exc:
                print(
                    f"[analysis failed: {exc}; continuing without it]",
                    file=sys.stderr,
                )
                analysis = ""
            if analysis:
                inject_analysis(turn, analysis)
                print(
                    f"[analysis {len(analysis):,} chars]",
                    file=sys.stderr,
                )
                if args.show_analysis:
                    print("=== analysis ===", file=sys.stderr)
                    print(analysis, file=sys.stderr)
            else:
                print(
                    "[analysis empty; continuing without injection]",
                    file=sys.stderr,
                )
        if args.show_prefix:
            print("=== system_append ===", file=sys.stderr)
            print(turn.system_append, file=sys.stderr)
            print("=== user_message_prefix ===", file=sys.stderr)
            print(turn.user_prefix, file=sys.stderr)
            print("=== user message ===", file=sys.stderr)
            print(turn.user_content, file=sys.stderr)
        if args.dump_request:
            args.dump_request.parent.mkdir(parents=True, exist_ok=True)
            args.dump_request.write_text(
                json.dumps(turn.payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        result = run_turn(
            runtime,
            turn,
            base_url=args.base_url,
            api_key=args.api_key,
            timeout=args.timeout,
            stream=not args.no_stream,
            show_reasoning=args.show_reasoning,
        )
        print_turn_summary(turn, result)
        if args.mode == "solo":
            append_exchange(
                solo_entries, conversation_messages, turn, result
            )
        else:
            append_exchange(
                group_entries, conversation_messages, turn, result
            )

    print(
        f"Aki local turn | model={model} | mode={args.mode} | "
        f"time={fixed_at.strftime('%Y-%m-%d %H:%M %Z')} | "
        f"thinking={'off' if args.no_thinking else args.reasoning_budget_tokens} | "
        f"conversation={len(conversation_messages)} msgs | "
        f"group={len(group_entries)} entries | "
        f"{'fresh session' if args.fresh else 'real conversation'}",
        file=sys.stderr,
    )
    if args.message:
        try:
            execute(args.message)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
            return 1
        except urllib.error.URLError as exc:
            print(f"connection error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    print("输入消息开始对话；/exit 退出。", file=sys.stderr)
    while True:
        try:
            message = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return 0
        if not message:
            continue
        if message in ("/exit", "/quit", "/q"):
            return 0
        try:
            execute(message)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"connection error: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
