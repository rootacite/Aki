"""Reject out-of-scope recon targets."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import ScopeConfig, get_config
from .errors import ReconError, ScopeError

_HOSTPORT_RE = re.compile(r"^(?P<host>.+):(?P<port>\d{1,5})$")


@dataclass(frozen=True)
class ParsedTarget:
    original: str
    kind: str  # hostname, ipv4, ipv6, cidr, url
    hostname: str | None
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None
    port: int | None


def assert_scan_targets(
    targets: list[str],
    *,
    max_items: int,
    max_hosts: int | None = None,
    scope: ScopeConfig | None = None,
) -> list[ParsedTarget]:
    """Scope-check every target and reject oversized CIDRs / lists."""
    if not targets:
        raise ReconError("targets is required")
    if len(targets) > max_items:
        raise ReconError(f"too many targets ({len(targets)} > {max_items})")
    cap = get_config().limits.max_hosts if max_hosts is None else max_hosts
    parsed_list: list[ParsedTarget] = []
    for target in targets:
        parsed = assert_in_scope(target, scope=scope)
        if parsed.network is not None and parsed.network.num_addresses > cap:
            raise ReconError(
                f"CIDR {parsed.network} has {parsed.network.num_addresses} addresses; "
                f"max is {cap}"
            )
        parsed_list.append(parsed)
    return parsed_list


def assert_in_scope(target: str, *, scope: ScopeConfig | None = None) -> ParsedTarget:
    """Raise ScopeError unless target is inside configured scope."""
    parsed = parse_target(target)
    cfg = scope or get_config().scope
    if not cfg.allowed_domains and not cfg.allowed_cidrs:
        raise ScopeError(
            "recon scope is empty; set scope.allowed_domains or "
            "scope.allowed_cidrs in config/recon.yaml"
        )
    if parsed.network is not None:
        _assert_network(parsed.network, cfg)
        return parsed
    if parsed.ip is not None:
        _assert_ip(parsed.ip, cfg)
        return parsed
    if parsed.hostname:
        _assert_hostname(parsed.hostname, cfg)
        return parsed
    raise ScopeError(f"cannot determine host for target: {target!r}")


def parse_target(target: str) -> ParsedTarget:
    raw = (target or "").strip()
    if not raw:
        raise ScopeError("empty target")
    if any(c.isspace() for c in raw):
        raise ScopeError(f"target must not contain whitespace: {raw!r}")
    if raw.startswith("file:") or raw.startswith("unix:"):
        raise ScopeError(f"unsupported URL scheme: {raw!r}")

    if "://" in raw or raw.startswith("//"):
        return _from_url(raw)

    network = _try_network(raw)
    if network is not None:
        return ParsedTarget(raw, "cidr", None, None, network, None)

    ip = _try_ip(raw)
    if ip is not None:
        kind = "ipv6" if isinstance(ip, ipaddress.IPv6Address) else "ipv4"
        return ParsedTarget(raw, kind, None, ip, None, None)

    if raw.startswith("[") and "]" in raw:
        return _from_bracket_host(raw)

    m = _HOSTPORT_RE.match(raw)
    if m and _looks_like_hostport(m.group("host"), m.group("port")):
        host = m.group("host")
        port = int(m.group("port"))
        ip = _try_ip(host)
        if ip is not None:
            kind = "ipv6" if isinstance(ip, ipaddress.IPv6Address) else "ipv4"
            return ParsedTarget(raw, kind, None, ip, None, port)
        return ParsedTarget(raw, "hostname", _normalize_hostname(host), None, None, port)

    return ParsedTarget(raw, "hostname", _normalize_hostname(raw), None, None, None)


def _from_url(raw: str) -> ParsedTarget:
    parts = urlsplit(raw if "://" in raw else "http:" + raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ScopeError(f"unsupported URL scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise ScopeError(f"URL has no hostname: {raw!r}")
    port = parts.port
    ip = _try_ip(host)
    if ip is not None:
        kind = "ipv6" if isinstance(ip, ipaddress.IPv6Address) else "ipv4"
        return ParsedTarget(raw, kind, None, ip, None, port)
    return ParsedTarget(raw, "url", _normalize_hostname(host), None, None, port)


def _from_bracket_host(raw: str) -> ParsedTarget:
    # [2001:db8::1]:443
    close = raw.find("]")
    inner = raw[1:close]
    rest = raw[close + 1 :]
    port = None
    if rest:
        if not rest.startswith(":") or not rest[1:].isdigit():
            raise ScopeError(f"invalid bracket host: {raw!r}")
        port = int(rest[1:])
    ip = _try_ip(inner)
    if ip is None:
        raise ScopeError(f"invalid IPv6 literal: {raw!r}")
    return ParsedTarget(raw, "ipv6", None, ip, None, port)


def _looks_like_hostport(host: str, port: str) -> bool:
    if not host or host.count(":") > 0 and not host.startswith("["):
        # bare IPv6 without brackets is not host:port
        if _try_ip(host) is not None:
            return True
        if host.count(":") >= 2:
            return False
    try:
        n = int(port)
    except ValueError:
        return False
    return 1 <= n <= 65535


def _try_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _try_network(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if "/" not in value:
        return None
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None


def _normalize_hostname(host: str) -> str:
    host = host.strip(".").lower()
    if not host or len(host) > 253:
        raise ScopeError(f"invalid hostname: {host!r}")
    if host.startswith("*.") or "*" in host:
        raise ScopeError(f"wildcard hosts are not accepted as targets: {host!r}")
    labels = host.split(".")
    if any(not lbl or len(lbl) > 63 for lbl in labels):
        raise ScopeError(f"invalid hostname: {host!r}")
    if any(lbl.startswith("-") or lbl.endswith("-") for lbl in labels):
        raise ScopeError(f"invalid hostname: {host!r}")
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise ScopeError(f"invalid hostname: {host!r}") from exc
    return host


def _assert_hostname(hostname: str, cfg: ScopeConfig) -> None:
    if not cfg.allowed_domains:
        raise ScopeError(f"hostname {hostname!r} is out of scope (no allowed_domains)")
    for domain in cfg.allowed_domains:
        if hostname == domain:
            return
        if cfg.allow_subdomains and hostname.endswith("." + domain):
            return
    raise ScopeError(f"hostname {hostname!r} is out of scope")


def _assert_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, cfg: ScopeConfig
) -> None:
    if ip.is_unspecified or ip.is_multicast:
        raise ScopeError(f"address {ip} is not a usable scan target")
    if _ip_in_cidrs(ip, cfg.allowed_cidrs):
        return
    if _is_private(ip):
        if cfg.allow_private:
            return
        raise ScopeError(
            f"address {ip} is private/loopback/link-local and not in allowed_cidrs"
        )
    if not cfg.allowed_cidrs:
        raise ScopeError(f"address {ip} is out of scope (no allowed_cidrs)")
    raise ScopeError(f"address {ip} is out of scope")


def _assert_network(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, cfg: ScopeConfig
) -> None:
    if network.num_addresses > 65536:
        raise ScopeError(f"CIDR {network} is too large")
    if _network_in_cidrs(network, cfg.allowed_cidrs):
        return
    private = (
        network.network_address.is_private
        or network.network_address.is_loopback
        or network.network_address.is_link_local
    )
    if private and cfg.allow_private:
        return
    if private:
        raise ScopeError(f"CIDR {network} is private and not inside allowed_cidrs")
    if not cfg.allowed_cidrs:
        raise ScopeError(f"CIDR {network} is out of scope (no allowed_cidrs)")
    raise ScopeError(f"CIDR {network} is not fully inside allowed_cidrs")


def _ip_in_cidrs(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, cidrs: tuple[str, ...]) -> bool:
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        if ip in net:
            return True
    return False


def _network_in_cidrs(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network, cidrs: tuple[str, ...]
) -> bool:
    for cidr in cidrs:
        allowed = ipaddress.ip_network(cidr, strict=False)
        if network.version != allowed.version:
            continue
        if network.subnet_of(allowed):
            return True
    return False


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv4Address) and ip.is_link_local)
    )
