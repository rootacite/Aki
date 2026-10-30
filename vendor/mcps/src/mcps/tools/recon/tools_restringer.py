"""T13 restringer_deobfuscate."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import descriptions
from .bins import require
from .common import (
    assert_js_workspace_file,
    new_js_job_dir,
    run_recon,
    truncate_for_model,
)
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .fetch import fetch_js
from .util import dumps, fail, result_payload

_VENDOR = re.compile(
    r"(jquery|react|vue|angular|bootstrap|lodash|moment)([.-]|$)",
    re.I,
)


def _isolated_vm_dirs(restringer_bin: Path) -> list[Path]:
    root = restringer_bin.resolve().parent.parent
    return [
        root / "node_modules" / "isolated-vm",
        Path("/usr/lib/node_modules/isolated-vm"),
        Path("/usr/local/lib/node_modules/isolated-vm"),
    ]


def _sandbox_name() -> str:
    cfg = get_config()
    name = cfg.limits.restringer_sandbox
    if name == "isolated-vm":
        bin_path = Path(require("restringer"))
        if not any(p.exists() for p in _isolated_vm_dirs(bin_path)):
            raise ReconError(
                "restringer sandbox isolated-vm is not resolvable "
                "(not next to restringer and not in global node_modules)"
            )
    return name


def build_restringer_argv(
    infile: str,
    *,
    outfile: str,
    clean: bool = False,
    max_iterations: int = 5,
) -> list[str]:
    cfg = get_config()
    if max_iterations < 1 or max_iterations > cfg.limits.restringer_max_iterations:
        raise ReconError(
            f"max_iterations must be 1–{cfg.limits.restringer_max_iterations}"
        )
    argv = [
        "restringer",
        "--sandbox",
        cfg.limits.restringer_sandbox,
        "--sb-timeout",
        str(cfg.limits.restringer_sb_timeout_ms),
        "-m",
        str(max_iterations),
        "-o",
        outfile,
        infile,
    ]
    if clean:
        argv.insert(-1, "--clean")
    return argv


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.RESTRINGER_DEOBFUSCATE.strip())
    async def restringer_deobfuscate(
        file: str | None = None,
        source_url: str | None = None,
        clean: bool = False,
        max_iterations: int = 5,
        timeout_s: int = 60,
    ) -> str:
        """Deobfuscate workspace JS. Out of scope targets are rejected."""
        try:
            require("restringer")
            cfg = get_config()
            _sandbox_name()
            work = new_js_job_dir()
            if bool(file) == bool(source_url):
                raise ReconError("provide exactly one of file or source_url")
            if source_url:
                src = fetch_js(source_url, work, max_bytes=cfg.limits.restringer_max_input_bytes)
            else:
                src = assert_js_workspace_file(file or "")
            if _VENDOR.search(src.name):
                raise ReconError(
                    f"refusing likely vendor bundle {src.name}; restringer is for business JS"
                )
            size = src.stat().st_size
            if size > cfg.limits.restringer_max_input_bytes:
                raise ReconError("input exceeds restringer_max_input_bytes")
            outfile = work / f"{src.stem}-deob.js"
            argv = build_restringer_argv(
                str(src),
                outfile=str(outfile),
                clean=clean,
                max_iterations=max_iterations,
            )
            # Caller cannot choose -o; outfile is always under js_workspace.
            result = run_recon(argv, timeout_s=timeout_s, tool="restringer_deobfuscate")
            changed = False
            preview = ""
            out_size = 0
            if outfile.is_file():
                out_bytes = outfile.read_bytes()
                out_size = len(out_bytes)
                os.chmod(outfile, 0o600)
                changed = out_bytes != src.read_bytes()
                text = out_bytes.decode("utf-8", errors="replace")
                preview, _ = truncate_for_model(text, max_lines=200)
            extra = {
                "output_path": str(outfile) if outfile.is_file() else None,
                "changed": changed,
                "input_bytes": size,
                "output_bytes": out_size,
                "preview": preview,
            }
            payload = result_payload(result, extra=extra)
            sandbox_fail = "is not installed" in result.stderr or "Critical Error" in result.stderr
            if not outfile.is_file():
                if sandbox_fail or not result.ok:
                    payload["ok"] = False
                else:
                    text = src.read_text(encoding="utf-8", errors="replace")
                    preview, _ = truncate_for_model(text, max_lines=200)
                    extra["preview"] = preview
                    extra["note"] = "no obfuscation changes; restringer omitted -o"
                    payload = result_payload(result, extra=extra)
            return dumps(payload)
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
