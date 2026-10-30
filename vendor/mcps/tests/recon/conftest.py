from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcps.tools.recon import bins, config as recon_config, jobs


@pytest.fixture
def recon_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> recon_config.ReconConfig:
    data = {
        "scope": {
            "allowed_domains": ["example.com"],
            "allowed_cidrs": ["8.8.8.0/24", "1.1.1.0/24"],
            "allow_private": False,
            "allow_subdomains": True,
        },
        "privilege": {
            "allow_syn_scan": True,
            "allow_sudo": True,
            "sudo_bins": ["nmap", "naabu", "masscan"],
            "fallback_connect": True,
        },
        "limits": {
            "max_output_bytes": 1024,
            "max_lines_to_model": 5,
            "js_workspace": str(tmp_path / "js"),
        },
        "wordlists": {"allowed_prefixes": [str(tmp_path / "wordlists") + "/"]},
        "jobs": {"enabled": True, "dir": str(tmp_path / "jobs")},
        "env": {
            "passthrough": ["PATH", "HOME", "USER", "LANG", "TZ"],
            "passthrough_prefixes": ["NUCLEI_"],
        },
    }
    (tmp_path / "wordlists").mkdir()
    path = tmp_path / "recon.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("MCP_RECON_CONFIG", str(path))
    recon_config.reset_config()
    jobs.reset_jobs()
    yield recon_config.get_config()
    recon_config.reset_config()
    jobs.reset_jobs()


@pytest.fixture
def allow_exec(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Permit extra absolute executables in tests (echo/sleep/printf)."""
    extra: dict[str, str] = {}
    real = bins.resolve_allowed

    def resolve(argv0: str) -> str:
        if argv0 in extra:
            return extra[argv0]
        abs_map = {Path(v).resolve(): v for v in extra.values()}
        p = Path(argv0)
        if p.is_absolute() and p.resolve() in abs_map:
            return str(p.resolve())
        return real(argv0)

    monkeypatch.setattr(bins, "resolve_allowed", resolve)
    return extra
