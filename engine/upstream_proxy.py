"""Priority selection for configured HTTP upstream proxies."""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlsplit

from engine.aki_config import parse_upstream_proxy

LOGGER = logging.getLogger("akid.upstream_proxy")

UPSTREAM_PROXY_PROBE_TIMEOUT = 2.0


def split_upstream_proxy(value: str) -> tuple[str, ...]:
    """Return the configured proxies in priority order."""
    normalized = parse_upstream_proxy(value)
    return tuple(proxy for proxy in normalized.split(";") if proxy)


def _proxy_endpoint(proxy: str) -> tuple[str, int]:
    parsed = urlsplit(proxy)
    return parsed.hostname or "", parsed.port or 80


def select_upstream_proxy(
    value: str,
    *,
    timeout: float = UPSTREAM_PROXY_PROBE_TIMEOUT,
) -> str:
    """Return the first proxy whose port accepts a TCP connection.

    This intentionally probes on every call instead of caching the startup
    result, so failover follows the current proxy reachability.
    """
    proxies = split_upstream_proxy(value)
    if not proxies:
        return ""
    for proxy in proxies:
        host, port = _proxy_endpoint(proxy)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return proxy
        except OSError as exc:
            LOGGER.warning(
                "upstream proxy unavailable proxy=%s error=%s", proxy, exc
            )
    raise OSError(f"no reachable upstream proxy among {len(proxies)} configured")
