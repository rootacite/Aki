"""T11 katana_crawl."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import descriptions
from .bins import require
from .common import run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_in_scope
from .util import dumps, fail, parse_jsonl, result_payload


def _ensure_katana_user_config_dir() -> None:
    # katana-bin always writes ~/.config/katana/config.yaml (not XDG).
    # Create that real directory; never rewrite HOME.
    dest = Path.home() / ".config" / "katana"
    try:
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ReconError(
            f"katana cannot write {dest}: {exc}. "
            "Needs a writable real ~/.config/katana (no fake HOME)."
        ) from exc


def build_katana_argv(
    url: str,
    *,
    depth: int = 2,
    max_urls: int = 200,
    timeout_s: int = 180,
) -> list[str]:
    if not url:
        raise ReconError("url is required")
    if depth < 1 or depth > 3:
        raise ReconError("depth must be 1–3")
    if max_urls < 1 or max_urls > 500:
        raise ReconError("max_urls out of range")
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    return [
        "katana",
        "-u",
        url,
        "-d",
        str(depth),
        "-jc",
        "-j",
        "-silent",
        "-nc",
        "-duc",
        "-fs",
        "rdn",
        "-mdp",
        str(max_urls),
        "-ct",
        f"{timeout_s}s",
        "-or",
        "-ob",
    ]


def _row_urls(row: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("url", "endpoint"):
        val = row.get(key)
        if isinstance(val, str) and val.startswith("http"):
            found.append(val)
    req = row.get("request")
    if isinstance(req, str) and req.startswith("http"):
        found.append(req)
    elif isinstance(req, dict):
        for key in ("endpoint", "url"):
            val = req.get(key)
            if isinstance(val, str) and val.startswith("http"):
                found.append(val)
    return found


def extract_urls(rows: list[dict[str, Any]], lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        for item in _row_urls(row):
            if item not in seen:
                seen.add(item)
                out.append(item)
    if not out:
        for ln in lines:
            s = ln.strip()
            if s.startswith("http") and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _is_js_url(item: str) -> bool:
    path = urlsplit(item).path.lower()
    return path.endswith(".js") or path.endswith(".mjs")


def _in_scope(item: str) -> bool:
    try:
        assert_in_scope(item)
        return True
    except ScopeError:
        return False


def _js_urls(rows: list[dict[str, Any]], lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for item in extract_urls(rows, lines):
        if _is_js_url(item) and item not in seen:
            seen.add(item)
            found.append(item)
    return found


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.KATANA_CRAWL.strip())
    async def katana_crawl(
        url: str,
        depth: int = 2,
        max_urls: int = 200,
        timeout_s: int = 180,
    ) -> str:
        """Crawl and harvest JS URLs. Out of scope targets are rejected."""
        try:
            require("katana")
            assert_in_scope(url)
            _ensure_katana_user_config_dir()
            argv = build_katana_argv(
                url, depth=depth, max_urls=max_urls, timeout_s=timeout_s
            )
            if timeout_s > 60:
                return dumps(
                    start_job(argv, timeout_s=timeout_s, tool="katana_crawl")
                )
            result = run_recon(argv, timeout_s=timeout_s, tool="katana_crawl")
            rows = parse_jsonl(result.stdout)
            urls = [u for u in extract_urls(rows, result.stdout.splitlines()) if _in_scope(u)]
            extra = {
                "urls": urls[: get_config().limits.max_hosts],
                "js_urls": [u for u in _js_urls(rows, result.stdout.splitlines()) if _in_scope(u)],
            }
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
