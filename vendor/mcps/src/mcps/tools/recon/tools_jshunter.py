"""T12 jshunter_analyze — cc1a2b/jshunter only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import descriptions
from .bins import require
from .common import assert_js_workspace_file, new_js_job_dir, run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .jobs import start_job
from .scope import assert_in_scope
from .util import dumps, fail, parse_jsonl, redact_secret, result_payload


def build_jshunter_argv(
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    file: str | None = None,
    endpoints: bool = True,
    secrets: bool = True,
    params: bool = False,
    jwt: bool = False,
    graphql: bool = False,
    firebase: bool = False,
    deobfuscate: bool = False,
    threads: int = 3,
    json_path: str,
    list_path: str | None = None,
) -> list[str]:
    present = [x for x in (url, urls, file) if x]
    if len(present) != 1:
        raise ReconError("provide exactly one of url, urls, or file")
    cfg = get_config()
    if threads < 1 or threads > cfg.limits.jshunter_max_threads:
        raise ReconError(f"threads must be 1–{cfg.limits.jshunter_max_threads}")
    argv = ["jshunter", "-q", "--ndjson", "-o", json_path, "-t", str(threads)]
    if url:
        argv.extend(["-u", url])
    elif urls is not None:
        if not urls:
            raise ReconError("urls is empty")
        if len(urls) > cfg.limits.jshunter_max_urls:
            raise ReconError(f"urls exceeds jshunter_max_urls={cfg.limits.jshunter_max_urls}")
        if not list_path:
            raise ReconError("list_path is required for urls mode")
        argv.extend(["-l", list_path])
    else:
        argv.extend(["-f", file or ""])
    if endpoints:
        argv.append("-ep")
    if secrets:
        argv.append("-s")
    if params:
        argv.append("-P")
    if jwt:
        argv.append("-x")
    if graphql:
        argv.append("-g")
    if firebase:
        argv.append("-F")
    if deobfuscate:
        argv.append("-d")
    # No -H, -p, webhooks.
    return argv


def _summarize(raw: str) -> dict[str, Any]:
    endpoints: list[str] = []
    secrets: list[dict[str, str]] = []
    flags = {"params": False, "graphql": False, "firebase": False}
    for line in raw.splitlines():
        s = line.strip()
        if s.upper().startswith("ENDPOINT:"):
            endpoints.append(s.split(":", 1)[1].strip())
        elif s.upper().startswith("SECRET:") or s.upper().startswith("SECRETS:"):
            secrets.append({"type": "secret", "value": redact_secret(s.split(":", 1)[1].strip())})
    payload: Any = None
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        rows = parse_jsonl(raw)
        payload = {"records": rows} if rows else {}
    blobs: list[Any] = payload if isinstance(payload, list) else [payload]
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key in ("endpoints", "end_points", "urls", "results", "data"):
            val = blob.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        endpoints.append(item)
                    elif isinstance(item, dict):
                        u = item.get("url") or item.get("endpoint") or item.get("value") or item.get("match")
                        if isinstance(u, str):
                            endpoints.append(u)
                        nested = item.get("endpoints")
                        if isinstance(nested, list):
                            for n in nested:
                                if isinstance(n, str):
                                    endpoints.append(n)
        for key in ("secrets", "findings", "credentials"):
            val = blob.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        kind = str(item.get("type") or item.get("name") or "secret")
                        value = str(item.get("value") or item.get("match") or item.get("secret") or "")
                        secrets.append({"type": kind, "value": redact_secret(value)})
                    elif isinstance(item, str):
                        secrets.append({"type": "secret", "value": redact_secret(item)})
        if blob.get("params") or blob.get("parameters"):
            flags["params"] = True
        if blob.get("graphql"):
            flags["graphql"] = True
        if blob.get("firebase"):
            flags["firebase"] = True
    # de-dupe endpoints
    seen: set[str] = set()
    uniq: list[str] = []
    for ep in endpoints:
        if ep not in seen:
            seen.add(ep)
            uniq.append(ep)
    return {
        "endpoints": uniq[:200],
        "secrets": secrets[:100],
        "flags": flags,
    }


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.JSHUNTER_ANALYZE.strip())
    async def jshunter_analyze(
        url: str | None = None,
        urls: list[str] | None = None,
        file: str | None = None,
        endpoints: bool = True,
        secrets: bool = True,
        params: bool = False,
        jwt: bool = False,
        graphql: bool = False,
        firebase: bool = False,
        deobfuscate: bool = False,
        threads: int = 3,
        timeout_s: int = 180,
    ) -> str:
        """JS endpoint/secret extraction. Out of scope targets are rejected."""
        try:
            require("jshunter")
            work = new_js_job_dir()
            list_path = None
            file_path = None
            if url:
                assert_in_scope(url)
            if urls:
                if len(urls) > get_config().limits.jshunter_max_urls:
                    raise ReconError("urls exceeds jshunter_max_urls")
                for item in urls:
                    assert_in_scope(item)
                list_path = str(work / "urls.txt")
                (work / "urls.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
            if file:
                file_path = str(assert_js_workspace_file(file))
            json_path = str(work / "jshunter.json")
            argv = build_jshunter_argv(
                url=url,
                urls=urls,
                file=file_path,
                endpoints=endpoints,
                secrets=secrets,
                params=params,
                jwt=jwt,
                graphql=graphql,
                firebase=firebase,
                deobfuscate=deobfuscate,
                threads=threads,
                json_path=json_path,
                list_path=list_path,
            )
            use_job = timeout_s > 60 or (urls is not None and len(urls) > 10)
            if use_job:
                return dumps(start_job(argv, timeout_s=timeout_s, tool="jshunter_analyze"))
            result = run_recon(argv, timeout_s=timeout_s, tool="jshunter_analyze")
            raw = result.stdout
            if Path(json_path).is_file():
                raw = Path(json_path).read_text(encoding="utf-8", errors="replace")
            extra = _summarize(raw)
            extra["json_path"] = json_path
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
