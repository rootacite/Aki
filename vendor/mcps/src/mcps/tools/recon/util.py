"""Helpers shared by recon MCP tools (not subprocess)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

from .common import RunResult, truncate_for_model
from .config import get_config
from .errors import ReconError

_SAMPLE_PER_TYPE = 8


def fail(exc: BaseException) -> None:
    raise ToolError(str(exc)) from exc


def redact_secret(value: str) -> str:
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}…{value[-4:]}"


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def unique_hosts_from_jsonl(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    hosts: list[str] = []
    for row in rows:
        host = row.get("host") or row.get("hostname") or row.get("input")
        if isinstance(host, str) and host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def result_payload(
    result: RunResult,
    *,
    extra: dict[str, Any] | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    cfg = get_config()
    stdout, stout_cut = truncate_for_model(result.stdout, max_lines=cfg.limits.max_lines_to_model)
    stderr, sterr_cut = truncate_for_model(result.stderr, max_lines=40)
    payload: dict[str, Any] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "truncated": result.truncated or stout_cut or sterr_cut,
        "duration_ms": result.duration_ms,
        "argv_redacted": result.argv_redacted,
        "privilege_note": result.privilege_note,
        "stderr_tail": stderr,
    }
    if include_raw:
        payload["stdout_tail"] = stdout
    if extra:
        payload.update(extra)
    return payload


def summarize_jsonl_events(text: str, *, sample: int = _SAMPLE_PER_TYPE) -> dict[str, Any]:
    counts: dict[str, int] = {}
    samples: dict[str, list[Any]] = {}
    for row in parse_jsonl(text):
        kind = str(row.get("type") or row.get("event_type") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
        bucket = samples.setdefault(kind, [])
        if len(bucket) < sample:
            data = row.get("data", row)
            bucket.append(data)
    return {"event_counts": counts, "samples": samples}


def ensure_job_workdir(prefix: str) -> Path:
    cfg = get_config()
    root = Path(cfg.jobs.dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = (root / prefix).resolve()
    if not dest.is_relative_to(root):
        raise ReconError("workdir escaped jobs.dir")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest
