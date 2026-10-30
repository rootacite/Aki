"""T17–T19 orchestration tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import descriptions
from .bins import which
from .common import new_js_job_dir, run_recon
from .config import get_config
from .errors import BinUnavailable, ReconError, ScopeError
from .fetch import fetch_js
from .jobs import start_fn_job
from .scope import assert_in_scope, assert_scan_targets
from .tools_dnsx import build_dnsx_argv
from .tools_httpx import _summarize as httpx_summarize
from .tools_httpx import build_httpx_argv
from .tools_jshunter import _summarize as jshunter_summarize
from .tools_jshunter import build_jshunter_argv
from .http_filter import for_httpx, live_http_urls
from .tools_katana import _ensure_katana_user_config_dir, _js_urls, build_katana_argv
from .tools_naabu import _hostports, build_naabu_argv
from .tools_nuclei import _TECH_TAGS, build_nuclei_argv
from .tools_restringer import _VENDOR, build_restringer_argv
from .tools_subfinder import build_subfinder_argv
from .tools_whatweb import build_whatweb_argv
from .util import dumps, fail, parse_jsonl, unique_hosts_from_jsonl

_OBFUS = re.compile(r"_0x[a-f0-9]{4,}|obfuscator\.io|jsjiami", re.I)


def _require_active(authorize_active: bool) -> None:
    if not authorize_active:
        raise ReconError("authorize_active=true is required for this workflow")


def run_domain_passive(domain: str, *, timeout_s: int = 120) -> dict[str, Any]:
    assert_in_scope(domain)
    failed: list[str] = []
    hosts: list[str] = [domain]
    if which("subfinder"):
        argv = build_subfinder_argv(domain, timeout_s=timeout_s)
        result = run_recon(argv, timeout_s=timeout_s, tool="recon_domain_passive")
        if result.ok:
            found = unique_hosts_from_jsonl(parse_jsonl(result.stdout))
            for h in found:
                if h not in hosts:
                    hosts.append(h)
        else:
            failed.append(f"subfinder exit={result.exit_code}")
    else:
        failed.append("subfinder unavailable")
    if which("amass"):
        failed.append("amass_passive skipped in-pipeline (long job; call amass_passive)")
    resolved: list[dict[str, Any]] = []
    if which("dnsx") and hosts:
        scoped = []
        for h in hosts[: get_config().limits.max_hosts]:
            try:
                assert_in_scope(h)
                scoped.append(h)
            except ScopeError:
                continue
        if scoped:
            argv, stdin = build_dnsx_argv(scoped)
            result = run_recon(
                argv, timeout_s=min(timeout_s, 60), input_text=stdin, tool="recon_domain_passive"
            )
            if result.ok:
                resolved = parse_jsonl(result.stdout)
            else:
                failed.append(f"dnsx exit={result.exit_code}")
    else:
        failed.append("dnsx unavailable or no hosts")
    return {
        "domain": domain,
        "hostnames": hosts,
        "resolved": resolved,
        "failed_steps": failed,
    }


def run_web_active(
    *,
    domain: str | None,
    targets: list[str] | None,
    whatweb: bool = True,
) -> dict[str, Any]:
    cfg = get_config()
    hosts: list[str] = []
    if domain:
        assert_in_scope(domain)
        hosts.append(domain)
        if which("dnsx"):
            argv, stdin = build_dnsx_argv([domain])
            result = run_recon(argv, timeout_s=60, input_text=stdin, tool="recon_web_active")
            for row in parse_jsonl(result.stdout):
                h = row.get("host") or row.get("input")
                if isinstance(h, str) and h not in hosts:
                    try:
                        assert_in_scope(h)
                        hosts.append(h)
                    except ScopeError:
                        continue
    if targets:
        assert_scan_targets(targets, max_items=cfg.limits.max_hosts)
        for t in targets:
            if t not in hosts:
                hosts.append(t)
    if not hosts:
        raise ReconError("provide domain or targets")
    naabu_ports: list[str] = []
    if which("naabu"):
        argv, stdin, note, need_raw = build_naabu_argv(hosts[: cfg.limits.max_hosts])
        result = run_recon(
            argv,
            timeout_s=120,
            input_text=stdin,
            need_raw_socket=need_raw,
            tool="recon_web_active",
        )
        naabu_ports = _hostports(parse_jsonl(result.stdout))
        scan_note = note
    else:
        scan_note = "naabu unavailable"
    http_targets = for_httpx(naabu_ports or hosts, fallback_hosts=hosts)
    endpoints: list[dict[str, Any]] = []
    if which("httpx") and http_targets:
        argv, stdin = build_httpx_argv(http_targets[: cfg.limits.max_hosts])
        result = run_recon(argv, timeout_s=90, input_text=stdin, tool="recon_web_active")
        endpoints = httpx_summarize(parse_jsonl(result.stdout))
    live_urls = live_http_urls(endpoints)
    tech: list[dict[str, Any]] = []
    if which("nuclei") and live_urls:
        argv = build_nuclei_argv(
            live_urls[:20], tags=list(_TECH_TAGS), severity=["info", "low"]
        )
        result = run_recon(
            argv,
            timeout_s=120,
            input_text="\n".join(live_urls[:20]) + "\n",
            tool="recon_web_active",
        )
        tech = parse_jsonl(result.stdout)
    fingerprints: Any = None
    if whatweb and which("whatweb") and live_urls:
        argv = build_whatweb_argv(live_urls[:5], aggression=1)
        result = run_recon(argv, timeout_s=45, tool="recon_web_active")
        try:
            fingerprints = json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError:
            fingerprints = result.stdout[:2000]
    return {
        "hosts": hosts,
        "host_ports": naabu_ports,
        "scan_type_note": scan_note,
        "http": endpoints,
        "nuclei_tech": tech,
        "whatweb": fingerprints,
    }


def _looks_obfuscated(text: str) -> bool:
    if _OBFUS.search(text):
        return True
    lines = text.splitlines() or [text]
    if max(len(ln) for ln in lines) > 8000:
        return True
    if len(lines) <= 2 and len(text) > 12_000:
        return True
    return False


def run_js_analyze(
    *,
    start_url: str | None,
    js_urls: list[str] | None,
) -> dict[str, Any]:
    cfg = get_config()
    urls: list[str] = list(js_urls or [])
    if start_url:
        assert_in_scope(start_url)
        if which("katana"):
            try:
                _ensure_katana_user_config_dir()
            except ReconError:
                pass
            argv = build_katana_argv(start_url, depth=2, max_urls=80, timeout_s=45)
            result = run_recon(argv, timeout_s=50, tool="recon_js_analyze")
            rows = parse_jsonl(result.stdout)
            urls.extend(_js_urls(rows, result.stdout.splitlines()))
        if not urls and which("httpx"):
            argv, stdin = build_httpx_argv([start_url])
            result = run_recon(argv, timeout_s=30, input_text=stdin, tool="recon_js_analyze")
            # no js harvest from httpx body; keep start_url only if .js
            if start_url.lower().split("?", 1)[0].endswith(".js"):
                urls.append(start_url)
    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        if u in seen:
            continue
        try:
            assert_in_scope(u)
        except ScopeError:
            continue
        seen.add(u)
        uniq.append(u)
    uniq = uniq[:20]
    work = new_js_job_dir()
    all_endpoints: list[str] = []
    all_secrets: list[dict[str, str]] = []
    deob: list[str] = []
    analyzed: list[str] = []
    for url in uniq:
        analyzed.append(url)
        if which("jshunter"):
            json_path = str(work / "jshunter.json")
            argv = build_jshunter_argv(url=url, json_path=json_path, threads=3)
            result = run_recon(argv, timeout_s=45, tool="recon_js_analyze")
            raw = result.stdout
            if Path(json_path).is_file():
                raw = Path(json_path).read_text(encoding="utf-8", errors="replace")
            summary = jshunter_summarize(raw)
            all_endpoints.extend(summary.get("endpoints") or [])
            all_secrets.extend(summary.get("secrets") or [])
        try:
            local = fetch_js(url, work)
        except (ReconError, OSError):
            continue
        if _VENDOR.search(local.name):
            continue
        try:
            text = local.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _looks_obfuscated(text):
            continue
        if local.stat().st_size > cfg.limits.restringer_max_input_bytes:
            continue
        if which("restringer"):
            out = work / f"{local.stem}-deob.js"
            argv = build_restringer_argv(str(local), outfile=str(out))
            run_recon(argv, timeout_s=40, tool="recon_js_analyze")
            if out.is_file():
                deob.append(str(out))
                if which("jshunter"):
                    json_path = str(work / "jshunter-deob.json")
                    argv = build_jshunter_argv(
                        file=str(out), json_path=json_path, threads=2
                    )
                    result = run_recon(argv, timeout_s=40, tool="recon_js_analyze")
                    summary = jshunter_summarize(result.stdout)
                    all_endpoints.extend(summary.get("endpoints") or [])
                    all_secrets.extend(summary.get("secrets") or [])
    ep_seen: set[str] = set()
    endpoints = []
    for e in all_endpoints:
        if e not in ep_seen:
            ep_seen.add(e)
            endpoints.append(e)
    return {
        "js_urls": uniq,
        "analyzed": analyzed,
        "endpoints": endpoints[:200],
        "secrets": all_secrets[:100],
        "deobfuscated": deob,
        "workspace": str(work),
    }


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.RECON_DOMAIN_PASSIVE.strip())
    async def recon_domain_passive(domain: str, timeout_s: int = 120) -> str:
        """Passive subdomain + DNS. Out of scope targets are rejected."""
        try:
            return dumps(run_domain_passive(domain, timeout_s=timeout_s))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.RECON_WEB_ACTIVE.strip())
    async def recon_web_active(
        domain: str | None = None,
        targets: list[str] | None = None,
        authorize_active: bool = False,
        timeout_s: int = 600,
    ) -> str:
        """Active web recon pipeline. Out of scope targets are rejected."""
        try:
            _require_active(authorize_active)
            return dumps(
                start_fn_job(
                    lambda: run_web_active(domain=domain, targets=targets),
                    tool="recon_web_active",
                    argv_redacted=["pipeline:recon_web_active"],
                )
            )
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.RECON_JS_ANALYZE.strip())
    async def recon_js_analyze(
        start_url: str | None = None,
        js_urls: list[str] | None = None,
        authorize_active: bool = False,
        timeout_s: int = 600,
    ) -> str:
        """JS harvest + jshunter + optional restringer. Out of scope targets are rejected."""
        try:
            _require_active(authorize_active)
            if not start_url and not js_urls:
                raise ReconError("provide start_url or js_urls")
            return dumps(
                start_fn_job(
                    lambda: run_js_analyze(start_url=start_url, js_urls=js_urls),
                    tool="recon_js_analyze",
                    argv_redacted=["pipeline:recon_js_analyze"],
                )
            )
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
