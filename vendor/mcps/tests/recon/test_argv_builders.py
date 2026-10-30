from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from mcps.tools.recon.common import (
    assert_wordlist_path,
    elevate_if_needed,
    js_workspace_dir,
    prepare_argv,
    redact_argv,
    run_recon,
)
from mcps.tools.recon.errors import ReconError


def test_source_has_no_shell_true():
    src = Path("src/mcps/tools/recon/common.py").read_text(encoding="utf-8")
    assert "os.system" not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value is False
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen"}:
                raise AssertionError(f"forbidden call: {node.func.attr}")


def test_argv_must_be_list(recon_cfg):
    with pytest.raises(ReconError, match="list"):
        prepare_argv("nmap -sS example.com")  # type: ignore[arg-type]


def test_unlisted_binary_rejected(recon_cfg):
    bash = shutil.which("bash") or "/bin/bash"
    with pytest.raises(ReconError, match="unlisted"):
        prepare_argv([bash, "-c", "echo pwned"])


def test_relative_path_rejected(recon_cfg):
    with pytest.raises(ReconError, match="relative"):
        prepare_argv(["./nmap", "-sT", "example.com"])


def test_metacharacters_are_literal(recon_cfg, allow_exec):
    printf = shutil.which("printf")
    assert printf
    allow_exec[printf] = printf
    result = run_recon(
        [printf, "%s", "a; id; echo $HOME"],
        timeout_s=5,
        max_output_bytes=256,
    )
    assert result.ok
    assert result.stdout == "a; id; echo $HOME"


def test_timeout_kills_process(recon_cfg, allow_exec):
    sleep = shutil.which("sleep")
    assert sleep
    allow_exec[sleep] = sleep
    result = run_recon([sleep, "30"], timeout_s=1, max_output_bytes=64)
    assert result.timed_out is True
    assert result.ok is False
    assert result.duration_ms < 8000


def test_redact_argv_headers_and_keys():
    redacted = redact_argv(
        [
            "jshunter",
            "-H",
            "Authorization: Bearer supersecret",
            "--api-key",
            "abc123",
            "TOKEN=visible-should-hide",
        ]
    )
    assert "supersecret" not in redacted
    assert "abc123" not in redacted
    assert any("<redacted>" in x for x in redacted)
    assert "TOKEN=<redacted>" in redacted


def test_wordlist_path_must_stay_under_prefix(recon_cfg, tmp_path):
    allowed = tmp_path / "wordlists"
    good = allowed / "dirs.txt"
    good.write_text("a\n", encoding="utf-8")
    assert assert_wordlist_path(str(good)) == good.resolve()
    evil = tmp_path / "secret.txt"
    evil.write_text("nope\n", encoding="utf-8")
    with pytest.raises(ReconError, match="allowed prefix"):
        assert_wordlist_path(str(evil))
    # traversal
    with pytest.raises(ReconError, match="allowed prefix"):
        assert_wordlist_path(str(allowed / ".." / "secret.txt"))


def test_js_workspace_rejects_escape(recon_cfg, tmp_path):
    dest = js_workspace_dir("abc-def")
    assert dest.is_relative_to(Path(recon_cfg.limits.js_workspace).resolve())
    with pytest.raises(ReconError):
        js_workspace_dir("../etc")
    with pytest.raises(ReconError):
        js_workspace_dir("a/b")


def test_sudo_only_for_allowlisted_bins(recon_cfg, monkeypatch):
    monkeypatch.setattr("mcps.tools.recon.common.has_net_raw", lambda: False)
    monkeypatch.setattr(
        "mcps.tools.recon.common.shutil_which",
        lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )
    monkeypatch.setattr(
        "mcps.tools.recon.bins.logical_name_for_path",
        lambda path: Path(path).name,
    )
    argv, privileged, note = elevate_if_needed(
        ["/usr/bin/nmap", "-sS", "192.0.2.1"],
        need_raw_socket=True,
    )
    assert privileged is False
    assert argv[:3] == ["/usr/bin/sudo", "-n", "--"]
    assert note == "sudo_n"

    argv2, _, note2 = elevate_if_needed(
        ["/usr/bin/subfinder", "-d", "example.com"],
        need_raw_socket=True,
    )
    assert argv2[0] == "/usr/bin/subfinder"
    assert "sudo" not in argv2[0]
    assert note2.startswith("bin_not_in_sudo_allowlist")


def test_env_whitelist_drops_unrelated_secrets(recon_cfg, allow_exec, monkeypatch):
    envbin = shutil.which("env")
    assert envbin
    allow_exec[envbin] = envbin
    monkeypatch.setenv("UNRELATED_SECRET", "nope-should-not-leak")
    monkeypatch.setenv("NUCLEI_TOKEN", "sekrit")
    result = run_recon([envbin], timeout_s=5, max_output_bytes=20000)
    assert result.ok
    assert "UNRELATED_SECRET" not in result.stdout
    assert "NUCLEI_TOKEN=sekrit" in result.stdout


def test_no_sudo_when_not_need_raw(recon_cfg, monkeypatch):
    monkeypatch.setattr("mcps.tools.recon.common.has_net_raw", lambda: False)
    argv, _, note = elevate_if_needed(["/usr/bin/nmap"], need_raw_socket=False)
    assert argv == ["/usr/bin/nmap"]
    assert note == "not_required"
