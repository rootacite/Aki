"""Background recon jobs. Base has no job system; this is the only one."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from .common import RunResult, run_recon
from .config import get_config
from .errors import ReconError

Status = Literal["running", "ok", "error", "cancelled", "timeout"]

_lock = threading.Lock()
_jobs: dict[str, Job] = {}


@dataclass
class Job:
    id: str
    status: Status
    argv_redacted: list[str]
    started_at: str
    finished_at: str | None = None
    result: RunResult | None = None
    pipeline_result: dict[str, Any] | None = None
    error: str | None = None
    log_path: Path | None = None
    _thread: threading.Thread | None = field(default=None, repr=False, compare=False)
    _cancel: threading.Event | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "argv_redacted": self.argv_redacted,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "log_path": str(self.log_path) if self.log_path else None,
            "result": self.result.to_dict() if self.result else None,
            "pipeline_result": self.pipeline_result,
        }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_dir(job_id: str) -> Path:
    cfg = get_config()
    root = Path(cfg.jobs.dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = (root / job_id).resolve()
    if not dest.is_relative_to(root):
        raise ReconError("job path escaped jobs.dir")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest


def start_job(
    argv: list[str],
    *,
    timeout_s: int,
    max_output_bytes: int | None = None,
    input_text: str | None = None,
    need_raw_socket: bool = False,
    tool: str = "job",
    operator: str = "mcp",
    cwd: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = get_config()
    if not cfg.jobs.enabled:
        raise ReconError("recon jobs are disabled in config")
    job_id = str(uuid.uuid4())
    dest = _job_dir(job_id)
    log_path = dest / "job.log"
    cancel = threading.Event()
    job = Job(
        id=job_id,
        status="running",
        argv_redacted=[],
        started_at=_now(),
        log_path=log_path,
        _cancel=cancel,
    )
    with _lock:
        _jobs[job_id] = job

    def _run() -> None:
        try:
            result = run_recon(
                argv,
                timeout_s=timeout_s,
                max_output_bytes=max_output_bytes,
                input_text=input_text,
                need_raw_socket=need_raw_socket,
                tool=tool,
                job_id=job_id,
                operator=operator,
                cancel_event=cancel,
                cwd=cwd,
                extra_env=extra_env,
            )
            job.argv_redacted = result.argv_redacted
            job.result = result
            if job.status == "cancelled":
                pass
            elif result.timed_out:
                job.status = "timeout"
            elif result.ok:
                job.status = "ok"
            else:
                job.status = "error"
            _write_log(log_path, result)
        except Exception as exc:  # noqa: BLE001 - surface into job status
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = _now()
            _write_meta(dest, job)

    thread = threading.Thread(target=_run, name=f"recon-job-{job_id[:8]}", daemon=True)
    job._thread = thread
    thread.start()
    return {"job_id": job_id, "status": "running"}


def start_fn_job(
    fn: Callable[[], dict[str, Any]],
    *,
    tool: str,
    operator: str = "mcp",
    argv_redacted: list[str] | None = None,
) -> dict[str, Any]:
    """Run a Python pipeline in a thread (multi-step workflows)."""
    cfg = get_config()
    if not cfg.jobs.enabled:
        raise ReconError("recon jobs are disabled in config")
    job_id = str(uuid.uuid4())
    dest = _job_dir(job_id)
    log_path = dest / "job.log"
    cancel = threading.Event()
    job = Job(
        id=job_id,
        status="running",
        argv_redacted=argv_redacted or [f"pipeline:{tool}"],
        started_at=_now(),
        log_path=log_path,
        _cancel=cancel,
    )
    with _lock:
        _jobs[job_id] = job

    def _run() -> None:
        try:
            if cancel.is_set():
                job.status = "cancelled"
                return
            payload = fn()
            job.pipeline_result = payload
            job.status = "cancelled" if cancel.is_set() else "ok"
            log_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2)[:200_000],
                encoding="utf-8",
            )
            os.chmod(log_path, 0o600)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            log_path.write_text(job.error or "", encoding="utf-8")
        finally:
            job.finished_at = _now()
            _write_meta(dest, job)

    thread = threading.Thread(target=_run, name=f"recon-pipe-{job_id[:8]}", daemon=True)
    job._thread = thread
    thread.start()
    return {"job_id": job_id, "status": "running", "tool": tool}


def job_status(job_id: str) -> dict[str, Any]:
    job = _get(job_id)
    return job.to_dict()


def job_logs(job_id: str, *, tail: int = 80) -> dict[str, Any]:
    if tail < 1 or tail > 2000:
        raise ReconError("tail must be between 1 and 2000")
    job = _get(job_id)
    text = ""
    if job.log_path and job.log_path.is_file():
        text = job.log_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return {
        "job_id": job_id,
        "status": job.status,
        "tail": lines[-tail:],
    }


def job_cancel(job_id: str) -> dict[str, Any]:
    job = _get(job_id)
    if job.status == "running":
        job.status = "cancelled"
        job.error = "cancelled by operator"
        if job._cancel is not None:
            job._cancel.set()
    return job.to_dict()


def reset_jobs() -> None:
    with _lock:
        _jobs.clear()


def _get(job_id: str) -> Job:
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        raise ReconError(f"unknown job_id: {job_id}")
    return job


def _write_log(path: Path, result: RunResult) -> None:
    body = (
        f"exit_code={result.exit_code} timed_out={result.timed_out} "
        f"truncated={result.truncated} privilege={result.privilege_note}\n"
        f"argv={result.argv_redacted}\n"
        "--- stdout ---\n"
        f"{result.stdout}\n"
        "--- stderr ---\n"
        f"{result.stderr}\n"
    )
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def _write_meta(dest: Path, job: Job) -> None:
    meta = dest / "meta.json"
    payload = job.to_dict()
    meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(meta, 0o600)
