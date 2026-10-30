from __future__ import annotations

import shutil
import time

from mcps.tools.recon.jobs import job_cancel, job_logs, job_status, start_job


def _wait(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = job_status(job_id)
        if st["status"] != "running":
            return st
        time.sleep(0.05)
    return job_status(job_id)


def test_job_runs_printf(recon_cfg, allow_exec):
    printf = shutil.which("printf")
    assert printf
    allow_exec[printf] = printf
    started = start_job([printf, "hello-job"], timeout_s=5, max_output_bytes=64)
    assert started["status"] == "running"
    st = _wait(started["job_id"])
    assert st["status"] == "ok"
    assert st["result"]["stdout"] == "hello-job"
    logs = job_logs(started["job_id"], tail=20)
    assert any("hello-job" in line for line in logs["tail"])


def test_job_cancel_sleep(recon_cfg, allow_exec):
    sleep = shutil.which("sleep")
    assert sleep
    allow_exec[sleep] = sleep
    started = start_job([sleep, "30"], timeout_s=20, max_output_bytes=16)
    job_id = started["job_id"]
    cancelled = job_cancel(job_id)
    assert cancelled["status"] == "cancelled"
    st = _wait(job_id, timeout=3)
    assert st["status"] == "cancelled"
