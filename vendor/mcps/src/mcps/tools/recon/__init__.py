"""Recon toolkit: public layer + Phase 1 tools."""

from __future__ import annotations

from typing import Any

from .bins import inspect, require, which
from .common import RunResult, run_recon, truncate_for_model
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_in_scope


def register(mcp: Any) -> None:
    from . import (
        tools_amass,
        tools_bbot,
        tools_dnsx,
        tools_ffuf,
        tools_httpx,
        tools_jobs,
        tools_jshunter,
        tools_katana,
        tools_naabu,
        tools_nmap,
        tools_nuclei,
        tools_restringer,
        tools_subfinder,
        tools_health,
        tools_wafw00f,
        tools_whatweb,
        workflows,
    )

    tools_subfinder.register(mcp)
    tools_dnsx.register(mcp)
    tools_amass.register(mcp)
    tools_bbot.register(mcp)
    tools_naabu.register(mcp)
    tools_nmap.register(mcp)
    tools_httpx.register(mcp)
    tools_whatweb.register(mcp)
    tools_wafw00f.register(mcp)
    tools_katana.register(mcp)
    tools_jshunter.register(mcp)
    tools_restringer.register(mcp)
    tools_nuclei.register(mcp)
    tools_ffuf.register(mcp)
    tools_jobs.register(mcp)
    tools_health.register(mcp)
    workflows.register(mcp)


__all__ = [
    "BinUnavailable",
    "ReconError",
    "RunResult",
    "ScopeError",
    "assert_in_scope",
    "inspect",
    "register",
    "require",
    "run_recon",
    "truncate_for_model",
    "which",
]
