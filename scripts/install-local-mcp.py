#!/usr/bin/env python3
"""Provision the vendored local recon MCP server and configure member profiles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.aki_config import ConfigError, load_config, profile_config_path, validate_profile_name


VENDORED = ROOT / "vendor" / "mcps"
VENV_BIN = VENDORED / ".venv" / "bin" / "mcps"
SERVER_NAME = "local"


class InstallError(RuntimeError):
    """Provisioning cannot continue safely."""


def _run(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _ensure_venv(*, offline: bool) -> None:
    if not (VENDORED / "pyproject.toml").is_file():
        raise InstallError(f"vendored MCP project not found: {VENDORED}")
    uv = shutil.which("uv")
    if not uv:
        raise InstallError("uv is required to provision vendor/mcps/.venv")
    command = [uv, "sync", "--project", str(VENDORED), "--frozen"]
    if offline:
        command.append("--offline")
    result = _run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"uv sync failed: {detail[-3000:]}")
    if not VENV_BIN.is_file() or not os.access(VENV_BIN, os.X_OK):
        raise InstallError(f"vendored MCP executable is missing: {VENV_BIN}")


def _settings_path() -> Path:
    configured = os.environ.get("LETTA_SETTINGS_PATH")
    return Path(configured).expanduser() if configured else Path.home() / ".letta" / "settings.json"


def _server_config() -> dict:
    return {
        "name": SERVER_NAME,
        "transport": "stdio",
        "command": str(VENV_BIN),
        "args": [],
        "cwd": str(ROOT),
    }


def _profiles(requested: list[str]) -> list[str]:
    names: list[str] = []
    for raw in requested or ["natsu"]:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                names.append(validate_profile_name(part))
            except ConfigError as exc:
                raise InstallError(f"invalid profile {part!r}: {exc}") from exc
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _configure(settings_path: Path, profiles: list[str]) -> tuple[list[dict], dict]:
    if not settings_path.is_file():
        raise InstallError(f"Letta settings not found: {settings_path}")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"cannot read {settings_path}: {exc}") from exc
    if not isinstance(settings, dict):
        raise InstallError(f"{settings_path} is not a JSON object")
    agents = settings.get("agents")
    if not isinstance(agents, list):
        raise InstallError(f"{settings_path} has no agents list")
    changes: list[dict] = []
    for profile in profiles:
        config_path = profile_config_path(profile)
        if not config_path.is_file():
            raise InstallError(f"profile config not found: {config_path}")
        config = load_config(config_path, profile=profile)
        matches = [
            item
            for item in agents
            if isinstance(item, dict) and item.get("agentId") == config.agent_id
        ]
        if not matches:
            raise InstallError(
                f"agent {config.agent_id} for profile {profile} is missing from {settings_path}"
            )
        replaced = False
        for entry in matches:
            servers = entry.get("mcpServers")
            if not isinstance(servers, list):
                servers = []
            entry_replaced = False
            for index, server in enumerate(servers):
                if isinstance(server, dict) and server.get("name") == SERVER_NAME:
                    servers[index] = _server_config()
                    entry_replaced = True
                    break
            if not entry_replaced:
                servers.append(_server_config())
            entry["mcpServers"] = servers
            replaced = replaced or entry_replaced
        changes.append(
            {
                "profile": profile,
                "agent_id": config.agent_id,
                "entries": len(matches),
                "replaced": replaced,
                "excluded": SERVER_NAME in config.mcp_excluded,
            }
        )
    return changes, settings


def _write_settings(settings_path: Path, settings: dict) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = settings_path.with_name(f"{settings_path.name}.bak-local-mcp-{stamp}")
    shutil.copy2(settings_path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{settings_path.name}.", dir=settings_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, settings_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", default=[], help="profile to configure; repeatable (default: natsu)")
    parser.add_argument("--offline", action="store_true", help="use only the local uv cache")
    parser.add_argument("--skip-venv", action="store_true", help="do not run uv sync")
    parser.add_argument("--dry-run", action="store_true", help="show planned changes without writing")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    try:
        profiles = _profiles(args.profile)
        if not args.skip_venv and not args.dry_run:
            _ensure_venv(offline=args.offline)
        if not VENV_BIN.is_file():
            raise InstallError(f"vendored MCP executable is missing: {VENV_BIN}")
        settings_path = _settings_path()
        changes, settings = _configure(settings_path, profiles)
        backup = None
        if not args.dry_run:
            backup = _write_settings(settings_path, settings)
    except (InstallError, ConfigError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {
        "vendored": str(VENDORED),
        "command": str(VENV_BIN),
        "settings": str(_settings_path()),
        "backup": str(backup) if backup else None,
        "dry_run": args.dry_run,
        "profiles": changes,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in changes:
            action = "would update" if args.dry_run else "updated"
            if not item["replaced"]:
                action = "would add" if args.dry_run else "added"
            print(
                f"[install-local-mcp] {item['profile']}: "
                f"{action} {SERVER_NAME} MCP server"
                + (" (excluded by profile; tools stay hidden)" if item["excluded"] else "")
            )
        if backup:
            print(f"[install-local-mcp] settings backup: {backup}")
        if not args.dry_run:
            print("[install-local-mcp] restart affected profiles to load MCP servers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
