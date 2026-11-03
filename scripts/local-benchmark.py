#!/usr/bin/env python3
"""Benchmark one profile's local solo model with the live solo context.

The benchmark deliberately does not call the channel, Letta bridge, or the
akid gateway.  It reads the current profile/solo-conversation/solo-memory/
channel-history state, assembles the same stable ``system_append`` and
volatile ``user_message_prefix`` blocks as ``engine/akid/turns.py``, then makes
an isolated OpenAI-compatible streaming request to the profile's solo local
backend.

Before each case it prints a ``estimate_tokens`` breakdown for the system,
overlay, history, current user message, and tool schemas, followed by the test
case definition. Assistant content deltas are printed and flushed as they
arrive; reasoning deltas can be enabled with ``--stream-reasoning``. Full
prompts are never printed to the terminal; use ``--dump-request-dir`` when the
raw request is needed.

No live state is written.  If the configured backend is not already reachable,
the script may start it with ``SoloBackendManager`` using temporary state/log
files, and stops it again on exit.  The generated coding answers are checked in
a short-lived subprocess; use ``--no-execute`` to disable that.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import re
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.aki_config import (  # noqa: E402
    AkiConfig,
    discover_profiles,
    load_config,
    profile_config_path,
    profile_from_config_path,
)
from engine.channel_history import channel_history_key, channel_history_path  # noqa: E402
from engine.akid.constants import (  # noqa: E402
    ACQUIRE_EXECUTION_LOCK_TOOL,
    SEARCH_CHAT_HISTORY_TOOL,
    TOKIFACTOR_FILE_TOOL,
    WAIT_USER_TOOL_SCHEMA,
)
from engine.emoji_catalog import EmojiEntry, load_emoji_entries  # noqa: E402
from engine.life_ledger import LifeLedger, render_ledger_block  # noqa: E402
from engine.live_context import (  # noqa: E402
    ChannelImageAttachment,
    ChannelMember,
    ChannelTranscriptEntry,
    DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    LiveContextOptions,
    SOLO_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
    collect_live_context,
    estimate_tokens,
    render_channel_identity,
    render_channel_transcript,
    render_live_context,
    render_runtime_context,
    render_solo_memories,
    render_solo_mode,
    render_solo_policy,
    render_solo_wait_user,
    render_user_format_guard,
)
from engine.member_profile import MEMBER_KIND_AGENT, MEMBER_KIND_USER  # noqa: E402
from engine.solo_backend import (  # noqa: E402
    SoloBackendError,
    SoloBackendManager,
    SoloRoute,
    default_state_path,
    discover_solo_route,
)
from engine.solo_memory import (  # noqa: E402
    SOLO_MEMORY_BUDGET_TOKENS,
    SoloMemorySession,
)
from engine.solo_mode import (  # noqa: E402
    conversation_directory,
    load_solo_scene,
    load_solo_session,
    solo_conversation_id,
)
from engine.tool_ownership import (  # noqa: E402
    render_tool_ownership_prompt,
    tools_for_profile,
)


SCHEMA = "aki.local-benchmark.v1"
DEFAULT_PROFILE = "aki"
DEFAULT_REASONING_BUDGET_TOKENS = 1536
DEFAULT_HISTORY_BUDGET_TOKENS = 64 * 1024
MAX_EXECUTION_SECONDS = 5.0
MAX_CODE_BYTES = 128 * 1024
ALLOWED_IMPORTS = {
    "bisect",
    "collections",
    "dataclasses",
    "functools",
    "heapq",
    "itertools",
    "math",
    "re",
    "statistics",
    "string",
    "typing",
}
BANNED_CALL_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "vars",
}


@dataclass(frozen=True)
class BenchmarkCase:
    key: str
    kind: str
    prompt: str
    max_tokens: int
    function: str = ""
    tests: tuple[tuple[Any, Any], ...] = ()


@dataclass(frozen=True)
class StreamResult:
    text: str
    reasoning: str
    tool_calls: tuple[dict[str, Any], ...]
    usage: dict[str, Any]
    ttft_s: float | None
    first_content_s: float | None
    elapsed_s: float
    chunks: int
    timings: dict[str, Any]
    finish_reason: str = ""


@dataclass(frozen=True)
class CodeCheck:
    code: str
    code_chars: int
    code_lines: int
    code_tokens: int
    compile_ok: bool
    tests_passed: int
    tests_total: int
    tests_ok: bool
    execution_ms: float
    executed: bool = True
    error: str = ""


@dataclass
class SoloContext:
    profile: str
    username: str
    display_name: str
    user_id: str
    member: ChannelMember
    scene: str
    base_system: str
    emoji_entries: list[EmojiEntry]
    conversation_messages: list[dict[str, Any]]
    group_entries: list[ChannelTranscriptEntry]
    solo_entries: list[ChannelTranscriptEntry]
    solo_sessions: list[SoloMemorySession]
    session_id: str
    session_active: bool
    next_seq: int
    online_profiles: frozenset[str] = frozenset()
    history_messages: int = 0
    history_tokens: int = 0


@dataclass
class BuiltRequest:
    payload: dict[str, Any]
    system_prompt: str
    system_append: str
    user_prefix: str
    user_content: str
    context_stats: dict[str, Any]
    token_parts: list[dict[str, Any]]
    total_tokens: int


@dataclass
class ResolvedBackend:
    route: SoloRoute
    source: str
    endpoint: str
    manager: SoloBackendManager | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None

    async def close(self) -> None:
        if self.manager is not None:
            await self.manager.close()
        if self.temporary is not None:
            self.temporary.cleanup()


@dataclass
class BenchmarkResult:
    case: str
    repeat: int
    result: StreamResult
    prompt_tokens: int
    completion_tokens: int
    prompt_tokens_estimated: bool
    completion_tokens_estimated: bool
    generation_tokens_per_s: float
    wall_tokens_per_s: float
    code: CodeCheck | None = None
    error: str = ""


BENCHMARK_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        key="generation",
        kind="generation",
        prompt=(
            "今天总算忙完了，我靠在你旁边，不太想说话。外面还在下雨，"
            "屋里安静得只剩雨声。你忙你的也行，就是别离我太远。"
            "今晚我想多赖你一会儿，可以吗？"
        ),
        max_tokens=2048,
    ),
    BenchmarkCase(
        key="coding-merge-intervals",
        kind="coding",
        prompt=(
            "我窝在你旁边整理这周的日程，几个时间段重叠了，脑子有点乱。"
            "你帮我写个小函数吧，函数名就叫 merge_intervals，输入是 "
            "[[start, end], ...]，把重叠或相邻的区间合起来，结果按 start 升序。"
            "写完直接把 ```python 代码块发我，别讲原理，也别改我的输入列表。"
        ),
        max_tokens=4096,
        function="merge_intervals",
        tests=(
            (([],), []),
            (([[1, 3], [2, 6], [8, 10], [15, 18]],), [[1, 6], [8, 10], [15, 18]]),
            (([[1, 4], [4, 5]],), [[1, 5]]),
            (([[1, 4], [0, 4]],), [[0, 4]]),
            (([[1, 2], [3, 4]],), [[1, 2], [3, 4]]),
            (([[2, 3], [2, 2], [3, 3], [1, 4]],), [[1, 4]]),
        ),
    ),
    BenchmarkCase(
        key="coding-count-islands",
        kind="coding",
        prompt=(
            "请实现 `def count_islands(grid):`，使用 Python 标准库即可。"
            "grid 是由字符串 '0' 和 '1' 组成的二维列表，'1' 表示陆地，"
            "上下左右相邻算同一座岛。返回岛屿数量。不要修改输入；"
            "不要解释，不要调用工具，只输出一个 ```python 代码块。"
        ),
        max_tokens=4096,
        function="count_islands",
        tests=(
            (([],), 0),
            (([["0"]],), 0),
            (([["1"]],), 1),
            (
                (
                    [
                        ["1", "1", "0", "0", "0"],
                        ["1", "1", "0", "0", "0"],
                        ["0", "0", "1", "0", "0"],
                        ["0", "0", "0", "1", "1"],
                    ],
                ),
                3,
            ),
            (
                (
                    [
                        ["1", "1", "0", "0", "0"],
                        ["1", "0", "1", "1", "0"],
                        ["0", "0", "1", "0", "1"],
                        ["0", "0", "0", "1", "1"],
                    ],
                ),
                3,
            ),
            (
                (
                    [
                        ["1", "0", "1"],
                        ["0", "1", "0"],
                        ["1", "0", "1"],
                    ],
                ),
                5,
            ),
        ),
    ),
)


def append_block(current: str, block: str) -> str:
    """Match the bridge's appendBlock(): trim before choosing a separator."""
    left = str(current or "").strip()
    right = str(block or "").strip()
    if not right:
        return left
    if not left:
        return right
    return f"{left}\n\n{right}"


def prefix_user_content(content: str, prefix: str) -> str:
    text = str(prefix or "").strip()
    body = str(content or "")
    if not text:
        return body
    return f"{text}\n\n{body}" if body.strip() else text


def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def default_memory_dir(agent_id: str) -> Path:
    configured = os.environ.get("AKI_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return default_backend_dir() / "memfs" / agent_id / "memory"


def profile_state_dir(config: AkiConfig) -> Path:
    return Path(config.path).expanduser().parent


def channel_history_root(config: AkiConfig) -> Path:
    return profile_state_dir(config) / "channel-history"


def profile_history_path(config: AkiConfig, *, solo: bool) -> Path:
    key = channel_history_key(
        host=config.mqtt_host,
        port=config.mqtt_port,
        topic=config.mqtt_topic,
        magic=config.mqtt_magic,
    )
    return channel_history_path(
        channel_history_root(config),
        key=key,
        profile=config.profile,
        solo=solo,
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_solo_memory_sessions(config: AkiConfig) -> list[SoloMemorySession]:
    path = profile_state_dir(config) / "solo-memory" / f"{config.profile}.json"
    payload = read_json(path)
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list):
        return []
    sessions = [
        SoloMemorySession.from_dict(item)
        for item in raw_sessions
        if isinstance(item, dict)
    ]
    sessions.sort(key=lambda item: (item.started_at, item.session_id))
    return sessions


def load_identities(config: AkiConfig) -> tuple[str, str]:
    payload = read_json(profile_state_dir(config) / "identities.json")
    members = payload.get("members")
    if not isinstance(members, dict):
        return config.username, ""
    for item in members.values():
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().casefold() != MEMBER_KIND_USER:
            continue
        name = str(item.get("name") or "").strip()
        member_id = str(item.get("id") or "").strip()
        if name:
            return name, member_id
    return config.username, ""


def _attachment(value: Any) -> ChannelImageAttachment | None:
    if not isinstance(value, dict):
        return None
    try:
        index = int(value.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    return ChannelImageAttachment(
        name=str(value.get("name") or ""),
        path=str(value.get("path") or ""),
        mime=str(value.get("mime") or ""),
        size=int(value.get("size") or 0),
        index=index,
        missing=bool(value.get("missing")),
    )


def read_entries(path: Path) -> list[ChannelTranscriptEntry]:
    if not path.is_file():
        return []
    by_seq: dict[int, ChannelTranscriptEntry] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        if seq <= 0:
            continue
        attachments = tuple(
            item
            for value in row.get("attachments") or ()
            if (item := _attachment(value)) is not None
        )
        by_seq[seq] = ChannelTranscriptEntry(
            seq=seq,
            sender=str(row.get("sender") or ""),
            sender_id=str(row.get("sender_id") or ""),
            text=str(row.get("text") or ""),
            from_agent=bool(row.get("from_agent")),
            timestamp=str(row.get("timestamp") or ""),
            sent_at=int(row.get("sent_at") or 0),
            message_id=str(row.get("message_id") or ""),
            phase=str(row.get("phase") or "final"),
            arrived_while_busy=bool(row.get("arrived_while_busy")),
            attachments=attachments,
        )
    return [by_seq[seq] for seq in sorted(by_seq)]


def self_member(config: AkiConfig) -> ChannelMember:
    channel = config.channel
    return ChannelMember(
        id=channel.id or config.agent_id,
        name=channel.name or config.profile.title(),
        aliases=tuple(channel.aliases),
        description=channel.description,
        is_self=True,
        kind=MEMBER_KIND_AGENT,
    )


def find_agent_record(config: AkiConfig) -> dict[str, Any] | None:
    folder = default_backend_dir() / "agents"
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        payload = read_json(path)
        if payload.get("id") == config.agent_id:
            return payload
    return None


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) == 3 else text.strip()


def _frontmatter_description(text: str) -> str:
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


def render_worktree_core_memory_fallback(memory_dir: Path) -> str:
    if not memory_dir.is_dir():
        return ""
    order = {"human.md": 0, "memory-policy.md": 1, "persona.md": 2}
    paths = [
        path
        for path in memory_dir.glob("*.md")
        if path.is_file() and path.name != "MEMORY.md"
    ]
    paths.sort(key=lambda path: (order.get(path.name, 50), path.name))
    blocks: list[str] = []
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tag = path.stem.replace("-", "_")
        description = _frontmatter_description(raw)
        body = _strip_frontmatter(raw)
        lines = [f"<{tag}>"]
        if description:
            lines.append(f"<description>{description}</description>")
        lines.extend([body, f"</{tag}>"])
        blocks.append("\n".join(lines))
    index = memory_dir / "MEMORY.md"
    if index.is_file():
        try:
            body = index.read_text(encoding="utf-8").strip()
        except OSError:
            body = ""
        if body:
            blocks.append(f"<memory>\n{body}\n</memory>")
    return "\n\n".join(blocks)


def _git_output(memory_dir: Path, args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(memory_dir), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _is_projected_memory_path(
    relative_path: str,
    all_paths: set[str],
    memory_format: str,
) -> bool:
    if memory_format == "memfs-v1":
        return True
    if relative_path == "skills" or relative_path.startswith("skills/"):
        return False
    if "/" not in relative_path:
        return True
    parts = relative_path.split("/")
    current = ""
    for directory in parts[:-1]:
        current = f"{current}/{directory}" if current else directory
        if f"{current}/MEMORY.md" not in all_paths:
            return False
    return True


def _escape_xml_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _render_memfs_v2_projection(root: Path, paths: Sequence[str]) -> str:
    all_paths = set(paths)
    root_files: list[tuple[str, str]] = []
    root_index = ""
    child_indexes: list[str] = []
    for relative_path in paths:
        raw = _git_output(root, ["show", f"HEAD:{relative_path}"]) or ""
        body = _strip_frontmatter(raw).strip()
        description = _frontmatter_description(raw)
        if "/" not in relative_path:
            if relative_path == "MEMORY.md":
                root_index = body
            else:
                root_files.append((relative_path[:-3], description + "\0" + body))
            continue
        if (
            relative_path.count("/") == 1
            and relative_path.endswith("/MEMORY.md")
            and not relative_path.startswith("skills/")
            and _is_projected_memory_path(relative_path, all_paths, "memfs-v2")
        ):
            child_indexes.append(relative_path)

    blocks: list[str] = []
    for label, packed in sorted(root_files):
        description, _separator, value = packed.partition("\0")
        lines = [f"<{label}>"]
        if description.strip():
            lines.append(f"<description>{description.strip()}</description>")
        if value.strip():
            lines.append(value.rstrip())
        lines.append(f"</{label}>")
        blocks.append("\n".join(lines))
    if root_index:
        lines = ["<memory>"]
        if root_index:
            lines.append(root_index.rstrip())
        if child_indexes:
            lines.append("<deferred-memory>")
        for relative_path in sorted(child_indexes):
            directory = relative_path[: -len("/MEMORY.md")]
            escaped = _escape_xml_attribute(directory)
            lines.append(f'<directory path="{escaped}/" index="{escaped}/MEMORY.md" />')
        if child_indexes:
            lines.append("</deferred-memory>")
        lines.append("</memory>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def compile_current_core_memory(memory_dir: Path) -> str:
    """Rebuild the core memory projection from the current committed MemFS.

    This mirrors the memfs-v2 branch in the vendored Letta runtime.  The
    benchmark never writes memory and never trusts a stale compiled prompt.
    """
    revision = (_git_output(memory_dir, ["rev-parse", "--verify", "HEAD"]) or "").strip()
    if not revision:
        return render_worktree_core_memory_fallback(memory_dir)
    tree = _git_output(memory_dir, ["ls-tree", "-r", "--name-only", "HEAD"])
    if tree is None:
        return render_worktree_core_memory_fallback(memory_dir)
    paths = [
        path.strip().replace("\\", "/")
        for path in tree.splitlines()
        if path.strip().endswith(".md")
    ]
    if not paths:
        return ""
    memory_format = "memfs-v2" if "MEMORY.md" in paths else "memfs-v1"
    if memory_format == "memfs-v2":
        return _render_memfs_v2_projection(memory_dir, paths)
    return render_worktree_core_memory_fallback(memory_dir)


def inject_core_memory(raw_system: str, core_memory: str) -> str:
    if "{CORE_MEMORY}" in raw_system:
        return raw_system.replace("{CORE_MEMORY}", core_memory).rstrip()
    return append_block(raw_system, core_memory).rstrip()


def load_base_system(config: AkiConfig) -> str:
    conversation = conversation_directory(
        default_backend_dir(),
        solo_conversation_id(config.agent_id),
        config.agent_id,
    )
    compiled = read_json(conversation / "system-prompt.json")
    record = find_agent_record(config) or {}
    system = str(record.get("system") or "").strip()
    memory_dir = default_memory_dir(config.agent_id)
    if system:
        return inject_core_memory(system, compile_current_core_memory(memory_dir))

    # The agent record should always exist for a running profile.  Keep a
    # read-only compiled fallback for partially provisioned installations.
    content = compiled.get("content")
    return content.strip() if isinstance(content, str) else ""


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item.get("type") == "image":
            parts.append("[image omitted from benchmark context]")
    return "\n".join(part for part in parts if part.strip()).strip()


def load_solo_conversation_messages(
    config: AkiConfig,
    *,
    budget_tokens: int,
) -> tuple[list[dict[str, Any]], int, int]:
    directory = conversation_directory(
        default_backend_dir(),
        solo_conversation_id(config.agent_id),
        config.agent_id,
    )
    conversation = read_json(directory / "conversation.json")
    ids = conversation.get("in_context_message_ids")
    if not isinstance(ids, list):
        return [], 0, 0
    by_id: dict[str, dict[str, Any]] = {}
    try:
        lines = (directory / "messages.jsonl").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return [], 0, 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "message":
            continue
        message = row.get("message")
        if isinstance(message, dict) and message.get("id"):
            by_id[str(message["id"])] = message

    turns: list[dict[str, Any]] = []
    for message_id in ids:
        message = by_id.get(str(message_id))
        if message is None:
            continue
        role = str(message.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(message)
        if text:
            turns.append({"role": role, "content": text})

    if budget_tokens <= 0:
        return turns, len(turns), sum(estimate_tokens(str(item["content"])) for item in turns)
    selected: list[dict[str, Any]] = []
    used = 0
    for item in reversed(turns):
        cost = estimate_tokens(str(item["content"])) + 8
        if selected and used + cost > budget_tokens:
            break
        selected.append(item)
        used += cost
    selected.reverse()
    return selected, len(selected), used


def load_solo_context(
    config: AkiConfig,
    *,
    history_budget_tokens: int,
) -> SoloContext:
    active = load_solo_session(default_state_path(config.profile))
    if active is not None and active.profile != config.profile:
        active = None
    session_id = active.session_id if active is not None else f"solo-benchmark-{uuid.uuid4().hex[:12]}"
    prompt_username = config.username.strip() or "Master"
    display_name, user_id = load_identities(config)
    display_name = display_name or prompt_username
    conversation, history_messages, history_tokens = load_solo_conversation_messages(
        config, budget_tokens=history_budget_tokens
    )
    group_entries = read_entries(profile_history_path(config, solo=False))
    solo_entries = read_entries(profile_history_path(config, solo=True))
    sessions = load_solo_memory_sessions(config)
    visible_sessions = [
        session for session in sessions if session.session_id != session_id
    ]
    online = set(discover_profiles())
    online.add(config.profile)
    return SoloContext(
        profile=config.profile,
        username=prompt_username,
        display_name=display_name,
        user_id=user_id,
        member=self_member(config),
        scene=load_solo_scene(
            config.profile,
            username=prompt_username,
            explicit=config.solo_prompt_file,
            repo_root=ROOT,
        ),
        base_system=load_base_system(config),
        emoji_entries=load_emoji_entries(ROOT / "assets" / "emoji", config.profile),
        conversation_messages=conversation,
        group_entries=group_entries,
        solo_entries=solo_entries,
        solo_sessions=visible_sessions,
        session_id=session_id,
        session_active=active is not None,
        next_seq=max((entry.seq for entry in solo_entries), default=0) + 1,
        online_profiles=frozenset(online),
        history_messages=history_messages,
        history_tokens=history_tokens,
    )


def _ledger_block(config: AkiConfig) -> str:
    if config.profile != "aki":
        return ""
    path = Path(os.environ.get("AKI_LEDGER_STATE", "")).expanduser() if os.environ.get("AKI_LEDGER_STATE") else profile_state_dir(config) / "ledger" / f"{config.profile}.json"
    try:
        records = LifeLedger(path)._read_unlocked()  # read-only: do not create the lock file
    except Exception:
        return ""
    return render_ledger_block(records)


async def build_live_block(config: AkiConfig, *, scan: bool) -> str:
    memory_dir = str(default_memory_dir(config.agent_id))
    ledger = _ledger_block(config)
    if not scan or not config.live_enabled:
        return render_runtime_context(
            memory_dir=memory_dir,
            mcp_servers=(),
            mcp_tools=(),
            mcp_native_tools_enabled=config.mcp_native_tools,
            note=(
                "Benchmark live scan disabled; only runtime paths are shown."
                if scan is False
                else "Desktop live context is disabled for this profile."
            ),
        )
    options = LiveContextOptions(
        history_limit=20,
        process_limit=5,
        timeout=config.live_timeout,
        screenshot_timeout=0.0,
        include_screenshot=False,
    )
    try:
        snapshot = await collect_live_context(options, now=datetime.now().astimezone())
        return render_live_context(
            snapshot,
            budget_tokens=config.live_budget_tokens,
            ledger_block=ledger,
            memory_dir=memory_dir,
            mcp_servers=(),
            mcp_tools=(),
            mcp_native_tools_enabled=config.mcp_native_tools,
        )
    except Exception as exc:
        return render_runtime_context(
            memory_dir=memory_dir,
            mcp_servers=(),
            mcp_tools=(),
            mcp_native_tools_enabled=config.mcp_native_tools,
            note=f"Benchmark live scan failed: {type(exc).__name__}",
        )


def openai_tools(profile: str) -> list[dict[str, Any]]:
    definitions: list[Mapping[str, Any]] = [
        *tools_for_profile(profile),
        ACQUIRE_EXECUTION_LOCK_TOOL,
        SEARCH_CHAT_HISTORY_TOOL,
        WAIT_USER_TOOL_SCHEMA,
        TOKIFACTOR_FILE_TOOL,
    ]
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in definitions:
        name = str(tool.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parameters = tool.get("parameters")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": parameters if isinstance(parameters, dict) else {},
                },
            }
        )
    return tools


def format_message_time(sent_at_ms: int) -> str:
    return datetime.fromtimestamp(sent_at_ms / 1000.0).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def render_channel_user_message(
    *,
    sender: str,
    sender_id: str,
    text: str,
    seq: int,
    sent_at_ms: int,
) -> str:
    identity = f"{sender} ({sender_id})" if sender_id else sender
    lines = [
        "[akid MQTT channel message]",
        f"message-time: {format_message_time(sent_at_ms)}",
        f"seq: {seq}",
        f"sender: {identity}",
    ]
    if sender_id:
        lines.append(f"sender_id: {sender_id}")
    lines.extend(["source: user", "phase: final", "", text])
    return "\n".join(lines)


def build_request(
    context: SoloContext,
    *,
    task: str,
    live_block: str,
    model: str,
    max_tokens: int,
    thinking: bool,
    reasoning_budget_tokens: int,
    tools: Sequence[Mapping[str, Any]],
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
    sent_at_ms: int | None = None,
) -> BuiltRequest:
    stable_parts: list[tuple[str, str]] = [
        ("system/channel-identity", render_channel_identity(context.member)),
        (
            "system/tool-ownership",
            render_tool_ownership_prompt(
                context.profile,
                online_profiles=set(context.online_profiles),
                solo=True,
            ),
        ),
        (
            "system/solo-policy",
            render_solo_policy(
                user_name=context.username,
                emoji_entries=context.emoji_entries,
            ),
        ),
        (
            "system/solo-mode",
            render_solo_mode(
                member_name=context.member.name,
                user_name=context.username,
                session_id=context.session_id,
                scene=context.scene,
            ),
        ),
    ]
    memories = render_solo_memories(
        context.solo_sessions,
        budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
        # A benchmark simulates a current solo turn, not a group turn after one
        # ended, so it must not mark a session as just_ended.
        just_ended_session_id="",
        local_model=True,
    )
    if memories:
        stable_parts.append(("system/solo-memories", memories))
    frozen = render_channel_transcript(
        context.group_entries,
        budget_tokens=min(
            DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
            SOLO_CHANNEL_TRANSCRIPT_BUDGET_TOKENS,
        ),
        frozen=True,
    )
    if frozen:
        stable_parts.append(("system/channel-transcript", frozen))

    mid_parts: list[tuple[str, str]] = []
    if live_block.strip():
        mid_parts.append(("user-prefix/live-context", live_block))
    mid_parts.append(
        ("user-prefix/format-guard", render_user_format_guard(context.username, solo=True))
    )
    mid_parts.append(
        (
            "user-prefix/wait-user",
            render_solo_wait_user(user_name=context.username, from_agent=False),
        )
    )

    system_append = "\n\n".join(part for _label, part in stable_parts if part.strip())
    user_prefix = "\n\n".join(part for _label, part in mid_parts if part.strip())
    system_prompt = append_block(context.base_system, system_append)
    now_ms = sent_at_ms if sent_at_ms is not None else int(time.time() * 1000)
    envelope = render_channel_user_message(
        sender=context.display_name,
        sender_id=context.user_id,
        text=task,
        seq=context.next_seq,
        sent_at_ms=now_ms,
    )
    user_content = prefix_user_content(envelope, user_prefix)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *[dict(item) for item in context.conversation_messages],
            {"role": "user", "content": user_content},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max(1, int(max_tokens)),
    }
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if top_p is not None:
        payload["top_p"] = float(top_p)
    if top_k is not None:
        payload["top_k"] = int(top_k)
    if thinking:
        payload["reasoning_effort"] = "medium"
        payload["reasoning_budget_tokens"] = max(0, int(reasoning_budget_tokens))
    if tools:
        payload["tools"] = [dict(tool) for tool in tools]

    history_user_text = "".join(
        str(message.get("content") or "")
        for message in context.conversation_messages
        if message.get("role") == "user"
    )
    history_assistant_text = "".join(
        str(message.get("content") or "")
        for message in context.conversation_messages
        if message.get("role") == "assistant"
    )
    history_user = estimate_tokens(history_user_text)
    history_assistant = estimate_tokens(history_assistant_text)
    tools_json = json.dumps(list(tools), ensure_ascii=False, separators=(",", ":")) if tools else ""
    tools_tokens = estimate_tokens(tools_json)
    token_parts: list[dict[str, Any]] = []

    def add_token_part(label: str, text: str) -> None:
        tokens = estimate_tokens(text)
        if tokens <= 0:
            return
        token_parts.append(
            {
                "label": label,
                "tokens": tokens,
                "chars": len(text),
            }
        )

    add_token_part("system/base", context.base_system)
    for label, part in stable_parts:
        add_token_part(label, part)
    for label, part in mid_parts:
        add_token_part(label, part)
    if history_user:
        token_parts.append(
            {
                "label": "history/user",
                "tokens": history_user,
                "chars": len(history_user_text),
            }
        )
    if history_assistant:
        token_parts.append(
            {
                "label": "history/assistant",
                "tokens": history_assistant,
                "chars": len(history_assistant_text),
            }
        )
    add_token_part("current-user/envelope+task", envelope)
    if tools_tokens:
        token_parts.append(
            {
                "label": "tools/schemas",
                "tokens": tools_tokens,
                "chars": len(tools_json),
            }
        )

    total_tokens = (
        estimate_tokens(system_prompt)
        + history_user
        + history_assistant
        + estimate_tokens(user_content)
        + tools_tokens
    )
    part_total = sum(int(part["tokens"]) for part in token_parts)
    framing = max(0, total_tokens - part_total)
    if framing:
        token_parts.append(
            {
                "label": "request framing/separators",
                "tokens": framing,
                "chars": None,
            }
        )
    total_tokens = sum(int(part["tokens"]) for part in token_parts)

    context_stats = {
        "history_messages": context.history_messages,
        "history_tokens_estimated": context.history_tokens,
        "base_system_chars": len(context.base_system),
        "system_append_chars": len(system_append),
        "user_prefix_chars": len(user_prefix),
        "system_prompt_chars": len(system_prompt),
        "user_message_chars": len(user_content),
        "prompt_tokens_estimated": total_tokens,
        "token_breakdown": token_parts,
        "solo_session_id": context.session_id,
        "solo_session_active": context.session_active,
        "group_entries": len(context.group_entries),
        "solo_memory_sessions": len(context.solo_sessions),
        "tools": len(tools),
    }
    return BuiltRequest(
        payload=payload,
        system_prompt=system_prompt,
        system_append=system_append,
        user_prefix=user_prefix,
        user_content=user_content,
        context_stats=context_stats,
        token_parts=token_parts,
        total_tokens=total_tokens,
    )


def format_token_breakdown(built: BuiltRequest) -> str:
    total = max(1, built.total_tokens)
    lines = [
        "",
        "=== prompt token breakdown (estimate_tokens, before request) ===",
        f"{'part':42} {'tokens':>9} {'share':>8} {'chars':>10}",
        "-" * 73,
    ]
    for part in built.token_parts:
        tokens = int(part["tokens"])
        chars = part.get("chars")
        lines.append(
            f"{str(part['label']):42} {tokens:>9,} {tokens / total:>7.1%} "
            f"{('-' if chars is None else f'{int(chars):,}'):>10}"
        )
    lines.extend(
        [
            "-" * 73,
            f"{'TOTAL':42} {built.total_tokens:>9,} {100.0:>7.1f}% "
            f"{'-':>10}",
        ]
    )
    return "\n".join(lines)


def format_case_preview(case: BenchmarkCase, max_tokens: int) -> str:
    lines = [
        "",
        f"=== benchmark case: {case.key} ===",
        f"kind={case.kind} max_tokens={max_tokens}",
        "prompt:",
        case.prompt,
    ]
    if case.kind == "coding":
        lines.extend(["", f"function={case.function} tests={len(case.tests)}"])
        for index, (args, expected) in enumerate(case.tests, start=1):
            lines.append(
                f"  {index}. args={json.dumps(args, ensure_ascii=False)} "
                f"expected={json.dumps(expected, ensure_ascii=False)}"
            )
    return "\n".join(lines)


def parse_sse(line: str) -> Mapping[str, Any] | None:
    text = line.strip()
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _merge_tool_deltas(calls: dict[int, dict[str, Any]], value: Any) -> None:
    if not isinstance(value, list):
        return
    for item in value:
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
    live_output: bool = False,
    live_reasoning: bool = False,
) -> StreamResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": "Bearer local",
        },
        method="POST",
    )
    started = time.perf_counter()
    ttft: float | None = None
    first_content: float | None = None
    parts: list[str] = []
    reasoning: list[str] = []
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    calls: dict[int, dict[str, Any]] = {}
    chunks = 0
    printed_content = False
    printed_reasoning = False
    finish_reason = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                event = parse_sse(raw_line.decode("utf-8", "replace"))
                if event is None:
                    continue
                now = time.perf_counter()
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    usage = raw_usage
                raw_timings = event.get("timings")
                if isinstance(raw_timings, dict):
                    timings = raw_timings
                choices = event.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                if isinstance(choice.get("finish_reason"), str):
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue
                delta_reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(delta_reasoning, str) and delta_reasoning:
                    if ttft is None:
                        ttft = now - started
                    reasoning.append(delta_reasoning)
                    chunks += 1
                    if live_reasoning:
                        if not printed_reasoning:
                            sys.stderr.write("\n--- streamed reasoning ---\n")
                            printed_reasoning = True
                        sys.stderr.write(delta_reasoning)
                        sys.stderr.flush()
                content = delta.get("content")
                texts: list[str] = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                for text in texts:
                    if not text:
                        continue
                    if ttft is None:
                        ttft = now - started
                    if first_content is None:
                        first_content = now - started
                    parts.append(text)
                    chunks += 1
                    if live_output:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                        printed_content = True
                if delta.get("tool_calls"):
                    if ttft is None:
                        ttft = now - started
                    _merge_tool_deltas(calls, delta.get("tool_calls"))
    finally:
        elapsed = time.perf_counter() - started
        if printed_content:
            sys.stdout.write("\n")
            sys.stdout.flush()
        if printed_reasoning:
            sys.stderr.write("\n")
            sys.stderr.flush()
    return StreamResult(
        text="".join(parts),
        reasoning="".join(reasoning),
        tool_calls=tuple(calls[index] for index in sorted(calls)),
        usage=usage,
        ttft_s=ttft,
        first_content_s=first_content,
        elapsed_s=elapsed,
        chunks=chunks,
        timings=timings,
        finish_reason=finish_reason,
    )


def _endpoint_url(route: SoloRoute) -> str:
    return route.endpoint.rstrip("/") + (route.path if route.path.startswith("/") else "/" + route.path)


def _live_state_running(config: AkiConfig) -> bool:
    state = read_json(default_state_path(config.profile))
    return state.get("running") is True and isinstance(state.get("route"), dict)


async def resolve_backend(
    config: AkiConfig,
    *,
    mode: str,
    endpoint_override: str,
) -> ResolvedBackend:
    endpoint = endpoint_override.strip() or config.solo_backend.endpoint.strip()
    if not config.solo_backend.configured and not endpoint_override.strip():
        raise RuntimeError(
            f"profile {config.profile!r} has no [solo] startup_script; "
            "cannot locate a local solo model"
        )

    if mode == "start" and endpoint_override.strip():
        raise RuntimeError("--endpoint cannot be combined with --backend start")

    if endpoint and mode != "start":
        try:
            route = await asyncio.to_thread(discover_solo_route, endpoint)
        except SoloBackendError as exc:
            if endpoint_override.strip():
                raise RuntimeError(f"cannot use --endpoint {endpoint}: {exc}") from exc
            if _live_state_running(config):
                raise RuntimeError(
                    "akid marks the solo backend as running, but it is not reachable; "
                    "refusing to start a second copy"
                ) from exc
        else:
            return ResolvedBackend(
                route=route,
                source="live" if _live_state_running(config) else "configured-endpoint",
                endpoint=route.endpoint,
            )

    if mode == "existing":
        raise RuntimeError(
            "no reachable solo backend; start it first or use --backend auto/start"
        )
    if endpoint_override.strip():
        raise RuntimeError("--endpoint cannot be combined with starting a managed backend")
    if not config.solo_backend.startup_script.strip():
        raise RuntimeError("solo startup_script is empty")

    temporary = tempfile.TemporaryDirectory(prefix=f"aki-benchmark-{config.profile}-")
    root = Path(temporary.name)
    manager = SoloBackendManager(
        config.solo_backend,
        profile=config.profile,
        state_path=root / "backend.json",
        activity_path=root / "activity",
        log_path=root / "backend.log",
    )
    try:
        route = await manager.start(
            reserved_model_ids={item.model_id for item in config.enabled_models}
        )
    except Exception:
        temporary.cleanup()
        raise
    return ResolvedBackend(
        route=route,
        source="ephemeral",
        endpoint=route.endpoint,
        manager=manager,
        temporary=temporary,
    )


def _python_block(text: str) -> str:
    fences = re.findall(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", text, re.I | re.S)
    if fences:
        return fences[-1].strip() + "\n"
    stripped = text.strip()
    match = re.search(r"(?m)^(?:from|import|def|class)\b", stripped)
    if match and match.start() > 0:
        stripped = stripped[match.start() :]
    return stripped + ("\n" if stripped else "")


def validate_code_safety(code: str) -> str:
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return "code exceeds size limit"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg} (line {exc.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in ALLOWED_IMPORTS:
                    return f"import not allowed: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".", 1)[0]
            if node.level or root not in ALLOWED_IMPORTS:
                return f"import not allowed: {node.module or '.'}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALL_NAMES:
                return f"call not allowed: {node.func.id}"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"dunder attribute not allowed: {node.attr}"
    return ""


def _run_harness(code: str, case: BenchmarkCase, timeout: float) -> CodeCheck:
    code_chars = len(code)
    code_lines = len(code.splitlines())
    code_tokens = estimate_tokens(code)
    safety_error = validate_code_safety(code)
    if safety_error:
        return CodeCheck(
            code=code,
            code_chars=code_chars,
            code_lines=code_lines,
            code_tokens=code_tokens,
            compile_ok=safety_error.startswith("syntax error") is False,
            tests_passed=0,
            tests_total=len(case.tests),
            tests_ok=False,
            execution_ms=0.0,
            error=safety_error,
        )

    harness = (
        "import importlib.util, json, sys, traceback\n"
        "spec = importlib.util.spec_from_file_location('solution', 'solution.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"fn = getattr(module, {case.function!r})\n"
        f"tests = json.loads({json.dumps(list(case.tests), ensure_ascii=False)!r})\n"
        "passed = 0\n"
        "details = []\n"
        "for index, (args, expected) in enumerate(tests):\n"
        "    try:\n"
        "        actual = fn(*args)\n"
        "        ok = actual == expected\n"
        "    except Exception as exc:\n"
        "        actual = None\n"
        "        ok = False\n"
        "        details.append({'index': index, 'error': f'{type(exc).__name__}: {exc}'})\n"
        "    if ok:\n"
        "        passed += 1\n"
        "print(json.dumps({'passed': passed, 'total': len(tests), 'details': details}, ensure_ascii=False))\n"
    )
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="aki-benchmark-code-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(code, encoding="utf-8")
        (root / "harness.py").write_text(harness, encoding="utf-8")

        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "harness.py"],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                start_new_session=True,
                preexec_fn=limits,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CodeCheck(
                code=code,
                code_chars=code_chars,
                code_lines=code_lines,
                code_tokens=code_tokens,
                compile_ok=True,
                tests_passed=0,
                tests_total=len(case.tests),
                tests_ok=False,
                execution_ms=(time.perf_counter() - started) * 1000.0,
                error=f"test execution timed out after {timeout:g}s",
            )
        except OSError as exc:
            return CodeCheck(
                code=code,
                code_chars=code_chars,
                code_lines=code_lines,
                code_tokens=code_tokens,
                compile_ok=True,
                tests_passed=0,
                tests_total=len(case.tests),
                tests_ok=False,
                execution_ms=(time.perf_counter() - started) * 1000.0,
                error=f"cannot execute test harness: {exc}",
            )

    output = ""
    for line in reversed(completed.stdout.splitlines()):
        if line.strip().startswith("{"):
            output = line
            break
    try:
        payload = json.loads(output) if output else {}
    except ValueError:
        payload = {}
    passed = int(payload.get("passed") or 0)
    total = int(payload.get("total") or len(case.tests))
    details = payload.get("details")
    error = ""
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout).strip()[-2000:]
    elif isinstance(details, list) and details:
        error = json.dumps(details, ensure_ascii=False)
    return CodeCheck(
        code=code,
        code_chars=code_chars,
        code_lines=code_lines,
        code_tokens=code_tokens,
        compile_ok=True,
        tests_passed=passed,
        tests_total=total,
        tests_ok=total > 0 and passed == total and completed.returncode == 0,
        execution_ms=(time.perf_counter() - started) * 1000.0,
        error=error,
    )


def check_code(case: BenchmarkCase, text: str, *, execute: bool) -> CodeCheck | None:
    if case.kind != "coding":
        return None
    code = _python_block(text)
    if not execute:
        try:
            ast.parse(code)
            compile_ok = True
            error = ""
        except SyntaxError as exc:
            compile_ok = False
            error = f"syntax error: {exc.msg} (line {exc.lineno})"
        return CodeCheck(
            code=code,
            code_chars=len(code),
            code_lines=len(code.splitlines()),
            code_tokens=estimate_tokens(code),
            compile_ok=compile_ok,
            tests_passed=0,
            tests_total=len(case.tests),
            tests_ok=False,
            execution_ms=0.0,
            executed=False,
            error=error,
        )
    return _run_harness(code, case, MAX_EXECUTION_SECONDS)


def result_payload(result: BenchmarkResult) -> dict[str, Any]:
    stream = result.result
    data: dict[str, Any] = {
        "case": result.case,
        "repeat": result.repeat,
        "ttft_ms": None if stream.ttft_s is None else round(stream.ttft_s * 1000, 2),
        "first_content_ms": (
            None if stream.first_content_s is None else round(stream.first_content_s * 1000, 2)
        ),
        "elapsed_ms": round(stream.elapsed_s * 1000, 2),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "prompt_tokens_estimated": result.prompt_tokens_estimated,
        "completion_tokens_estimated": result.completion_tokens_estimated,
        "generation_tokens_per_s": round(result.generation_tokens_per_s, 3),
        "wall_tokens_per_s": round(result.wall_tokens_per_s, 3),
        "output_chars": len(stream.text),
        "reasoning_chars": len(stream.reasoning),
        "chunks": stream.chunks,
        "finish_reason": stream.finish_reason,
        "tool_calls": list(stream.tool_calls),
        "server_timings": stream.timings,
        "error": result.error,
    }
    if result.code is not None:
        data["code"] = {
            "chars": result.code.code_chars,
            "lines": result.code.code_lines,
            "tokens_estimated": result.code.code_tokens,
            "compile_ok": result.code.compile_ok,
            "tests_passed": result.code.tests_passed,
            "tests_total": result.code.tests_total,
            "tests_ok": result.code.tests_ok,
            "execution_ms": round(result.code.execution_ms, 2),
            "executed": result.code.executed,
            "error": result.code.error,
            "tokens_per_s": round(
                result.code.code_tokens / max(result.result.elapsed_s, 0.001), 3
            ),
        }
    return data


def _median(values: Iterable[float]) -> float | None:
    items = [float(value) for value in values if value is not None]
    return statistics.median(items) if items else None


def summarize(results: Sequence[BenchmarkResult]) -> dict[str, Any]:
    by_case: dict[str, list[BenchmarkResult]] = {}
    for result in results:
        by_case.setdefault(result.case, []).append(result)
    summary: dict[str, Any] = {}
    for case, items in sorted(by_case.items()):
        code_items = [item.code for item in items if item.code is not None]
        summary[case] = {
            "runs": len(items),
            "ttft_ms_median": _median(
                item.result.ttft_s * 1000 for item in items if item.result.ttft_s is not None
            ),
            "elapsed_ms_median": _median(item.result.elapsed_s * 1000 for item in items),
            "generation_tokens_per_s_median": _median(
                item.generation_tokens_per_s for item in items
            ),
            "wall_tokens_per_s_median": _median(item.wall_tokens_per_s for item in items),
            "completion_tokens_median": _median(
                item.completion_tokens for item in items
            ),
            "code_tests_passed": sum(item.tests_passed for item in code_items),
            "code_tests_total": sum(item.tests_total for item in code_items),
            "code_runs_ok": sum(1 for item in code_items if item.tests_ok),
            "code_runs": len(code_items),
            "code_runs_executed": sum(1 for item in code_items if item.executed),
        }
    return summary


def print_table(report: Mapping[str, Any]) -> None:
    model = report["model"]
    context = report["context"]
    print(
        f"local benchmark | profile={report['profile']} | "
        f"model={model['upstream_model']} | source={model['source']}"
    )
    print(
        f"context | system={context['system_prompt_chars']:,} chars | "
        f"stable={context['system_append_chars']:,} | "
        f"prefix={context['user_prefix_chars']:,} | "
        f"history={context['history_messages']} msgs | "
        f"estimated prompt={context['prompt_tokens_estimated']:,} tokens | "
        f"solo_session={context['solo_session_id']}"
    )
    print("")
    print(
        f"{'case':30} {'ttft':>9} {'total':>9} {'tok/s':>9} "
        f"{'wall':>9} {'out':>7} {'code':>9}"
    )
    for result in report["results"]:
        code = ""
        if "code" in result:
            block = result["code"]
            if not block["executed"]:
                code = "syntax-ok" if block["compile_ok"] else "syntax!"
            else:
                code = f"{block['tests_passed']}/{block['tests_total']}"
                if not block["tests_ok"]:
                    code += "!"
        ttft = "-" if result["ttft_ms"] is None else f"{result['ttft_ms'] / 1000:.2f}s"
        print(
            f"{result['case']:30} {ttft:>9} "
            f"{result['elapsed_ms'] / 1000:>8.2f}s "
            f"{result['generation_tokens_per_s']:>8.2f} "
            f"{result['wall_tokens_per_s']:>8.2f} "
            f"{result['completion_tokens']:>7} {code:>9}"
        )
        if result["error"]:
            print(f"  error: {result['error']}")
    print("")
    for case, summary in report["summary"].items():
        if not summary["code_runs_executed"]:
            continue
        print(
            f"{case}: tests {summary['code_tests_passed']}/{summary['code_tests_total']} "
            f"across {summary['code_runs']} run(s)"
        )


async def run(args: argparse.Namespace) -> int:
    if args.config:
        path = Path(args.config).expanduser()
        profile = args.profile or profile_from_config_path(path)
    else:
        profile = args.profile or DEFAULT_PROFILE
        path = profile_config_path(profile)
    config = load_config(path, profile=profile)

    selected = [case for case in BENCHMARK_CASES if args.case == "all" or case.key == args.case]
    if not selected:
        raise RuntimeError("no benchmark case selected")

    backend = await resolve_backend(
        config,
        mode=args.backend,
        endpoint_override=args.endpoint or "",
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "profile": config.profile,
        "agent_id": config.agent_id,
        "mode": "solo",
        "model": {
            "id": backend.route.model_id,
            "upstream_model": backend.route.upstream_model,
            "endpoint": backend.endpoint,
            "context_window": backend.route.context_window,
            "max_tokens": backend.route.max_tokens,
            "source": backend.source,
        },
        "results": [],
    }
    try:
        context = load_solo_context(
            config,
            history_budget_tokens=args.history_budget_tokens,
        )
        if context.history_tokens + context.history_messages * 8 > backend.route.context_window:
            print(
                "warning: estimated history alone exceeds the model context window",
                file=sys.stderr,
            )
        live_block = await build_live_block(config, scan=not args.no_live_scan)
        tools = openai_tools(config.profile) if args.tools == "profile" else []
        sent_at_ms = int(time.time() * 1000)
        print(
            f"profile={config.profile} backend={backend.source} "
            f"model={backend.route.upstream_model} endpoint={backend.endpoint}",
            file=sys.stderr,
        )
        results: list[BenchmarkResult] = []
        for case_index, case in enumerate(selected):
            max_tokens = args.max_tokens or case.max_tokens
            built = build_request(
                context,
                task=case.prompt,
                live_block=live_block,
                model=backend.route.upstream_model,
                max_tokens=max_tokens,
                thinking=not args.no_thinking,
                reasoning_budget_tokens=args.reasoning_budget_tokens,
                tools=tools,
                temperature=args.temperature,
                top_p=args.top_p,
                top_k=args.top_k,
                sent_at_ms=sent_at_ms + case_index,
            )
            if case_index == 0:
                report["context"] = built.context_stats
            if args.dump_request_dir:
                directory = Path(args.dump_request_dir).expanduser()
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / f"{case.key}.request.json"
                target.write_text(
                    json.dumps(built.payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(format_case_preview(case, max_tokens), flush=True)
            print(format_token_breakdown(built), flush=True)
            for repeat in range(1, args.repeat + 1):
                print(
                    f"[{case.key}] run {repeat}/{args.repeat} "
                    f"(max_tokens={max_tokens})",
                    file=sys.stderr,
                )
                live_output = not args.no_stream_output
                if live_output:
                    print(
                        f"\n--- streamed output: {case.key} run {repeat}/{args.repeat} ---",
                        flush=True,
                    )
                stream = await asyncio.to_thread(
                    stream_chat,
                    _endpoint_url(backend.route),
                    built.payload,
                    timeout=args.timeout,
                    live_output=live_output,
                    live_reasoning=args.stream_reasoning,
                )
                usage = stream.usage
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                timings = stream.timings
                if not isinstance(prompt_tokens, int) or prompt_tokens <= 0:
                    prompt_tokens = timings.get("prompt_n")
                if not isinstance(completion_tokens, int) or completion_tokens <= 0:
                    completion_tokens = timings.get("predicted_n")
                prompt_estimated = not isinstance(prompt_tokens, int) or prompt_tokens <= 0
                completion_estimated = (
                    not isinstance(completion_tokens, int) or completion_tokens <= 0
                )
                if prompt_estimated:
                    prompt_tokens = int(
                        built.context_stats.get("prompt_tokens_estimated") or 0
                    )
                if completion_estimated:
                    completion_tokens = estimate_tokens(stream.text + stream.reasoning)
                generation_elapsed = max(
                    0.001,
                    stream.elapsed_s - (stream.ttft_s or 0.0),
                )
                generation_tokens_per_s = int(completion_tokens) / generation_elapsed
                predicted_ms = timings.get("predicted_ms")
                predicted_n = timings.get("predicted_n")
                if (
                    isinstance(predicted_ms, (int, float))
                    and predicted_ms > 0
                    and isinstance(predicted_n, int)
                    and predicted_n > 0
                ):
                    generation_tokens_per_s = predicted_n / (predicted_ms / 1000.0)
                wall_tokens_per_s = int(completion_tokens) / max(stream.elapsed_s, 0.001)
                code = check_code(case, stream.text, execute=not args.no_execute)
                if args.show_output and (args.no_stream_output or not stream.text):
                    print(f"=== {case.key} output ===", file=sys.stderr)
                    print(stream.text, file=sys.stderr)
                if args.show_output and stream.reasoning and not args.stream_reasoning:
                    print("=== reasoning ===", file=sys.stderr)
                    print(stream.reasoning, file=sys.stderr)
                result = BenchmarkResult(
                    case=case.key,
                    repeat=repeat,
                    result=stream,
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                    prompt_tokens_estimated=prompt_estimated,
                    completion_tokens_estimated=completion_estimated,
                    generation_tokens_per_s=generation_tokens_per_s,
                    wall_tokens_per_s=wall_tokens_per_s,
                    code=code,
                )
                results.append(result)
                report["results"].append(result_payload(result))
        report["summary"] = summarize(results)
        print_table(report)
        if args.json:
            target = Path(args.json).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"report written to {target}", file=sys.stderr)
        failed_code = any(
            item.code is not None and not item.code.tests_ok for item in results
        )
        return 2 if failed_code and args.fail_on_code_error else 0
    finally:
        await backend.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark a profile's local solo model with its current solo prompt "
            "and conversation context. The benchmark does not write live state."
        )
    )
    parser.add_argument("--profile", "--agent", dest="profile", default="")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--backend",
        choices=("auto", "existing", "start"),
        default="auto",
        help=(
            "auto: use a reachable backend, otherwise start a temporary one; "
            "existing: only use a reachable backend; start: always start a "
            "temporary managed backend"
        ),
    )
    parser.add_argument("--endpoint", help="override the solo /v1 endpoint")
    parser.add_argument(
        "--case",
        default="all",
        choices=("all", *[case.key for case in BENCHMARK_CASES]),
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--no-live-scan",
        action="store_true",
        help="do not inspect processes/windows; keep only runtime context",
    )
    parser.add_argument(
        "--tools",
        choices=("none", "profile"),
        default="profile",
        help=(
            "include akid/profile tool schemas by default for a more realistic first "
            "call; use none for a pure text/code generation measurement"
        ),
    )
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument(
        "--reasoning-budget-tokens",
        type=int,
        default=DEFAULT_REASONING_BUDGET_TOKENS,
    )
    parser.add_argument(
        "--history-budget-tokens",
        type=int,
        default=DEFAULT_HISTORY_BUDGET_TOKENS,
        help="maximum estimated tokens of solo conversation history; 0 means all",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="override case max_tokens; 0 uses each case default",
    )
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--show-output", action="store_true")
    parser.add_argument(
        "--no-stream-output",
        action="store_true",
        help="do not print assistant content deltas while the request runs",
    )
    parser.add_argument(
        "--stream-reasoning",
        action="store_true",
        help="also print reasoning deltas live to stderr",
    )
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="only syntax-check and statically inspect generated code",
    )
    parser.add_argument(
        "--fail-on-code-error",
        action="store_true",
        help="exit 2 when a coding sample fails its tests",
    )
    parser.add_argument("--dump-request-dir", type=Path)
    parser.add_argument("--json", type=Path, help="write the machine-readable report")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.history_budget_tokens < 0:
        parser.error("--history-budget-tokens must be >= 0")
    if args.reasoning_budget_tokens < 0:
        parser.error("--reasoning-budget-tokens must be >= 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (
        RuntimeError,
        SoloBackendError,
        ValueError,
        OSError,
        urllib.error.HTTPError,
    ) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read().decode("utf-8", "replace")
            print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
