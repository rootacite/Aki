from __future__ import annotations

import pytest

from mcps.tools.recon.scope import ScopeError, assert_in_scope, parse_target


def test_empty_scope_rejects_everything(monkeypatch, tmp_path):
    import yaml

    from mcps.tools.recon import config as recon_config

    path = tmp_path / "empty.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scope": {
                    "allowed_domains": [],
                    "allowed_cidrs": [],
                    "allow_private": False,
                    "allow_subdomains": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RECON_CONFIG", str(path))
    recon_config.reset_config()
    with pytest.raises(ScopeError, match="scope is empty"):
        assert_in_scope("example.com")
    recon_config.reset_config()


def test_subdomain_allowed(recon_cfg):
    assert_in_scope("example.com")
    assert_in_scope("foo.example.com")
    assert_in_scope("https://a.b.example.com:8443/x")
    assert_in_scope("foo.example.com:8080")


def test_sibling_and_suffix_rejected(recon_cfg):
    with pytest.raises(ScopeError):
        assert_in_scope("notexample.com")
    with pytest.raises(ScopeError):
        assert_in_scope("example.com.evil.test")
    with pytest.raises(ScopeError):
        assert_in_scope("example.org")


def test_subdomains_can_be_disabled(monkeypatch, tmp_path):
    import yaml

    from mcps.tools.recon import config as recon_config
    from mcps.tools.recon.scope import assert_in_scope as check

    path = tmp_path / "nosub.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scope": {
                    "allowed_domains": ["example.com"],
                    "allowed_cidrs": [],
                    "allow_subdomains": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RECON_CONFIG", str(path))
    recon_config.reset_config()
    check("example.com")
    with pytest.raises(ScopeError):
        check("foo.example.com")
    recon_config.reset_config()


def test_cidr_and_ip(recon_cfg):
    assert_in_scope("8.8.8.8")
    assert_in_scope("8.8.8.0/28")
    assert_in_scope("http://8.8.8.8/foo")
    with pytest.raises(ScopeError):
        assert_in_scope("9.9.9.9")
    with pytest.raises(ScopeError):
        assert_in_scope("8.8.0.0/16")  # not a subnet of 8.8.8.0/24


def test_private_rejected_until_enabled(recon_cfg):
    with pytest.raises(ScopeError, match="private"):
        assert_in_scope("10.0.0.1")
    with pytest.raises(ScopeError, match="private"):
        assert_in_scope("127.0.0.1")


def test_explicit_rfc1918_cidr_is_in_scope(monkeypatch, tmp_path):
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
                    "allow_subdomains": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_RECON_CONFIG", str(path))
    recon_config.reset_config()
    assert_in_scope("192.168.1.23")
    assert_in_scope("192.168.0.0/24")
    assert_in_scope("http://192.168.10.4:8080/")
    with pytest.raises(ScopeError):
        assert_in_scope("10.0.0.1")
    with pytest.raises(ScopeError):
        assert_in_scope("example.com")
    recon_config.reset_config()


def test_repo_default_yaml_allows_192_168(monkeypatch):
    from mcps.tools.recon import config as recon_config

    monkeypatch.delenv("MCP_RECON_CONFIG", raising=False)
    recon_config.reset_config()
    assert_in_scope("192.168.0.1")
    assert_in_scope("http://localhost:3000/")
    assert_in_scope("127.0.0.1")
    assert_in_scope("https://git.acitelight.top")
    with pytest.raises(ScopeError):
        assert_in_scope("10.1.1.1")
    recon_config.reset_config()


def test_host_port_and_url_parse():
    p = parse_target("https://Foo.Example.COM:443/a")
    assert p.hostname == "foo.example.com"
    assert p.port == 443
    p2 = parse_target("8.8.8.8:22")
    assert str(p2.ip) == "8.8.8.8"
    assert p2.port == 22


def test_whitespace_and_wildcard_rejected():
    with pytest.raises(ScopeError):
        parse_target("exa mple.com")
    with pytest.raises(ScopeError):
        parse_target("*.example.com")
    with pytest.raises(ScopeError):
        parse_target("file:///etc/passwd")
