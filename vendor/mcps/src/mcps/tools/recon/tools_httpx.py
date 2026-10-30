"""T8 httpx_probe — ProjectDiscovery httpx via bins.httpx (httpx-pd)."""

from __future__ import annotations

from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_scan_targets
from .util import dumps, fail, parse_jsonl, result_payload

_KEEP = (
    "url",
    "status_code",
    "title",
    "webserver",
    "tech",
    "cdn",
    "content_length",
    "host",
    "port",
    "ip",
    "content_type",
    "location",
    "scheme",
)


def build_httpx_argv(
    targets: list[str],
    *,
    follow_redirects: bool = True,
    tech_detect: bool = True,
    timeout_s: int = 90,
) -> tuple[list[str], str]:
    if not targets:
        raise ReconError("targets is required")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    cfg = get_config()
    if len(targets) > cfg.limits.max_hosts:
        raise ReconError(f"targets exceeds max_hosts={cfg.limits.max_hosts}")
    argv = [
        "httpx",
        "-l",
        "/dev/stdin",
        "-silent",
        "-json",
        "-nc",
        "-duc",
        "-title",
        "-sc",
        "-cl",
        "-server",
        "-ip",
        "-cdn",
        "-timeout",
        str(min(timeout_s, 60)),
        "-omit-body",
    ]
    if follow_redirects:
        argv.extend(["-fr", "-maxr", "5"])
    if tech_detect:
        argv.append("-td")
    # This httpx-pd build has no -js / -include-body flag; do not invent one.
    stdin = "\n".join(targets) + "\n"
    return argv, stdin


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slim: list[dict[str, Any]] = []
    for row in rows:
        item = {k: row[k] for k in _KEEP if k in row}
        slim.append(item)
    return slim


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.HTTPX_PROBE.strip())
    async def httpx_probe(
        targets: list[str] | None = None,
        follow_redirects: bool = True,
        tech_detect: bool = True,
        timeout_s: int = 90,
        json_output: bool = True,
    ) -> str:
        """HTTP probe and basic fingerprint. Out of scope targets are rejected."""
        try:
            require("httpx")
            targets = targets or []
            assert_scan_targets(targets, max_items=get_config().limits.max_hosts)
            argv, stdin = build_httpx_argv(
                targets,
                follow_redirects=follow_redirects,
                tech_detect=tech_detect,
                timeout_s=timeout_s,
            )
            result = run_recon(
                argv,
                timeout_s=timeout_s,
                input_text=stdin,
                tool="httpx_probe",
            )
            rows = parse_jsonl(result.stdout)
            extra: dict[str, Any] = {
                "count": len(rows),
                "endpoints": _summarize(rows) if json_output else [],
            }
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
