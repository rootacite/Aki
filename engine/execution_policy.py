"""Static tool capability classification for the execution lock."""

from __future__ import annotations

import json
import re
from typing import Any

from engine.tool_ownership import EXCLUSIVE_TOOL_NAMES, is_exclusive_tool

EXECUTION_LOCK_TAG = "aki-execution-lock"
WAIT_OTHER_TOOL = "wait_other"

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
        WAIT_OTHER_TOOL,
        "aki_schedule_list",
        "aki_ledger_list",
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

def is_read_only_tool(tool_name: str) -> bool:
    return tool_name in READ_ONLY_TOOLS or tool_name in READ_ONLY_MCP_TOOLS


def is_lock_control_tool(tool_name: str) -> bool:
    return tool_name in LOCK_CONTROL_TOOLS


def requires_execution_lock(tool_name: str) -> bool:
    """Fail closed: every tool not explicitly classified as read-only is gated."""
    if is_exclusive_tool(tool_name):
        # Exclusive tools are profile-private state operations authorized by
        # ownership. They never touch the device-wide execution surface.
        return False
    return not is_read_only_tool(tool_name) and not is_lock_control_tool(tool_name)


def execution_policy_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "read_only_tools": sorted(READ_ONLY_TOOLS),
        "read_only_mcp_tools": sorted(READ_ONLY_MCP_TOOLS),
        "lock_control_tools": sorted(LOCK_CONTROL_TOOLS),
        "exclusive_tools": sorted(EXCLUSIVE_TOOL_NAMES),
    }


def execution_policy_json() -> str:
    return json.dumps(
        execution_policy_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def clear_execution_lock_policy(system: str) -> str:
    if _EXECUTION_LOCK_RE.search(system) is None:
        return system
    return _EXECUTION_LOCK_RE.sub("\n", system).rstrip()
