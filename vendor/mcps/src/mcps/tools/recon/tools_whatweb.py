"""T9 whatweb_fingerprint."""

from __future__ import annotations

import json
from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_in_scope
from .util import dumps, fail, result_payload

_MAX_URLS = 20


def build_whatweb_argv(urls: list[str], *, aggression: int = 1) -> list[str]:
    if not urls:
        raise ReconError("urls is required")
    if len(urls) > _MAX_URLS:
        raise ReconError(f"urls exceeds {_MAX_URLS}")
    if aggression not in {1, 2, 3}:
        raise ReconError("aggression must be 1, 2, or 3 (4/heavy is forbidden)")
    return [
        "whatweb",
        f"--aggression={aggression}",
        "--color=never",
        "--log-json=-",
        "--quiet",
        "--max-redirects=5",
        *urls,
    ]


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.WHATWEB_FINGERPRINT.strip())
    async def whatweb_fingerprint(
        urls: list[str] | None = None,
        aggression: int = 1,
        timeout_s: int = 60,
    ) -> str:
        """WhatWeb fingerprint. Out of scope targets are rejected."""
        try:
            require("whatweb")
            urls = urls or []
            for url in urls:
                assert_in_scope(url)
            argv = build_whatweb_argv(urls, aggression=aggression)
            result = run_recon(argv, timeout_s=timeout_s, tool="whatweb_fingerprint")
            parsed: Any
            try:
                parsed = json.loads(result.stdout) if result.stdout.strip() else []
            except json.JSONDecodeError:
                parsed = result.stdout
            return dumps(result_payload(result, extra={"results": parsed}))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
