#!/usr/bin/env python3
"""Standalone per-profile loopback LLM proxy for Aki."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import logging
import os
import signal
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.aki_config import ConfigError, ensure_proxy_token, load_config, resolve_config_path
from engine.llm_proxy import LlmProxy

LOGGER = logging.getLogger("akid.proxy")
RELOAD_INTERVAL_S = 2.0


def _crypto_available() -> bool:
    return importlib.util.find_spec("cryptography") is not None


def _load(profile: str | None):
    name, path = resolve_config_path(profile)
    if not path.is_file():
        raise ConfigError(f"profile config not found: {path}")
    ensure_proxy_token(path)
    return load_config(path, profile=name)


def _build(config) -> LlmProxy:
    return LlmProxy(
        config.proxy_host,
        config.proxy_port,
        config.proxy_token,
        config.enabled_models,
        upstream_proxy=config.upstream_proxy,
        profile=config.profile,
    )


async def _serve(profile: str | None) -> None:
    config = _load(profile)
    proxy = _build(config)
    await proxy.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    reload_event = asyncio.Event()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGHUP, reload_event.set)
    last_signature = _signature(config.path)
    reload_task = asyncio.create_task(
        _reload_loop(config.path, config.profile, reload_event, proxy),
        name="akid-proxy-reload",
    )
    try:
        await stop_event.wait()
    finally:
        reload_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reload_task
        await proxy.stop()


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


async def _reload_loop(
    path: Path,
    profile: str,
    reload_event: asyncio.Event,
    proxy: LlmProxy,
) -> None:
    signature = _signature(path)
    while True:
        try:
            await asyncio.wait_for(reload_event.wait(), timeout=RELOAD_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        reload_event.clear()
        current = _signature(path)
        if current == signature:
            continue
        signature = current
        try:
            config = load_config(path, profile=profile)
        except (OSError, ConfigError) as exc:
            LOGGER.warning("proxy config reload failed: %s", exc)
            continue
        if (
            config.proxy_host != proxy.host
            or config.proxy_port != proxy.port
            or config.proxy_token != proxy.token
        ):
            LOGGER.warning("proxy listen/token changed; restart akid-proxy@%s", profile)
        proxy.routes = {route.model_id: route for route in config.enabled_models}
        proxy.upstream_proxy = config.upstream_proxy
        LOGGER.info("proxy routes reloaded profile=%s models=%s", profile, sorted(proxy.routes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aki standalone LLM proxy")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--log-level", default=os.environ.get("AKI_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not _crypto_available():
        LOGGER.warning("Python package 'cryptography' is missing; grok-oauth may be unavailable")
    try:
        asyncio.run(_serve(args.profile))
    except KeyboardInterrupt:
        return 130
    except (ConfigError, OSError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
