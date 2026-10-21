"""Static tool capability classification and execution-lock prompt blocks."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from xml.sax.saxutils import quoteattr

EXECUTION_LOCK_TAG = "aki-execution-lock"

READ_ONLY_TOOLS = frozenset(
    {
        "Read",
        "ReadLSP",
        "Grep",
        "Glob",
        "LS",
        "ListDir",
        "ViewImage",
        "read_artifact_file",
        "TaskGet",
        "TaskList",
        "AskUserQuestion",
        "AskUserQuestionAsync",
        "desktop_take_screenshot",
        "camera_take_photo",
    }
)

# Tavily's exposed MCP tools only query or extract public data.  Unknown MCP
# tools are deliberately not trusted as read-only.
READ_ONLY_MCP_TOOLS = frozenset(
    {
        "mcp__tavily__tavily_search",
        "mcp__tavily__tavily_extract",
        "mcp__tavily__tavily_crawl",
        "mcp__tavily__tavily_map",
        "mcp__tavily__tavily_research",
    }
)

LOCK_ACQUIRE_TOOL = "acquire_execution_lock"
LOCK_RELEASE_TOOL = "release_execution_lock"
LOCK_CONTROL_TOOLS = frozenset({LOCK_ACQUIRE_TOOL, LOCK_RELEASE_TOOL})

LOCK_REQUIRED_ERROR = (
    "EXECUTION_LOCK_REQUIRED: this tool can modify state or produce external "
    "effects. Call acquire_execution_lock first. Do not retry this tool without "
    "the lock; end this turn and report the lock failure."
)
LOCK_NOT_AUTHORIZED_ERROR = (
    "EXECUTION_LOCK_NOT_AUTHORIZED: the user has not assigned this non-read-only "
    "action to you. Do not retry; end this turn and ask the user to name an "
    "executor with a valid @mention."
)
LOCK_BUSY_ERROR = (
    "EXECUTION_LOCK_UNAVAILABLE: another agent holds the device-wide execution "
    "lock. Do not retry in this turn; end this turn."
)

_EXECUTION_LOCK_RE = re.compile(
    rf"\n*<{re.escape(EXECUTION_LOCK_TAG)}\b.*?</{re.escape(EXECUTION_LOCK_TAG)}>\n*",
    re.S,
)

_READ_ONLY_EXAMPLES = (
    "`Read`, `ReadLSP`, `Grep`, `Glob`, `LS`, `ListDir`, `ViewImage`, "
    "截图、摄像头、Tavily MCP 查询"
)
_NON_READ_ONLY_EXAMPLES = (
    "`Bash`、`Memory`、`Agent`、Edit/Write/ApplyPatch、任务状态写入、"
    "发图、未知 MCP"
)


def is_read_only_tool(tool_name: str) -> bool:
    return tool_name in READ_ONLY_TOOLS or tool_name in READ_ONLY_MCP_TOOLS


def is_lock_control_tool(tool_name: str) -> bool:
    return tool_name in LOCK_CONTROL_TOOLS


def requires_execution_lock(tool_name: str) -> bool:
    """Fail closed: every tool not explicitly classified as read-only is gated."""
    return not is_read_only_tool(tool_name) and not is_lock_control_tool(tool_name)


def execution_policy_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "read_only_tools": sorted(READ_ONLY_TOOLS),
        "read_only_mcp_tools": sorted(READ_ONLY_MCP_TOOLS),
        "lock_control_tools": sorted(LOCK_CONTROL_TOOLS),
    }


def execution_policy_json() -> str:
    return json.dumps(
        execution_policy_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_execution_lock_policy(
    *,
    authorized: bool,
    reason: str,
    holder: Mapping[str, Any] | None = None,
) -> str:
    auth = "true" if authorized else "false"
    holder_profile = str((holder or {}).get("profile") or "")
    lines = [
        f"<{EXECUTION_LOCK_TAG} authorized={quoteattr(auth)} "
        f"reason={quoteattr(reason)} holder={quoteattr(holder_profile)}>",
        "<tool-classification>只读工具不需要锁："
        f"{_READ_ONLY_EXAMPLES}。非只读及未知工具一律需要锁："
        f"{_NON_READ_ONLY_EXAMPLES}。</tool-classification>",
        "<lock-rule>调用任何非只读工具前必须先调用 "
        f"`{LOCK_ACQUIRE_TOOL}`。锁是整机共享排他锁；成功后才能在后续工具调用中"
        "执行非只读操作。完成后调用 "
        f"`{LOCK_RELEASE_TOOL}`，turn 结束时网关也会自动释放。"
        "如果工具返回缺少锁、抢锁失败或未获授权，禁止重试同一操作；立即结束本轮"
        "并说明需要用户指定执行者。除非用户主动询问，否则无需刻意向用户强调锁的"
        "获取、持有或释放状态；正常完成工作即可。</lock-rule>",
    ]
    if authorized:
        lines.append(
            "<authorization>当前请求已获得非只读执行授权；先获取锁，再执行需要的"
            "非只读工具。不要因为其他成员在线就跳过加锁。</authorization>"
        )
    else:
        lines.append(
            "<authorization>当前请求没有授权你执行非只读操作。纯聊天和只读工具"
            "仍可正常使用；如果完成任务需要非只读工具，不要尝试抢锁，也不要执行。"
            "在群聊中应由约定的确认者请用户用有效 `@成员名` 指定执行者；其他成员"
            "保持沉默或只提供只读分析。</authorization>"
        )
    lines.append(f"</{EXECUTION_LOCK_TAG}>")
    return "\n".join(lines)


def apply_execution_lock_policy(system: str, block: str) -> str:
    cleaned = _EXECUTION_LOCK_RE.sub("\n", system).rstrip()
    return f"{cleaned}\n\n{block.strip()}\n"


def clear_execution_lock_policy(system: str) -> str:
    if _EXECUTION_LOCK_RE.search(system) is None:
        return system
    return _EXECUTION_LOCK_RE.sub("\n", system).rstrip()
