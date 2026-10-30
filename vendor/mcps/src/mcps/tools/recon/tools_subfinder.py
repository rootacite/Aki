"""T1 subfinder_enum."""

from __future__ import annotations

from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_in_scope
from .util import dumps, fail, parse_jsonl, result_payload, unique_hosts_from_jsonl


def build_subfinder_argv(
    domain: str, *, all_sources: bool = False, timeout_s: int = 120
) -> list[str]:
    if not domain or not isinstance(domain, str):
        raise ReconError("domain is required")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    if timeout_s > 900:
        raise ReconError("timeout_s exceeds 900s")
    argv = [
        "subfinder",
        "-d",
        domain,
        "-silent",
        "-oJ",
        "-duc",
        "-nc",
        "-timeout",
        str(min(timeout_s, 120)),
        "-max-time",
        str(max(1, (timeout_s + 59) // 60)),
    ]
    if all_sources:
        argv.append("-all")
    return argv


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.SUBFINDER_ENUM.strip())
    async def subfinder_enum(
        domain: str,
        all_sources: bool = False,
        timeout_s: int = 120,
        json_output: bool = True,
    ) -> str:
        """Passive subdomain enumeration. Out of scope targets are rejected."""
        try:
            assert_in_scope(domain)
            require("subfinder")
            argv = build_subfinder_argv(
                domain, all_sources=all_sources, timeout_s=timeout_s
            )
            result = run_recon(
                argv,
                timeout_s=timeout_s,
                tool="subfinder_enum",
            )
            rows = parse_jsonl(result.stdout)
            hosts = unique_hosts_from_jsonl(rows)
            extra: dict[str, Any] = {
                "domain": domain,
                "hosts": hosts,
                "count": len(hosts),
            }
            if json_output:
                extra["records"] = rows[: get_config().limits.max_hosts]
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise  # unreachable
