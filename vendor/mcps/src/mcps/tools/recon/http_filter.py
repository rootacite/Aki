"""Drop non-HTTP ports and dead httpx rows before nuclei/whatweb."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

# Ports that are not HTTP(S) in practice. Anything else is left for httpx.
_NON_HTTP_PORTS = frozenset(
    {
        21,
        22,
        23,
        25,
        53,
        67,
        68,
        69,
        110,
        111,
        135,
        137,
        138,
        139,
        143,
        161,
        389,
        445,
        514,
        587,
        636,
        873,
        993,
        995,
        1433,
        1521,
        2049,
        2181,
        2375,
        2376,
        27017,
        3306,
        3389,
        5432,
        5672,
        5900,
        6379,
        9200,
        11211,
    }
)


def for_httpx(items: list[str], *, fallback_hosts: list[str] | None = None) -> list[str]:
    """Keep URLs and host:port that might speak HTTP. Skip ssh/smb/nfs/etc."""
    kept: list[str] = []
    seen: set[str] = set()
    for item in items:
        port = _port_of(item)
        if port is not None and port in _NON_HTTP_PORTS:
            continue
        if item not in seen:
            seen.add(item)
            kept.append(item)
    if kept:
        return kept
    hosts: list[str] = []
    for h in fallback_hosts or []:
        name = h.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
        if name and name not in seen:
            seen.add(name)
            hosts.append(name)
    return hosts


def live_http_urls(endpoints: list[dict[str, Any]]) -> list[str]:
    """2xx/3xx httpx rows only — skip 502 on ssh/nfs-as-http."""
    out: list[str] = []
    for row in endpoints:
        code = row.get("status_code")
        url = row.get("url")
        if not isinstance(url, str) or not url:
            continue
        if not isinstance(code, int) or code < 200 or code >= 400:
            continue
        out.append(url)
    return out


def _port_of(item: str) -> int | None:
    raw = item.strip()
    if "://" in raw:
        parts = urlsplit(raw)
        if parts.port:
            return parts.port
        if parts.scheme == "https":
            return 443
        if parts.scheme == "http":
            return 80
        return None
    if raw.startswith("[") and "]:" in raw:
        try:
            return int(raw.rsplit("]:", 1)[1])
        except ValueError:
            return None
    if raw.count(":") == 1:
        _, _, port_s = raw.rpartition(":")
        try:
            return int(port_s)
        except ValueError:
            return None
    return None
