from __future__ import annotations

import pytest

from mcps.tools.recon.bins import BinUnavailable, inspect, require, which


def test_unknown_name_unavailable(recon_cfg):
    with pytest.raises(BinUnavailable, match="unknown recon binary"):
        require("sqlmap")
    info = inspect("sqlmap")
    assert info.available is False


def test_missing_optional_bin(recon_cfg, monkeypatch):
    monkeypatch.setattr(
        "mcps.tools.recon.bins.shutil.which",
        lambda name: None,
    )
    with pytest.raises(BinUnavailable, match="theHarvester not found"):
        require("theHarvester")
    assert which("theHarvester") is None


def test_httpx_does_not_bind_python_httpx(recon_cfg):
    info = inspect("httpx")
    if info.path:
        assert "python" not in info.note.lower() or info.identity_ok
        assert not info.path.endswith("/httpx") or info.identity_ok is False
        if info.identity_ok:
            assert info.path.endswith("httpx-pd") or "projectdiscovery" in info.note.lower()


def test_jshunter_identity_when_present(recon_cfg):
    info = inspect("jshunter")
    if info.available:
        assert info.identity_ok
        assert "cc1a2b" in info.note
