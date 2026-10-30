"""T6 naabu_scan."""

from __future__ import annotations

import re
from typing import Any, Literal

from . import descriptions
from .bins import require
from .common import resolve_scan_type, run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_scan_targets
from .util import dumps, fail, parse_jsonl, result_payload

ScanType = Literal["syn", "connect"]
_ALL_PORTS = re.compile(r"^(full|-|0-65535|1-65535|\*|all)$", re.I)
_PORT_TOKEN = re.compile(r"^\d+(-\d+)?$")


def build_naabu_argv(
    targets: list[str],
    *,
    top_ports: int = 100,
    ports: str | None = None,
    exclude_cdn: bool = True,
    rate: int = 1000,
    aggressive: bool = False,
    scan_type: str = "syn",
) -> tuple[list[str], str, str, bool]:
    if not targets:
        raise ReconError("targets is required")
    cfg = get_config()
    if rate < 1:
        raise ReconError("rate must be positive")
    if rate > cfg.limits.naabu_max_rate:
        raise ReconError(f"rate exceeds naabu_max_rate={cfg.limits.naabu_max_rate}")
    if ports and _looks_like_all_ports(ports) and not aggressive:
        raise ReconError("full-port scan requires aggressive=true")
    if ports is None:
        if top_ports not in {100, 1000}:
            raise ReconError("top_ports must be 100 or 1000")
        if top_ports == 1000 and not aggressive:
            raise ReconError("top_ports=1000 requires aggressive=true")
    else:
        _validate_ports(ports)

    actual, need_raw, note = resolve_scan_type(scan_type)
    argv = [
        "naabu",
        "-l",
        "/dev/stdin",
        "-silent",
        "-json",
        "-nc",
        "-duc",
        "-Pn",
        "-rate",
        str(rate),
        "-s",
        "s" if actual == "syn" else "c",
    ]
    if ports:
        argv.extend(["-p", ports])
    else:
        argv.extend(["-tp", str(top_ports)])
    if exclude_cdn:
        argv.append("-ec")
    stdin = "\n".join(targets) + "\n"
    return argv, stdin, note, need_raw


def _looks_like_all_ports(ports: str) -> bool:
    return bool(_ALL_PORTS.match(ports.strip()))


def _validate_ports(ports: str) -> None:
    raw = ports.strip()
    if not raw or any(c.isspace() for c in raw):
        raise ReconError("invalid ports spec")
    if _looks_like_all_ports(raw):
        return
    for token in raw.split(","):
        if not _PORT_TOKEN.match(token):
            raise ReconError(f"invalid ports spec: {ports!r}")
        if "-" in token:
            a, b = token.split("-", 1)
            lo, hi = int(a), int(b)
            if lo < 1 or hi > 65535 or lo > hi:
                raise ReconError(f"invalid port range: {token}")
        else:
            n = int(token)
            if n < 1 or n > 65535:
                raise ReconError(f"invalid port: {token}")


def _hostports(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        host = row.get("host") or row.get("ip")
        port = row.get("port")
        if host is None or port is None:
            continue
        item = f"{host}:{port}"
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.NAABU_SCAN.strip())
    async def naabu_scan(
        targets: list[str] | None = None,
        top_ports: int = 100,
        ports: str | None = None,
        exclude_cdn: bool = True,
        rate: int = 1000,
        aggressive: bool = False,
        scan_type: ScanType = "syn",
        timeout_s: int | None = None,
        json_output: bool = True,
    ) -> str:
        """Fast port discovery. Out of scope targets are rejected."""
        try:
            require("naabu")
            targets = targets or []
            assert_scan_targets(targets, max_items=get_config().limits.max_hosts)
            argv, stdin, note, need_raw = build_naabu_argv(
                targets,
                top_ports=top_ports,
                ports=ports,
                exclude_cdn=exclude_cdn,
                rate=rate,
                aggressive=aggressive,
                scan_type=scan_type,
            )
            timeout = timeout_s or get_config().limits.default_timeout_s
            result = run_recon(
                argv,
                timeout_s=timeout,
                input_text=stdin,
                need_raw_socket=need_raw,
                tool="naabu_scan",
            )
            rows = parse_jsonl(result.stdout)
            extra: dict[str, Any] = {
                "scan_type_requested": scan_type,
                "scan_type_note": note,
                "host_ports": _hostports(rows),
                "count": len(rows),
            }
            if json_output:
                extra["records"] = rows
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
