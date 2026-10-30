"""Resolve recon CLIs; never wrap the wrong same-name binary."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .errors import BinUnavailable, ReconError

# Logical name -> PATH candidates, first match that passes identity wins.
BIN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "subfinder": ("subfinder",),
    "dnsx": ("dnsx",),
    "naabu": ("naabu",),
    "httpx": ("httpx-pd",),  # never python-httpx at /usr/bin/httpx
    "nuclei": ("nuclei",),
    "nmap": ("nmap",),
    "whatweb": ("whatweb",),
    "ffuf": ("ffuf",),
    "jshunter": ("jshunter",),
    "restringer": ("restringer",),
    "amass": ("amass",),
    "bbot": ("bbot",),
    "katana": ("katana", "katana-pd"),
    "wafw00f": ("wafw00f",),
    "masscan": ("masscan",),
    "theHarvester": ("theHarvester", "theharvester"),
    "gau": ("gau",),
    "waybackurls": ("waybackurls",),
}

_REQUIRED = frozenset(
    {
        "subfinder",
        "dnsx",
        "naabu",
        "httpx",
        "nuclei",
        "nmap",
        "whatweb",
        "ffuf",
        "jshunter",
        "restringer",
    }
)


@dataclass(frozen=True)
class BinInfo:
    name: str
    path: str | None
    available: bool
    identity_ok: bool
    note: str = ""


def known_names() -> tuple[str, ...]:
    return tuple(BIN_CANDIDATES)


def is_required(name: str) -> bool:
    return name in _REQUIRED


def which(name: str) -> str | None:
    """Return an executable path or None. Does not raise."""
    info = inspect(name)
    if info.available and info.identity_ok and info.path:
        return info.path
    return None


def require(name: str) -> str:
    """Return path or raise BinUnavailable (do not crash the server at import)."""
    info = inspect(name)
    if info.available and info.identity_ok and info.path:
        return info.path
    raise BinUnavailable(info.note or f"{name} is not available")


def inspect(name: str) -> BinInfo:
    if name not in BIN_CANDIDATES:
        return BinInfo(name, None, False, False, f"unknown recon binary name: {name}")
    for candidate in BIN_CANDIDATES[name]:
        path = shutil.which(candidate)
        if not path:
            continue
        resolved = str(Path(path).resolve())
        ok, note = _identity(name, resolved)
        if ok:
            return BinInfo(name, resolved, True, True, note)
        return BinInfo(name, resolved, True, False, note)
    return BinInfo(
        name,
        None,
        False,
        False,
        f"{name} not found on PATH (tried {', '.join(BIN_CANDIDATES[name])})",
    )


def resolve_allowed(argv0: str) -> str:
    """argv[0] must be a known recon name or the realpath of one."""
    if not argv0 or not isinstance(argv0, str):
        raise ReconError("argv[0] must be a recon binary name or path")
    if argv0 in BIN_CANDIDATES:
        return require(argv0)
    path = Path(argv0)
    if not path.is_absolute():
        raise ReconError(f"refusing relative executable path: {argv0!r}")
    if not path.exists():
        raise ReconError(f"executable not found: {argv0}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ReconError(f"not a file: {argv0}")
    for name in BIN_CANDIDATES:
        info = inspect(name)
        if not info.path:
            continue
        if Path(info.path).resolve() == resolved:
            if not info.identity_ok:
                raise BinUnavailable(info.note)
            return str(resolved)
    raise ReconError(f"refusing to execute unlisted binary: {argv0}")


def logical_name_for_path(path: str) -> str | None:
    resolved = Path(path).resolve()
    for name in BIN_CANDIDATES:
        info = inspect(name)
        if info.path and Path(info.path).resolve() == resolved:
            return name
    return Path(path).name


def _identity(name: str, path: str) -> tuple[bool, str]:
    if name == "httpx":
        return _httpx_is_projectdiscovery(path)
    if name == "jshunter":
        return _jshunter_is_cc1a2b(path)
    if name == "gau":
        return _gau_is_binary(path)
    return True, Path(path).name


@lru_cache(maxsize=32)
def _help_text(path: str) -> str:
    try:
        proc = subprocess.run(
            [path, "-h"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            shell=False,
        )
        return (proc.stdout or "") + "\n" + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _httpx_is_projectdiscovery(path: str) -> tuple[bool, str]:
    if Path(path).name == "httpx-pd":
        return True, "httpx-pd (projectdiscovery)"
    help_text = _help_text(path)
    if "projectdiscovery" in help_text.lower():
        return True, "projectdiscovery/httpx"
    return False, (
        f"{path} is not projectdiscovery/httpx "
        "(likely python-httpx); use httpx-pd"
    )


def _jshunter_is_cc1a2b(path: str) -> tuple[bool, str]:
    help_text = _help_text(path)
    if "Created by cc1a2b" in help_text:
        return True, "github.com/cc1a2b/jshunter"
    if "-ep" in help_text and "--end-point" in help_text and "-u" in help_text:
        return True, "likely github.com/cc1a2b/jshunter"
    return False, (
        f"{path} does not look like github.com/cc1a2b/jshunter; "
        "will not wrap this binary"
    )


def _gau_is_binary(path: str) -> tuple[bool, str]:
    resolved = Path(path)
    if not resolved.is_file():
        return False, f"{path} is not a regular file (alias/function?)"
    return True, "gau CLI"
