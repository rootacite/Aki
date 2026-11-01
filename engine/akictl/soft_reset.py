"""Soft-reset one agent's conversation turns."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
)
from engine.client import _print_json
from engine.protocol import RpcError

from .chat import _connect
from .support import _print_error, _rpc_call

from .access import pkg


def _agent_target(value: str, *, home: Path | None = None):
    wanted = value.strip()
    if not wanted:
        raise ConfigError("agent name is required")
    matches = []
    for name, path in discover_profiles(home).items():
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError):
            continue
        labels = {
            name.casefold(),
            config.profile.casefold(),
            config.agent_id.casefold(),
            config.channel.name.casefold(),
            config.channel.id.casefold(),
            *(alias.casefold() for alias in config.channel.aliases),
        }
        if wanted.casefold() in labels:
            matches.append((name, config))
    if not matches:
        raise ConfigError(f"unknown agent: {wanted}")
    if len(matches) > 1:
        raise ConfigError(
            "agent is ambiguous: " + ", ".join(name for name, _ in matches)
        )
    return matches[0]


def _profile_socket(profile: str, config) -> Path:
    if config.socket:
        return Path(config.socket).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) / "aki" if runtime else Path(f"/tmp/aki-{os.getuid()}")
    filename = "akid.sock" if profile == "aki" else f"{profile}.sock"
    return root / filename


def _soft_reset(args: argparse.Namespace) -> int:
    try:
        profile, config = _agent_target(args.agent_name)
    except (OSError, ValueError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 2
    socket_path = (
        Path(args.socket).expanduser()
        if getattr(args, "socket", None)
        else _profile_socket(profile, config)
    )
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(
            f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr
        )
        return 1
    try:
        result = _rpc_call(
            client,
            "aki.conversation.soft_reset",
            {"solo": bool(args.solo)},
            timeout=30.0,
        )
        _print_json(result)
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()
