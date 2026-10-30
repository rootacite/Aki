from __future__ import annotations

import pytest

from mcps.tools.recon.errors import ReconError, ScopeError
from mcps.tools.recon.tools_health import collect_health
from mcps.tools.recon.workflows import _require_active, run_domain_passive


def test_active_requires_flag():
    with pytest.raises(ReconError, match="authorize_active"):
        _require_active(False)
    _require_active(True)


def test_passive_rejects_out_of_scope(recon_cfg):
    with pytest.raises(ScopeError):
        run_domain_passive("not-in-scope.example")


def test_health_inventory(recon_cfg):
    data = collect_health()
    assert "subfinder" in data["binaries"]
    assert "jshunter" in data["binaries"]
    assert "restringer" in data["binaries"]
    assert "node" in data
    assert "scope" in data
    assert "js_workspace_writable" in data
    assert data["binaries"]["httpx"]["found"] is True
    assert "httpx-pd" in (data["binaries"]["httpx"].get("path") or "")
