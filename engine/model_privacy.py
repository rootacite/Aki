"""Classify a model route as local or cloud for private-content handling."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit


_LOCAL_MODEL_SUFFIXES = (
    ".gguf",
    ".ggml",
    ".safetensors",
    ".onnx",
    ".bin",
)


def _local_host(host: str) -> bool:
    value = host.strip().strip("[]").casefold()
    if not value:
        return False
    if value in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value.endswith(".local") or value.endswith(".localhost")
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def _local_model_file(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.casefold()
    return (
        text.startswith("/")
        or text.startswith("~/")
        or lowered.endswith(_LOCAL_MODEL_SUFFIXES)
    )


def is_local_model_route(route: Any) -> bool:
    """Return True only for routes that clearly run a local model.

    Cloud providers can expose a localhost endpoint (for example a local
    CLI proxy). Endpoint alone therefore cannot decide this. The explicit
    ``local`` flag wins; otherwise a managed solo route is local, and a
    loopback/private endpoint must also look like a local model file or an
    explicitly local upstream.
    """
    if route is None:
        return False
    explicit = getattr(route, "local", None)
    if isinstance(explicit, bool):
        return explicit
    if bool(getattr(route, "solo", False)):
        return True

    url = str(getattr(route, "url", "") or "")
    if not url:
        endpoint = str(getattr(route, "endpoint", "") or "").rstrip("/")
        if endpoint:
            url = endpoint + str(getattr(route, "path", "") or "")
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    if not _local_host(host):
        return False

    upstream_name = str(getattr(route, "upstream_name", "") or "").casefold()
    upstream_model = str(getattr(route, "upstream_model", "") or "")
    return (
        _local_model_file(upstream_model)
        or "local" in upstream_name
    )
