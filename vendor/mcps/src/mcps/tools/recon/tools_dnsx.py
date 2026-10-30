"""T2 dnsx_resolve."""

from __future__ import annotations

from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_in_scope
from .util import dumps, fail, parse_jsonl, result_payload

_RECORD_FLAGS = {
    "A": "-a",
    "AAAA": "-aaaa",
    "CNAME": "-cname",
    "NS": "-ns",
    "TXT": "-txt",
    "MX": "-mx",
    "SOA": "-soa",
    "CAA": "-caa",
    "SRV": "-srv",
    "PTR": "-ptr",
}
_FORBIDDEN = {"AXFR", "ANY", "ALL"}


def build_dnsx_argv(
    hosts: list[str],
    *,
    records: list[str] | None = None,
    timeout_s: int = 60,
) -> tuple[list[str], str]:
    if not hosts:
        raise ReconError("hosts is required")
    cfg = get_config()
    if len(hosts) > cfg.limits.max_hosts:
        raise ReconError(f"hosts exceeds max_hosts={cfg.limits.max_hosts}")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    recs = records or ["A", "AAAA", "CNAME"]
    flags: list[str] = []
    seen: set[str] = set()
    for rec in recs:
        name = rec.strip().upper()
        if name in _FORBIDDEN:
            raise ReconError(f"DNS record type {name} is not allowed (too aggressive)")
        flag = _RECORD_FLAGS.get(name)
        if not flag:
            raise ReconError(f"unsupported DNS record type: {rec!r}")
        if name not in seen:
            seen.add(name)
            flags.append(flag)
    argv = ["dnsx", "-l", "/dev/stdin", "-silent", "-j", "-cdn", "-nc", "-duc", *flags]
    stdin = "\n".join(hosts) + "\n"
    return argv, stdin


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.DNSX_RESOLVE.strip())
    async def dnsx_resolve(
        hosts: list[str] | None = None,
        records: list[str] | None = None,
        timeout_s: int = 60,
        json_output: bool = True,
    ) -> str:
        """Batch DNS resolve. Out of scope targets are rejected."""
        try:
            require("dnsx")
            hosts = hosts or []
            for host in hosts:
                assert_in_scope(host)
            argv, stdin = build_dnsx_argv(hosts, records=records, timeout_s=timeout_s)
            result = run_recon(
                argv,
                timeout_s=timeout_s,
                input_text=stdin,
                tool="dnsx_resolve",
            )
            rows = parse_jsonl(result.stdout)
            extra: dict[str, Any] = {
                "count": len(rows),
                "records": rows if json_output else [],
            }
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
