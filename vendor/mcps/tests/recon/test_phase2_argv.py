from __future__ import annotations

import pytest

from mcps.tools.recon.common import resolve_scan_type
from mcps.tools.recon.errors import ReconError
from mcps.tools.recon.scope import assert_scan_targets
from mcps.tools.recon.tools_httpx import build_httpx_argv
from mcps.tools.recon.tools_naabu import build_naabu_argv
from mcps.tools.recon.tools_nmap import NMAP_MAX_TARGETS, build_nmap_argv, parse_nmap_xml


def test_naabu_default_top100(recon_cfg, monkeypatch):
    monkeypatch.setattr("mcps.tools.recon.common.has_net_raw", lambda: True)
    argv, stdin, note, need_raw = build_naabu_argv(["192.0.2.1"])
    assert argv[0] == "naabu"
    assert "-tp" in argv and "100" in argv
    assert "-p" not in argv
    assert "-ec" in argv
    assert "-s" in argv and argv[argv.index("-s") + 1] == "s"
    assert "-nmap-cli" not in argv
    assert need_raw is True
    assert "192.0.2.1" in stdin
    assert note == "syn_privileged"


def test_naabu_top1000_requires_aggressive(recon_cfg):
    with pytest.raises(ReconError, match="aggressive"):
        build_naabu_argv(["192.0.2.1"], top_ports=1000, aggressive=False)


def test_naabu_full_ports_require_aggressive(recon_cfg):
    with pytest.raises(ReconError, match="aggressive"):
        build_naabu_argv(["192.0.2.1"], ports="1-65535", aggressive=False)
    argv, _, _, _ = build_naabu_argv(
        ["8.8.8.8"], ports="1-65535", aggressive=True, scan_type="connect"
    )
    assert "-p" in argv and "1-65535" in argv
    assert "-tp" not in argv


def test_naabu_rate_cap(recon_cfg):
    with pytest.raises(ReconError, match="naabu_max_rate"):
        build_naabu_argv(["8.8.8.8"], rate=50_000)


def test_naabu_connect_no_raw(recon_cfg):
    argv, _, note, need_raw = build_naabu_argv(["8.8.8.8"], scan_type="connect")
    assert argv[argv.index("-s") + 1] == "c"
    assert need_raw is False
    assert note == "connect"


def test_nmap_default_small_and_not_dash_a(recon_cfg, monkeypatch):
    monkeypatch.setattr("mcps.tools.recon.common.has_net_raw", lambda: True)
    argv, note, need_raw = build_nmap_argv(["8.8.8.8"])
    assert argv[0] == "nmap"
    assert "-sS" in argv
    assert "-A" not in argv
    assert "-F" in argv
    assert "-oX" in argv
    assert "-sV" in argv
    assert "-sC" not in argv
    assert need_raw is True
    assert argv[-1] == "8.8.8.8"


def test_nmap_rejects_too_many_targets(recon_cfg):
    hosts = [f"8.8.8.{i}" for i in range(1, NMAP_MAX_TARGETS + 2)]
    with pytest.raises(ReconError, match="at most"):
        build_nmap_argv(hosts)


def test_nmap_rejects_vuln_and_star_scripts(recon_cfg):
    with pytest.raises(ReconError, match="not allowed"):
        build_nmap_argv(["8.8.8.8"], scripts=["vuln"])
    with pytest.raises(ReconError, match="not allowed"):
        build_nmap_argv(["8.8.8.8"], scripts=["*"])
    with pytest.raises(ReconError, match="allowlist"):
        build_nmap_argv(["8.8.8.8"], scripts=["ftp-anon"])
    argv, _, _ = build_nmap_argv(
        ["8.8.8.8"], scripts=["http-title", "ssl-cert"], scan_type="connect"
    )
    assert "--script" in argv
    assert "http-title,ssl-cert" in argv


def test_nmap_rejects_full_ports(recon_cfg):
    with pytest.raises(ReconError, match="full-port"):
        build_nmap_argv(["8.8.8.8"], ports="-")


def test_nmap_xml_parse_open_ports():
    xml = """<?xml version="1.0"?>
    <nmaprun>
      <host>
        <address addr="8.8.8.8" addrtype="ipv4"/>
        <hostnames><hostname name="dns.google"/></hostnames>
        <ports>
          <port protocol="tcp" portid="53">
            <state state="open"/>
            <service name="domain" product="Google DNS"/>
          </port>
          <port protocol="tcp" portid="80">
            <state state="closed"/>
          </port>
        </ports>
      </host>
    </nmaprun>
    """
    hosts = parse_nmap_xml(xml)
    assert hosts[0]["address"] == "8.8.8.8"
    assert [p["port"] for p in hosts[0]["ports"]] == [53]


def test_httpx_default_uses_logical_httpx_not_python(recon_cfg):
    argv, stdin = build_httpx_argv(["8.8.8.8:80", "https://example.com"])
    assert argv[0] == "httpx"
    assert "-json" in argv and "-silent" in argv
    assert "-td" in argv and "-fr" in argv
    assert "-omit-body" in argv
    assert "-js" not in argv
    assert "-irr" not in argv
    assert "8.8.8.8:80" in stdin


def test_httpx_can_disable_tech_and_redirects(recon_cfg):
    argv, _ = build_httpx_argv(
        ["8.8.8.8"], follow_redirects=False, tech_detect=False
    )
    assert "-td" not in argv
    assert "-fr" not in argv


def test_scan_type_falls_back_without_privilege(recon_cfg, monkeypatch):
    monkeypatch.setattr("mcps.tools.recon.common.has_net_raw", lambda: False)
    monkeypatch.setattr("mcps.tools.recon.common.shutil_which", lambda name: None)
    actual, need_raw, note = resolve_scan_type("syn")
    assert actual == "connect"
    assert need_raw is False
    assert note == "unprivileged_fallback_connect"


def test_cidr_too_large_for_nmap(monkeypatch, tmp_path):
    import yaml

    from mcps.tools.recon import config as recon_config

    path = tmp_path / "lan.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scope": {
                    "allowed_domains": [],
                    "allowed_cidrs": ["192.168.0.0/16"],
                    "allow_private": False,
                },
                "limits": {"max_hosts": 256},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RECON_CONFIG", str(path))
    recon_config.reset_config()
    with pytest.raises(ReconError, match="addresses"):
        assert_scan_targets(["192.168.0.0/16"], max_items=16)
    assert_scan_targets(["192.168.1.9"], max_items=16)
    recon_config.reset_config()
