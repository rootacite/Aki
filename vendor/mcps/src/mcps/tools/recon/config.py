"""Load and cache config/recon.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ReconError

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_PATH = _REPO_ROOT / "config" / "recon.yaml"
_cached: ReconConfig | None = None
_cached_path: Path | None = None


@dataclass(frozen=True)
class ScopeConfig:
    allowed_domains: tuple[str, ...] = ()
    allowed_cidrs: tuple[str, ...] = ()
    allow_private: bool = False
    allow_subdomains: bool = True


@dataclass(frozen=True)
class PrivilegeConfig:
    allow_syn_scan: bool = True
    allow_sudo: bool = True
    sudo_bins: tuple[str, ...] = ("nmap", "naabu", "masscan")
    fallback_connect: bool = True


@dataclass(frozen=True)
class LimitsConfig:
    default_timeout_s: int = 120
    long_timeout_s: int = 900
    max_hosts: int = 256
    max_output_bytes: int = 262144
    max_lines_to_model: int = 200
    naabu_default_top_ports: int = 100
    naabu_max_rate: int = 2000
    ffuf_max_threads: int = 20
    nuclei_max_concurrency: int = 25
    jshunter_max_urls: int = 50
    jshunter_max_threads: int = 5
    restringer_max_input_bytes: int = 1048576
    restringer_max_iterations: int = 10
    js_workspace: str = "/tmp/mcp-recon-js"
    restringer_sandbox: str = "isolated-vm"
    restringer_sb_timeout_ms: int = 1000
    nuclei_templates_dir: str = "/usr/share/nuclei-templates"


@dataclass(frozen=True)
class WordlistsConfig:
    allowed_prefixes: tuple[str, ...] = (
        "/usr/share/seclists/",
        "/usr/share/wordlists/",
        "/usr/share/ffuf/",
    )


@dataclass(frozen=True)
class JobsConfig:
    enabled: bool = True
    dir: str = "/tmp/mcp-recon-jobs"


@dataclass(frozen=True)
class EnvConfig:
    passthrough: tuple[str, ...] = ()
    passthrough_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconConfig:
    path: Path
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    privilege: PrivilegeConfig = field(default_factory=PrivilegeConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    wordlists: WordlistsConfig = field(default_factory=WordlistsConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    env: EnvConfig = field(default_factory=EnvConfig)


def config_path() -> Path:
    override = os.environ.get("MCP_RECON_CONFIG", "").strip()
    if override:
        return Path(os.path.expanduser(override)).resolve()
    return _DEFAULT_PATH


def reset_config() -> None:
    global _cached, _cached_path
    _cached = None
    _cached_path = None


def get_config() -> ReconConfig:
    global _cached, _cached_path
    path = config_path()
    if _cached is not None and _cached_path == path:
        return _cached
    _cached = load_config(path)
    _cached_path = path
    return _cached


def load_config(path: Path | str) -> ReconConfig:
    path = Path(path)
    if not path.is_file():
        raise ReconError(f"recon config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ReconError(f"recon config is not valid YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReconError(f"recon config must be a mapping: {path}")
    return ReconConfig(
        path=path,
        scope=_scope(raw.get("scope") or {}),
        privilege=_privilege(raw.get("privilege") or {}),
        limits=_limits(raw.get("limits") or {}),
        wordlists=_wordlists(raw.get("wordlists") or {}),
        jobs=_jobs(raw.get("jobs") or {}),
        env=_env(raw.get("env") or {}),
    )


def _as_tuple_str(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ReconError(f"{field_name} must be a list of strings")
    return tuple(x.strip() for x in value if x.strip())


def _as_bool(value: Any, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ReconError(f"{field_name} must be a boolean")
    return value


def _as_int(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReconError(f"{field_name} must be an integer")
    return value


def _as_str(value: Any, default: str, field_name: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ReconError(f"{field_name} must be a non-empty string")
    return value.strip()


def _scope(raw: dict[str, Any]) -> ScopeConfig:
    defaults = ScopeConfig()
    return ScopeConfig(
        allowed_domains=tuple(
            d.lower().rstrip(".")
            for d in _as_tuple_str(raw.get("allowed_domains"), "scope.allowed_domains")
        ),
        allowed_cidrs=_as_tuple_str(raw.get("allowed_cidrs"), "scope.allowed_cidrs"),
        allow_private=_as_bool(raw.get("allow_private"), defaults.allow_private, "scope.allow_private"),
        allow_subdomains=_as_bool(
            raw.get("allow_subdomains"), defaults.allow_subdomains, "scope.allow_subdomains"
        ),
    )


def _privilege(raw: dict[str, Any]) -> PrivilegeConfig:
    defaults = PrivilegeConfig()
    sudo_bins = _as_tuple_str(raw.get("sudo_bins"), "privilege.sudo_bins") or defaults.sudo_bins
    return PrivilegeConfig(
        allow_syn_scan=_as_bool(
            raw.get("allow_syn_scan"), defaults.allow_syn_scan, "privilege.allow_syn_scan"
        ),
        allow_sudo=_as_bool(raw.get("allow_sudo"), defaults.allow_sudo, "privilege.allow_sudo"),
        sudo_bins=sudo_bins,
        fallback_connect=_as_bool(
            raw.get("fallback_connect"), defaults.fallback_connect, "privilege.fallback_connect"
        ),
    )


def _limits(raw: dict[str, Any]) -> LimitsConfig:
    d = LimitsConfig()
    fields = {f.name: f.default for f in LimitsConfig.__dataclass_fields__.values()}
    kwargs: dict[str, Any] = {}
    for name, default in fields.items():
        if isinstance(default, bool):
            kwargs[name] = _as_bool(raw.get(name), default, f"limits.{name}")
        elif isinstance(default, int):
            kwargs[name] = _as_int(raw.get(name), default, f"limits.{name}")
        else:
            kwargs[name] = _as_str(raw.get(name), str(default), f"limits.{name}")
    return LimitsConfig(**kwargs)


def _wordlists(raw: dict[str, Any]) -> WordlistsConfig:
    prefixes = _as_tuple_str(raw.get("allowed_prefixes"), "wordlists.allowed_prefixes")
    if not prefixes:
        prefixes = WordlistsConfig().allowed_prefixes
    return WordlistsConfig(allowed_prefixes=prefixes)


def _jobs(raw: dict[str, Any]) -> JobsConfig:
    d = JobsConfig()
    return JobsConfig(
        enabled=_as_bool(raw.get("enabled"), d.enabled, "jobs.enabled"),
        dir=_as_str(raw.get("dir"), d.dir, "jobs.dir"),
    )


def _env(raw: dict[str, Any]) -> EnvConfig:
    d = EnvConfig()
    passthrough = _as_tuple_str(raw.get("passthrough"), "env.passthrough")
    prefixes = _as_tuple_str(raw.get("passthrough_prefixes"), "env.passthrough_prefixes")
    if not passthrough:
        passthrough = (
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "TZ",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
        )
    return EnvConfig(
        passthrough=passthrough or d.passthrough,
        passthrough_prefixes=prefixes,
    )
