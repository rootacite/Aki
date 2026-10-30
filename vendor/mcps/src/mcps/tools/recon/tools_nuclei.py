"""T14 nuclei_tech, T15 nuclei_vuln. Template path is locked."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import descriptions
from .bins import require
from .common import run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_scan_targets
from .util import dumps, fail, parse_jsonl, result_payload

_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.I)
_SEV = {"info", "low", "medium", "high", "critical"}
_TECH_TAGS = ("tech", "waf", "cdn")
_VULN_TAGS = ("cve", "misconfig", "exposure")


def nuclei_templates_dir() -> Path:
    raw = get_config().limits.nuclei_templates_dir
    path = Path(raw).resolve()
    if not path.is_dir():
        raise ReconError(f"nuclei templates dir missing: {path}")
    return path


def _tags(values: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for item in values:
        name = item.strip().lower()
        if not name or not _TAG.match(name) or "/" in name or ".." in name:
            raise ReconError(f"invalid nuclei tag: {item!r}")
        if name.endswith((".yaml", ".yml")):
            raise ReconError("template filenames are not tags; -t paths are locked")
        out.append(name)
    return out


def _severity(values: list[str]) -> list[str]:
    out: list[str] = []
    for item in values:
        name = item.strip().lower()
        if name not in _SEV:
            raise ReconError(f"invalid severity: {item!r}")
        out.append(name)
    return out


def build_nuclei_argv(
    urls: list[str],
    *,
    tags: list[str],
    severity: list[str],
    extra_tags: list[str] | None = None,
) -> list[str]:
    if not urls:
        raise ReconError("urls is required")
    cfg = get_config()
    if extra_tags:
        tags = [*tags, *_tags(extra_tags)]
    else:
        tags = _tags(tags)
    severity = _severity(severity)
    templates = str(nuclei_templates_dir())
    conc = str(cfg.limits.nuclei_max_concurrency)
    return [
        "nuclei",
        "-l",
        "/dev/stdin",
        "-silent",
        "-jsonl",
        "-nc",
        "-duc",
        "-t",
        templates,
        "-tags",
        ",".join(tags),
        "-s",
        ",".join(severity),
        "-pt",
        "http",
        "-ept",
        "code,javascript,headless,file",
        "-c",
        conc,
        "-bs",
        conc,
    ]


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.NUCLEI_TECH.strip())
    async def nuclei_tech(
        urls: list[str] | None = None,
        extra_tags: list[str] | None = None,
        timeout_s: int = 120,
    ) -> str:
        """Tech/WAF/CDN nuclei. Out of scope targets are rejected."""
        try:
            require("nuclei")
            urls = urls or []
            assert_scan_targets(urls, max_items=get_config().limits.max_hosts)
            argv = build_nuclei_argv(
                urls,
                tags=list(_TECH_TAGS),
                severity=["info", "low"],
                extra_tags=extra_tags,
            )
            if timeout_s > 60:
                return dumps(
                    start_job(
                        argv,
                        timeout_s=timeout_s,
                        input_text="\n".join(urls) + "\n",
                        tool="nuclei_tech",
                    )
                )
            result = run_recon(
                argv,
                timeout_s=timeout_s,
                input_text="\n".join(urls) + "\n",
                tool="nuclei_tech",
            )
            rows = parse_jsonl(result.stdout)
            return dumps(result_payload(result, extra={"findings": rows, "count": len(rows)}))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.NUCLEI_VULN.strip())
    async def nuclei_vuln(
        urls: list[str] | None = None,
        tags: list[str] | None = None,
        severity: list[str] | None = None,
        timeout_s: int = 600,
    ) -> str:
        """Vuln nuclei (job). Out of scope targets are rejected."""
        try:
            require("nuclei")
            urls = urls or []
            assert_scan_targets(urls, max_items=get_config().limits.max_hosts)
            argv = build_nuclei_argv(
                urls,
                tags=tags or list(_VULN_TAGS),
                severity=severity or ["medium", "high", "critical"],
            )
            return dumps(
                start_job(
                    argv,
                    timeout_s=timeout_s,
                    input_text="\n".join(urls) + "\n",
                    tool="nuclei_vuln",
                )
            )
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
