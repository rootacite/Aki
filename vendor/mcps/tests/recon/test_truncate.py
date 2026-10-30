from __future__ import annotations

import shutil

from mcps.tools.recon.common import run_recon, truncate_bytes, truncate_for_model


def test_truncate_bytes():
    data, truncated = truncate_bytes(b"abcdef", 4)
    assert data == b"abcd"
    assert truncated is True
    data, truncated = truncate_bytes(b"ab", 4)
    assert data == b"ab"
    assert truncated is False


def test_truncate_for_model_lines(recon_cfg):
    text = "\n".join(f"line{i}" for i in range(20))
    out, truncated = truncate_for_model(text, max_lines=5)
    assert truncated is True
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines == [f"line{i}" for i in range(5)]
    assert "line19" not in out


def test_run_recon_truncates_stdout(recon_cfg, allow_exec):
    printf = shutil.which("printf")
    assert printf
    allow_exec[printf] = printf
    result = run_recon(
        [printf, "abcdefghij"],
        timeout_s=5,
        max_output_bytes=4,
        tool="test",
    )
    assert result.truncated is True
    assert result.stdout == "abcd"
