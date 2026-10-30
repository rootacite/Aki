"""Download in-scope JS into js_workspace. Not an MCP tool."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import get_config
from .errors import ReconError
from .scope import assert_in_scope

_JS_TYPE = re.compile(r"(javascript|ecmascript|json)", re.I)


class _ScopedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        assert_in_scope(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_js(url: str, dest_dir: Path, *, max_bytes: int | None = None) -> Path:
    assert_in_scope(url)
    cfg = get_config()
    limit = cfg.limits.restringer_max_input_bytes if max_bytes is None else max_bytes
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "mcp-recon/0.1"}, method="GET")
    opener = build_opener(_ScopedRedirect)
    with opener.open(req, timeout=20) as resp:
        final = resp.geturl()
        assert_in_scope(final)
        ctype = resp.headers.get("Content-Type") or ""
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ReconError(f"JS download exceeds {limit} bytes")
    if not data:
        raise ReconError("empty JS download")
    pathish = url.split("?", 1)[0]
    if (
        not _JS_TYPE.search(ctype)
        and not pathish.lower().endswith(".js")
        and "javascript" not in ctype.lower()
    ):
        raise ReconError(f"refusing non-JS content-type {ctype!r} for {url}")
    name = Path(pathish.rstrip("/")).name or "download.js"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.endswith(".js"):
        name += ".js"
    dest = (dest_dir / name).resolve()
    if not dest.is_relative_to(dest_dir.resolve()):
        raise ReconError("download path escaped workspace")
    dest.write_bytes(data)
    os.chmod(dest, 0o600)
    return dest
