"""Job status / logs / cancel MCP tools."""

from __future__ import annotations

from typing import Any

from . import descriptions
from .errors import ReconError
from .jobs import job_cancel, job_logs, job_status
from .util import dumps, fail


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.RECON_JOB_STATUS.strip())
    async def recon_job_status(job_id: str) -> str:
        """Poll a recon background job."""
        try:
            return dumps(job_status(job_id))
        except ReconError as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.RECON_JOB_LOGS.strip())
    async def recon_job_logs(job_id: str, tail: int = 80) -> str:
        """Tail logs for a recon job."""
        try:
            return dumps(job_logs(job_id, tail=tail))
        except ReconError as exc:
            fail(exc)
            raise

    @mcp.tool(description=descriptions.RECON_JOB_CANCEL.strip())
    async def recon_job_cancel(job_id: str) -> str:
        """Cancel a running recon job and kill its process group."""
        try:
            return dumps(job_cancel(job_id))
        except ReconError as exc:
            fail(exc)
            raise
