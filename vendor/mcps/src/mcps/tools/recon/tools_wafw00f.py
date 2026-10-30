"""T10 wafw00f_detect."""

from __future__ import annotations

import json
from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_in_scope
from .util import dumps, fail, result_payload


def build_wafw00f_argv(url: str) -> list[str]:
    if not url:
        raise ReconError("url is required")
    return [
        "wafw00f",
        "-f",
        "json",
        "-o",
        "-",
        "--no-colors",
        url,
    ]


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.WAFW00F_DETECT.strip())
    async def wafw00f_detect(url: str, timeout_s: int = 45) -> str:
        """WAF detection. Out of scope targets are rejected."""
        try:
            require("wafw00f")
            assert_in_scope(url)
            argv = build_wafw00f_argv(url)
            result = run_recon(argv, timeout_s=timeout_s, tool="wafw00f_detect")
            parsed: Any
            try:
                parsed = json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else result.stdout
            except json.JSONDecodeError:
                parsed = result.stdout
            return dumps(result_payload(result, extra={"waf": parsed}))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
