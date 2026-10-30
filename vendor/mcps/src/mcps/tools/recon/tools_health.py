"""T21 recon_health."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import bins, descriptions
from .common import has_net_raw
from .config import get_config
from .errors import ReconError
from .util import dumps, fail


def collect_health() -> dict[str, Any]:
    cfg = get_config()
    binaries: dict[str, Any] = {}
    for name in bins.known_names():
        info = bins.inspect(name)
        binaries[name] = {
            "found": info.available and info.identity_ok,
            "required": bins.is_required(name),
            "path": info.path,
            "note": info.note,
        }
    node = shutil.which("node")
    node_ver = None
    if node:
        try:
            proc = subprocess.run(
                [node, "-v"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                shell=False,
            )
            node_ver = (proc.stdout or proc.stderr or "").strip().splitlines()[:1]
            node_ver = node_ver[0] if node_ver else None
        except (OSError, subprocess.TimeoutExpired):
            node_ver = None
    js_root = Path(cfg.limits.js_workspace)
    js_writable = False
    try:
        js_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = js_root / ".health"
        probe.write_text("ok", encoding="utf-8")
        os.chmod(probe, 0o600)
        probe.unlink(missing_ok=True)
        js_writable = True
    except OSError:
        js_writable = False
    scope_ok = bool(cfg.scope.allowed_domains or cfg.scope.allowed_cidrs)
    return {
        "binaries": binaries,
        "node": {"path": node, "version": node_ver},
        "scope": {
            "configured": scope_ok,
            "allowed_domains": list(cfg.scope.allowed_domains),
            "allowed_cidrs": list(cfg.scope.allowed_cidrs),
            "allow_private": cfg.scope.allow_private,
        },
        "privilege": {
            "allow_syn_scan": cfg.privilege.allow_syn_scan,
            "has_net_raw": has_net_raw(),
            "fallback_connect": cfg.privilege.fallback_connect,
        },
        "limits": {
            "max_hosts": cfg.limits.max_hosts,
            "max_output_bytes": cfg.limits.max_output_bytes,
            "js_workspace": cfg.limits.js_workspace,
            "nuclei_templates_dir": cfg.limits.nuclei_templates_dir,
            "restringer_sandbox": cfg.limits.restringer_sandbox,
        },
        "js_workspace_writable": js_writable,
        "jobs": {"enabled": cfg.jobs.enabled, "dir": cfg.jobs.dir},
    }


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.RECON_HEALTH.strip())
    async def recon_health() -> str:
        """Inventory recon binaries and scope. Does not probe keys or scan."""
        try:
            return dumps(collect_health())
        except ReconError as exc:
            fail(exc)
            raise
