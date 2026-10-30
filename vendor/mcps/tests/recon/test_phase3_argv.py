from __future__ import annotations

from pathlib import Path

import pytest

from mcps.tools.recon.common import assert_js_workspace_file
from mcps.tools.recon.errors import ReconError
from mcps.tools.recon.tools_ffuf import build_ffuf_argv
from mcps.tools.recon.http_filter import for_httpx, live_http_urls
from mcps.tools.recon.tools_jshunter import _summarize as jshunter_summarize
from mcps.tools.recon.tools_jshunter import build_jshunter_argv
from mcps.tools.recon.tools_katana import extract_urls
from mcps.tools.recon.tools_katana import build_katana_argv
from mcps.tools.recon.tools_nuclei import build_nuclei_argv
from mcps.tools.recon.tools_restringer import build_restringer_argv
from mcps.tools.recon.tools_wafw00f import build_wafw00f_argv
from mcps.tools.recon.tools_whatweb import build_whatweb_argv
from mcps.tools.recon.util import redact_secret


def test_whatweb_default_and_rejects_heavy(recon_cfg):
    argv = build_whatweb_argv(["http://localhost:3000/"])
    assert argv[0] == "whatweb"
    assert "--aggression=1" in argv
    assert "--log-json=-" in argv
    with pytest.raises(ReconError):
        build_whatweb_argv(["http://localhost:3000/"], aggression=4)
    with pytest.raises(ReconError, match="20"):
        build_whatweb_argv([f"http://localhost:{i}/" for i in range(21)])


def test_wafw00f_stdout_json(recon_cfg):
    argv = build_wafw00f_argv("http://localhost:3000/")
    assert argv[:5] == ["wafw00f", "-f", "json", "-o", "-"]
    assert "-H" not in argv


def test_katana_depth_cap(recon_cfg):
    argv = build_katana_argv("http://localhost:3000/", depth=2)
    assert "-jc" in argv and "-j" in argv
    with pytest.raises(ReconError):
        build_katana_argv("http://localhost:3000/", depth=4)


def test_jshunter_mutex_and_no_headers(recon_cfg, tmp_path):
    out = str(tmp_path / "out.json")
    argv = build_jshunter_argv(url="http://localhost:3000/app.js", json_path=out)
    assert "-u" in argv and "-ep" in argv and "-s" in argv
    assert "--ndjson" in argv
    assert "-H" not in argv and "-p" not in argv
    assert "-d" not in argv
    with pytest.raises(ReconError, match="exactly one"):
        build_jshunter_argv(url="http://x/", file="/tmp/a.js", json_path=out)
    with pytest.raises(ReconError, match="jshunter_max_urls"):
        build_jshunter_argv(
            urls=[f"http://localhost/{i}.js" for i in range(60)],
            json_path=out,
            list_path=str(tmp_path / "l.txt"),
        )


def test_jshunter_file_must_be_workspace(recon_cfg, tmp_path):
    outside = tmp_path / "nope.js"
    outside.write_text("console.log(1)\n", encoding="utf-8")
    with pytest.raises(ReconError, match="js_workspace"):
        assert_js_workspace_file(str(outside))


def test_restringer_output_forced(recon_cfg, tmp_path):
    infile = tmp_path / "a.js"
    outfile = tmp_path / "a-deob.js"
    argv = build_restringer_argv(str(infile), outfile=str(outfile))
    assert argv[0] == "restringer"
    assert "--sandbox" in argv and "isolated-vm" in argv
    assert argv[argv.index("-o") + 1] == str(outfile)
    with pytest.raises(ReconError):
        build_restringer_argv(str(infile), outfile=str(outfile), max_iterations=99)


def test_nuclei_template_path_locked(recon_cfg):
    argv = build_nuclei_argv(
        ["http://localhost:3000/"],
        tags=["tech", "waf"],
        severity=["info", "low"],
    )
    assert argv[argv.index("-t") + 1] == "/usr/share/nuclei-templates"
    assert "-code" not in argv
    assert "http" in argv
    with pytest.raises(ReconError, match="tag"):
        build_nuclei_argv(
            ["http://localhost:3000/"],
            tags=["tech"],
            severity=["info"],
            extra_tags=["../../evil.yaml"],
        )
    with pytest.raises(ReconError, match="severity"):
        build_nuclei_argv(
            ["http://localhost:3000/"],
            tags=["cve"],
            severity=["nuclear"],
        )


def test_ffuf_wordlist_and_fuzz(recon_cfg, tmp_path):
    wl = tmp_path / "wordlists" / "common.txt"
    wl.parent.mkdir(parents=True, exist_ok=True)
    wl.write_text("admin\nlogin\n", encoding="utf-8")
    argv = build_ffuf_argv(
        "http://localhost:3000/FUZZ",
        mode="dir",
        wordlist=str(wl),
        threads=10,
        fc="404,403",
    )
    assert argv[argv.index("-w") + 1] == str(wl.resolve())
    assert "FUZZ" in argv[argv.index("-u") + 1]
    with pytest.raises(ReconError, match="FUZZ"):
        build_ffuf_argv("http://localhost:3000/", mode="dir", wordlist=str(wl))
    with pytest.raises(ReconError, match="allowed prefix"):
        build_ffuf_argv(
            "http://localhost:3000/FUZZ",
            mode="dir",
            wordlist=str(tmp_path / "secret.txt"),
        )
    with pytest.raises(ReconError, match="threads"):
        build_ffuf_argv(
            "http://localhost:3000/FUZZ",
            mode="dir",
            wordlist=str(wl),
            threads=99,
        )
    home_list = Path.home() / "wordlist.txt"
    with pytest.raises(ReconError, match="allowed prefix"):
        build_ffuf_argv(
            "http://localhost:3000/FUZZ",
            mode="dir",
            wordlist=str(home_list),
        )


def test_jshunter_parses_endpoint_text():
    raw = (
        "URL: http://127.0.0.1:3000/book.js\n"
        "ENDPOINT: https://play.rust-lang.org/evaluate.json\n"
        "ENDPOINT: /api/v1/foo\n"
    )
    summary = jshunter_summarize(raw)
    assert "https://play.rust-lang.org/evaluate.json" in summary["endpoints"]
    assert "/api/v1/foo" in summary["endpoints"]


def test_katana_extracts_request_endpoint():
    rows = [
        {
            "request": {
                "method": "GET",
                "endpoint": "https://git.acitelight.top/assets/app.js",
            }
        },
        {"url": "https://git.acitelight.top/"},
    ]
    urls = extract_urls(rows, [])
    assert "https://git.acitelight.top/" in urls
    assert "https://git.acitelight.top/assets/app.js" in urls
    from mcps.tools.recon.tools_katana import _is_js_url

    assert _is_js_url("https://git.acitelight.top/assets/app.js")
    assert not _is_js_url("https://git.acitelight.top/foo.json")
    assert not _is_js_url("https://git.acitelight.top/commit/rb.json")


def test_http_filter_skips_ssh_nfs(recon_cfg):
    kept = for_httpx(
        ["192.168.4.1:22", "192.168.4.1:445", "192.168.4.1:80", "https://git.acitelight.top"],
        fallback_hosts=["192.168.4.1"],
    )
    assert "192.168.4.1:22" not in kept
    assert "192.168.4.1:80" in kept
    assert "https://git.acitelight.top" in kept
    live = live_http_urls(
        [
            {"url": "http://192.168.4.1:22", "status_code": 502},
            {"url": "https://git.acitelight.top/", "status_code": 200},
        ]
    )
    assert live == ["https://git.acitelight.top/"]


def test_redact_secret_keeps_affixes():
    assert redact_secret("abcdefghijklmnop") == "abcd…mnop"
    assert redact_secret("short") == "<redacted>"
