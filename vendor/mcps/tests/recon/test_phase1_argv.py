from __future__ import annotations

from pathlib import Path

import pytest

from mcps.tools.recon.errors import ReconError
from mcps.tools.recon.tools_amass import build_amass_intel_argv, build_amass_passive_argv
from mcps.tools.recon.tools_bbot import build_bbot_argv
from mcps.tools.recon.tools_dnsx import build_dnsx_argv
from mcps.tools.recon.tools_subfinder import build_subfinder_argv


def test_subfinder_default(recon_cfg):
    argv = build_subfinder_argv("example.com")
    assert argv[:5] == ["subfinder", "-d", "example.com", "-silent", "-oJ"]
    assert "-all" not in argv
    assert "-active" not in argv


def test_subfinder_all_sources(recon_cfg):
    argv = build_subfinder_argv("example.com", all_sources=True)
    assert "-all" in argv


def test_subfinder_rejects_bad_timeout(recon_cfg):
    with pytest.raises(ReconError):
        build_subfinder_argv("example.com", timeout_s=0)
    with pytest.raises(ReconError):
        build_subfinder_argv("example.com", timeout_s=10_000)


def test_dnsx_default_records(recon_cfg):
    argv, stdin = build_dnsx_argv(["a.example.com", "192.168.1.1"])
    assert argv[:4] == ["dnsx", "-l", "/dev/stdin", "-silent"]
    assert "-j" in argv
    assert "-a" in argv and "-aaaa" in argv and "-cname" in argv
    assert "-axfr" not in argv
    assert "a.example.com" in stdin


def test_dnsx_too_many_hosts(recon_cfg, monkeypatch):
    from mcps.tools.recon.config import get_config

    hosts = [f"h{i}.example.com" for i in range(get_config().limits.max_hosts + 1)]
    with pytest.raises(ReconError, match="max_hosts"):
        build_dnsx_argv(hosts)


def test_dnsx_rejects_axfr(recon_cfg):
    with pytest.raises(ReconError, match="not allowed"):
        build_dnsx_argv(["example.com"], records=["AXFR"])
    with pytest.raises(ReconError, match="unsupported"):
        build_dnsx_argv(["example.com"], records=["BOGUS"])


def test_amass_passive_default_not_active(recon_cfg, tmp_path):
    argv = build_amass_passive_argv("example.com", out_dir=str(tmp_path))
    assert argv[:4] == ["amass", "enum", "-d", "example.com"]
    assert "-active" not in argv
    assert "-brute" not in argv
    assert "-alts" not in argv
    assert "-rigid" in argv
    assert argv[argv.index("-dir") + 1] == str(tmp_path)
    assert argv[argv.index("-log") + 1].startswith(str(tmp_path))
    assert argv[argv.index("-oA") + 1].startswith(str(tmp_path))


def test_amass_does_not_forge_home():
    src = Path("src/mcps/tools/recon/tools_amass.py").read_text(encoding="utf-8")
    assert "extra_env" not in src
    assert 'HOME"' not in src
    assert "XDG_" not in src


def test_amass_passive_requires_domain(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="domain"):
        build_amass_passive_argv("", out_dir=str(tmp_path))


def test_amass_passive_timeout_cap(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="long_timeout"):
        build_amass_passive_argv("example.com", timeout_s=10_000, out_dir=str(tmp_path))


def test_amass_intel_domain_and_asn(recon_cfg, tmp_path):
    argv = build_amass_intel_argv(domain="example.com", asn=15169, out_dir=str(tmp_path))
    assert "enum" in argv
    assert "-d" in argv and "example.com" in argv
    assert "-asn" in argv and "15169" in argv
    assert "-brute" not in argv


def test_amass_intel_org_unsupported(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="org"):
        build_amass_intel_argv(org="Example Inc", out_dir=str(tmp_path))


def test_amass_intel_requires_one_selector(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="domain and/or asn"):
        build_amass_intel_argv(out_dir=str(tmp_path))


def test_bbot_default_passive(recon_cfg, tmp_path):
    argv = build_bbot_argv("example.com", out_dir=str(tmp_path))
    assert argv[:5] == ["bbot", "-t", "example.com", "-p", "subdomain-enum"]
    assert "-rf" in argv and "passive" in argv
    assert "--no-deps" in argv
    assert "kitchen-sink" not in argv


def test_bbot_rejects_kitchen_sink(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="kitchen-sink"):
        build_bbot_argv("example.com", preset="kitchen-sink", out_dir=str(tmp_path))


def test_bbot_does_not_forge_home():
    src = Path("src/mcps/tools/recon/tools_bbot.py").read_text(encoding="utf-8")
    assert "extra_env" not in src
    assert 'HOME"' not in src


def test_bbot_rejects_unknown_preset(recon_cfg, tmp_path):
    with pytest.raises(ReconError, match="not allowed"):
        build_bbot_argv("example.com", preset="web-spider", out_dir=str(tmp_path))
