"""T5 bbot_scan."""

from __future__ import annotations

from typing import Any, Literal

from . import descriptions
from .bins import require
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_in_scope
from .util import dumps, ensure_job_workdir, fail

Preset = Literal["subdomain-enum", "cloud-enum", "email-enum"]
_ALLOWED_PRESETS = {"subdomain-enum", "cloud-enum", "email-enum"}


def build_bbot_argv(
    target: str,
    *,
    preset: str = "subdomain-enum",
    passive_only: bool = True,
    timeout_s: int = 600,
    out_dir: str,
) -> list[str]:
    if not target:
        raise ReconError("target is required")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    if timeout_s > get_config().limits.long_timeout_s:
        raise ReconError("timeout_s exceeds long_timeout_s")
    if preset == "kitchen-sink" or "kitchen" in preset:
        raise ReconError("preset kitchen-sink is forbidden")
    if preset not in _ALLOWED_PRESETS:
        raise ReconError(
            f"preset {preset!r} is not allowed; use one of {sorted(_ALLOWED_PRESETS)}"
        )
    argv = [
        "bbot",
        "-t",
        target,
        "-p",
        preset,
        "-y",
        "--no-deps",
        "--no-color",
        "-j",
        "-o",
        out_dir,
        "--force",
    ]
    if passive_only:
        argv.extend(["-rf", "passive"])
    return argv


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.BBOT_SCAN.strip())
    async def bbot_scan(
        target: str,
        preset: Preset = "subdomain-enum",
        passive_only: bool = True,
        timeout_s: int = 600,
    ) -> str:
        """BBOT preset scan. Out of scope targets are rejected."""
        try:
            assert_in_scope(target)
            require("bbot")
            work = ensure_job_workdir("bbot")
            argv = build_bbot_argv(
                target,
                preset=preset,
                passive_only=passive_only,
                timeout_s=timeout_s,
                out_dir=str(work),
            )
            job = start_job(
                argv,
                timeout_s=timeout_s,
                tool="bbot_scan",
                cwd=work,
            )
            return dumps(job)
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
