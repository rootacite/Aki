from __future__ import annotations

from pathlib import Path

from mcps.tools.recon import descriptions

TOOLS_MD = Path("docs/recon-toolkit/TOOLS.md").read_text(encoding="utf-8")


def test_tools_md_contains_phase1_descriptions():
    for text in (
        descriptions.SUBFINDER_ENUM,
        descriptions.DNSX_RESOLVE,
        descriptions.AMASS_PASSIVE,
        descriptions.AMASS_INTEL,
        descriptions.BBOT_SCAN,
        descriptions.NAABU_SCAN,
        descriptions.NMAP_SCAN,
        descriptions.HTTPX_PROBE,
        descriptions.WHATWEB_FINGERPRINT,
        descriptions.WAFW00F_DETECT,
        descriptions.KATANA_CRAWL,
        descriptions.JSHUNTER_ANALYZE,
        descriptions.RESTRINGER_DEOBFUSCATE,
        descriptions.NUCLEI_TECH,
        descriptions.NUCLEI_VULN,
        descriptions.FFUF_FUZZ,
        descriptions.RECON_DOMAIN_PASSIVE,
        descriptions.RECON_WEB_ACTIVE,
        descriptions.RECON_JS_ANALYZE,
        descriptions.RECON_HEALTH,
    ):
        first = text.strip().splitlines()[0]
        assert first in TOOLS_MD
