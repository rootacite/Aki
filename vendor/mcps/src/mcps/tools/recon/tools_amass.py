"""T3 amass_passive, T4 amass_intel. Amass v5 has no `intel` subcommand."""

from __future__ import annotations

from typing import Any

from . import descriptions
from .bins import require
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_in_scope
from .util import dumps, ensure_job_workdir, fail


def _minutes(timeout_s: int) -> int:
    return max(1, (timeout_s + 59) // 60)


def build_amass_passive_argv(domain: str, *, timeout_s: int = 300, out_dir: str) -> list[str]:
    if not domain:
        raise ReconError("domain is required")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    if timeout_s > get_config().limits.long_timeout_s:
        raise ReconError("timeout_s exceeds long_timeout_s")
    # v5: passive is default; never pass -active/-brute/-alts.
    # Isolate artifacts with -dir/-log/-oA only. Leave the operator
    # Amass config and keys in the real user config directory.
    return [
        "amass",
        "enum",
        "-d",
        domain,
        "-nocolor",
        "-rigid",
        "-timeout",
        str(_minutes(timeout_s)),
        "-dir",
        out_dir,
        "-log",
        f"{out_dir}/amass.log",
        "-oA",
        f"{out_dir}/amass",
    ]


def build_amass_intel_argv(
    *,
    domain: str | None = None,
    org: str | None = None,
    asn: int | None = None,
    timeout_s: int = 300,
    out_dir: str,
) -> list[str]:
    if org:
        raise ReconError(
            "Amass v5.1 has no intel/org flag (subcommand 'intel' was removed); "
            "pass domain and/or asn"
        )
    if not domain and asn is None:
        raise ReconError("provide domain and/or asn")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    argv = [
        "amass",
        "enum",
        "-nocolor",
        "-rigid",
        "-timeout",
        str(_minutes(timeout_s)),
        "-dir",
        out_dir,
        "-log",
        f"{out_dir}/amass.log",
        "-oA",
        f"{out_dir}/amass",
    ]
    if domain:
        argv.extend(["-d", domain])
    if asn is not None:
        if asn <= 0:
            raise ReconError("asn must be a positive integer")
        argv.extend(["-asn", str(asn)])
    return argv


def _assert_no_active_flags(argv: list[str]) -> None:
    banned = {"-active", "-brute", "-alts"}
    if banned.intersection(argv):
        raise ReconError("active/brute flags are not allowed in this phase")


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.AMASS_PASSIVE.strip())
    async def amass_passive(domain: str, timeout_s: int = 300) -> str:
        """Passive Amass enum. Out of scope targets are rejected."""
        try:
            assert_in_scope(domain)
            require("amass")
            work = ensure_job_workdir("amass")
            argv = build_amass_passive_argv(domain, timeout_s=timeout_s, out_dir=str(work))
            _assert_no_active_flags(argv)
            job = start_job(
                argv,
                timeout_s=timeout_s,
                tool="amass_passive",
                cwd=work,
            )
            return dumps(job)
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.AMASS_INTEL.strip())
    async def amass_intel(
        domain: str | None = None,
        org: str | None = None,
        asn: int | None = None,
        timeout_s: int = 300,
    ) -> str:
        """Passive Amass intel via enum. Out of scope targets are rejected."""
        try:
            if domain:
                assert_in_scope(domain)
            require("amass")
            work = ensure_job_workdir("amass")
            argv = build_amass_intel_argv(
                domain=domain, org=org, asn=asn, timeout_s=timeout_s, out_dir=str(work)
            )
            _assert_no_active_flags(argv)
            job = start_job(
                argv,
                timeout_s=timeout_s,
                tool="amass_intel",
                cwd=work,
            )
            return dumps(job)
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
