"""T16 ffuf_fuzz."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from . import descriptions
from .bins import require
from .common import assert_wordlist_path, run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_in_scope
from .util import dumps, fail, parse_jsonl, result_payload

Mode = Literal["dir", "vhost", "param"]
_FILTER = re.compile(r"^(all|[0-9]+(-[0-9]+)?)(,(all|[0-9]+(-[0-9]+)?))*$")
_EXT = re.compile(r"^[A-Za-z0-9]{1,8}(,[A-Za-z0-9]{1,8})*$")
_LARGE_LINES = 5000


def build_ffuf_argv(
    url_pattern: str,
    *,
    mode: str,
    wordlist: str,
    threads: int = 10,
    fc: str | None = None,
    fs: str | None = None,
    mc: str | None = None,
    extensions: str | None = None,
) -> list[str]:
    if mode not in {"dir", "vhost", "param"}:
        raise ReconError("mode must be dir, vhost, or param")
    if not url_pattern:
        raise ReconError("url_pattern is required")
    cfg = get_config()
    if threads < 1 or threads > cfg.limits.ffuf_max_threads:
        raise ReconError(f"threads must be 1–{cfg.limits.ffuf_max_threads}")
    wl = str(assert_wordlist_path(wordlist))
    argv = [
        "ffuf",
        "-u",
        url_pattern,
        "-w",
        wl,
        "-t",
        str(threads),
        "-json",
        "-noninteractive",
        "-s",
        "-timeout",
        "10",
    ]
    if mode == "dir":
        if "FUZZ" not in url_pattern:
            raise ReconError("dir mode url_pattern must contain FUZZ")
        if extensions:
            if not _EXT.match(extensions):
                raise ReconError("invalid extensions list")
            argv.extend(["-e", extensions])
    elif mode == "param":
        if "FUZZ" not in url_pattern:
            raise ReconError("param mode url_pattern must contain FUZZ")
    else:
        # vhost: fuzz Host header. URL host still scoped by caller.
        argv.extend(["-H", "Host: FUZZ"])
        if "FUZZ" not in url_pattern and "FUZZ" not in "Host: FUZZ":
            raise ReconError("vhost mode needs a FUZZ point")
    if fc:
        if not _FILTER.match(fc):
            raise ReconError("invalid fc filter")
        argv.extend(["-fc", fc])
    if fs:
        if not _FILTER.match(fs):
            raise ReconError("invalid fs filter")
        argv.extend(["-fs", fs])
    if mc:
        if not _FILTER.match(mc):
            raise ReconError("invalid mc filter")
        argv.extend(["-mc", mc])
    return argv


def _wordlist_is_large(path: str) -> bool:
    file = Path(path)
    try:
        if file.stat().st_size > 512_000:
            return True
        with file.open(encoding="utf-8", errors="replace") as fh:
            for i, _ in enumerate(fh, 1):
                if i > _LARGE_LINES:
                    return True
    except OSError:
        return False
    return False


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.FFUF_FUZZ.strip())
    async def ffuf_fuzz(
        url_pattern: str,
        mode: Mode = "dir",
        wordlist: str = "",
        threads: int = 10,
        fc: str | None = None,
        fs: str | None = None,
        mc: str | None = None,
        extensions: str | None = None,
        timeout_s: int = 120,
    ) -> str:
        """Controlled ffuf. Out of scope targets are rejected."""
        try:
            require("ffuf")
            if not wordlist:
                raise ReconError("wordlist is required")
            assert_in_scope(url_pattern.replace("FUZZ", "x"))
            argv = build_ffuf_argv(
                url_pattern,
                mode=mode,
                wordlist=wordlist,
                threads=threads,
                fc=fc,
                fs=fs,
                mc=mc,
                extensions=extensions,
            )
            wl = argv[argv.index("-w") + 1]
            if timeout_s > 60 or _wordlist_is_large(wl):
                return dumps(start_job(argv, timeout_s=timeout_s, tool="ffuf_fuzz"))
            result = run_recon(argv, timeout_s=timeout_s, tool="ffuf_fuzz")
            rows = parse_jsonl(result.stdout)
            slim = [
                {
                    "url": r.get("url"),
                    "status": r.get("status"),
                    "length": r.get("length"),
                    "words": r.get("words"),
                }
                for r in rows
            ]
            return dumps(result_payload(result, extra={"matches": slim, "count": len(slim)}))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
