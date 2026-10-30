"""T7 nmap_scan."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Literal

from . import descriptions
from .bins import require
from .common import resolve_scan_type, run_recon
from .errors import BinUnavailable, ReconError, ScopeError
from .scope import assert_scan_targets
from .util import dumps, fail, result_payload

NMAP_MAX_TARGETS = 16
Timing = Literal["T2", "T3", "T4"]
ScanType = Literal["syn", "connect"]
_SCRIPT_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SCRIPT_PREFIXES = ("http-", "ssl-", "tls-", "ssh-", "dns-", "banner")
_SCRIPT_BANNED = {"vuln", "exploit", "brute", "dos", "intrusive", "all", "default"}
_ALL_PORTS = re.compile(r"^(full|-|0-65535|1-65535|\*|all)$", re.I)
_PORT_TOKEN = re.compile(r"^\d+(-\d+)?$")


def build_nmap_argv(
    targets: list[str],
    *,
    ports: str | None = None,
    service_version: bool = True,
    default_scripts: bool = False,
    scripts: list[str] | None = None,
    timing: str = "T3",
    skip_host_discovery: bool = True,
    scan_type: str = "syn",
) -> tuple[list[str], str, bool]:
    if not targets:
        raise ReconError("targets is required")
    if len(targets) > NMAP_MAX_TARGETS:
        raise ReconError(f"nmap accepts at most {NMAP_MAX_TARGETS} targets")
    if timing not in {"T2", "T3", "T4"}:
        raise ReconError("timing must be T2, T3, or T4")
    actual, need_raw, note = resolve_scan_type(scan_type)
    argv = ["nmap", "-n", f"-{timing}", "--open", "-oX", "-"]
    if actual == "syn":
        argv.append("-sS")
    else:
        argv.extend(["-sT", "--unprivileged"])
    if skip_host_discovery:
        argv.append("-Pn")
    if service_version:
        argv.append("-sV")
    if default_scripts:
        argv.append("-sC")
    if scripts:
        argv.extend(["--script", ",".join(_validate_scripts(scripts))])
    if ports:
        if _ALL_PORTS.match(ports.strip()):
            raise ReconError("full-port nmap is not allowed")
        _validate_ports(ports)
        argv.extend(["-p", ports])
    else:
        argv.append("-F")
    if "-A" in argv:
        raise ReconError("nmap -A is forbidden")
    argv.extend(targets)
    return argv, note, need_raw


def _validate_ports(ports: str) -> None:
    raw = ports.strip()
    if not raw or any(c.isspace() for c in raw):
        raise ReconError("invalid ports spec")
    for token in raw.split(","):
        if not _PORT_TOKEN.match(token):
            raise ReconError(f"invalid ports spec: {ports!r}")


def _validate_scripts(scripts: list[str]) -> list[str]:
    cleaned: list[str] = []
    for name in scripts:
        item = name.strip().lower()
        if not item or "/" in item or "*" in item or ".." in item:
            raise ReconError(f"nmap script not allowed: {name!r}")
        if item in _SCRIPT_BANNED or item.startswith("vuln"):
            raise ReconError(f"nmap script category not allowed: {name!r}")
        if not _SCRIPT_OK.match(item):
            raise ReconError(f"invalid nmap script name: {name!r}")
        if not item.startswith(_SCRIPT_PREFIXES) and item != "banner":
            raise ReconError(
                f"script {name!r} is outside the allowlist prefixes {list(_SCRIPT_PREFIXES)}"
            )
        cleaned.append(item)
    return cleaned


def parse_nmap_xml(text: str) -> list[dict[str, Any]]:
    blob = text.strip()
    start = blob.find("<?xml")
    if start == -1:
        start = blob.find("<nmaprun")
    if start == -1:
        return []
    blob = blob[start:]
    end = blob.rfind("</nmaprun>")
    if end != -1:
        blob = blob[: end + len("</nmaprun>")]
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return []
    hosts: list[dict[str, Any]] = []
    for host in root.findall("host"):
        addr_el = host.find("address")
        addr = addr_el.get("addr") if addr_el is not None else None
        hostname = None
        hn = host.find("hostnames/hostname")
        if hn is not None:
            hostname = hn.get("name")
        ports: list[dict[str, Any]] = []
        for port in host.findall("ports/port"):
            state_el = port.find("state")
            state = state_el.get("state") if state_el is not None else None
            if state != "open":
                continue
            svc = port.find("service")
            ports.append(
                {
                    "port": int(port.get("portid", "0")),
                    "protocol": port.get("protocol"),
                    "service": svc.get("name") if svc is not None else None,
                    "product": svc.get("product") if svc is not None else None,
                    "version": svc.get("version") if svc is not None else None,
                }
            )
        hosts.append({"address": addr, "hostname": hostname, "ports": ports})
    return hosts


def register(mcp: Any) -> None:
    @mcp.tool(description=descriptions.NMAP_SCAN.strip())
    async def nmap_scan(
        targets: list[str] | None = None,
        ports: str | None = None,
        service_version: bool = True,
        default_scripts: bool = False,
        scripts: list[str] | None = None,
        timing: Timing = "T3",
        skip_host_discovery: bool = True,
        scan_type: ScanType = "syn",
        timeout_s: int | None = None,
        json_output: bool = True,
    ) -> str:
        """Service identification on a small target set. Out of scope targets are rejected."""
        try:
            require("nmap")
            targets = targets or []
            assert_scan_targets(targets, max_items=NMAP_MAX_TARGETS)
            argv, note, need_raw = build_nmap_argv(
                targets,
                ports=ports,
                service_version=service_version,
                default_scripts=default_scripts,
                scripts=scripts,
                timing=timing,
                skip_host_discovery=skip_host_discovery,
                scan_type=scan_type,
            )
            timeout = timeout_s or 180
            result = run_recon(
                argv,
                timeout_s=timeout,
                need_raw_socket=need_raw,
                tool="nmap_scan",
            )
            parsed = parse_nmap_xml(result.stdout)
            extra: dict[str, Any] = {
                "scan_type_requested": scan_type,
                "scan_type_note": note,
                "hosts": parsed,
            }
            if not json_output:
                extra.pop("hosts", None)
            return dumps(result_payload(result, extra=extra))
        except (ReconError, BinUnavailable, ScopeError) as exc:
            fail(exc)
            raise
