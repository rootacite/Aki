"""Start, stop, and inspect per-profile akid systemd units."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
    resolve_config_path,
)
from engine.client import (
    GatewayClient,
    default_socket_path,
)
from engine.protocol import RpcError
from .support import _b64url

from .access import pkg



def _gateway_unit(profile: str) -> str:
    return f"akid@{profile}.service"



def _proxy_unit(profile: str) -> str:
    return f"akid-proxy@{profile}.service"



def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=True,
        check=False,
    )



def _unit_active(unit: str) -> bool:
    try:
        return pkg()._systemctl("is-active", "--quiet", unit).returncode == 0
    except FileNotFoundError:
        return False



def _agent_exists(agent_id: str) -> bool:
    backend = Path(
        os.environ.get(
            "AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend"
        )
    ).expanduser()
    agents = backend / "agents"
    try:
        paths = agents.glob("*.json")
    except OSError:
        return False
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == agent_id:
            return True
    return False



def _conversation_exists(agent_id: str, conversation_id: str) -> bool:
    backend = Path(
        os.environ.get(
            "AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend"
        )
    ).expanduser()
    return (
        backend
        / "conversations"
        / _b64url(f"{conversation_id}:{agent_id}")
        / "conversation.json"
    ).is_file()



def _socket_conflict(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            return f"{path} exists and is not a socket"
    except OSError as exc:
        return f"cannot inspect socket {path}: {exc}"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return None
    except OSError as exc:
        return f"another process may be listening on {path}: {exc}"
    else:
        return f"another akid is already listening on {path}"
    finally:
        probe.close()



def _port_in_use(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        return probe.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        probe.close()



def _profile_config(profile: str, config_path: Path | str | None = None):
    name = profile
    path = Path(config_path).expanduser() if config_path else resolve_config_path(profile)[1]
    if not path.is_file():
        raise ConfigError(f"profile {name!r} has no config at {path}")
    return load_config(path, profile=name)



def _preflight_profile(profile: str, config_path: Path | str | None = None):
    config = _profile_config(profile, config_path)
    if not _agent_exists(config.agent_id):
        raise ConfigError(
            f"agent {config.agent_id} was not found under the Letta backend"
        )
    if not _conversation_exists(config.agent_id, config.conversation):
        raise ConfigError(
            f"conversation {config.conversation!r} for agent {config.agent_id} "
            "does not exist"
        )
    socket_path = (
        Path(config.socket).expanduser()
        if config.socket
        else default_socket_path(profile)
    )
    own_active = pkg()._unit_active(_gateway_unit(profile))
    if not own_active:
        conflict = pkg()._socket_conflict(socket_path)
        if conflict:
            raise ConfigError(conflict)
    own_proxy_active = pkg()._unit_active(_proxy_unit(profile))
    if not own_proxy_active and pkg()._port_in_use(config.proxy_host, config.proxy_port):
        raise ConfigError(
            f"LLM proxy port {config.proxy_host}:{config.proxy_port} is already in use"
        )
    for other, path in discover_profiles().items():
        if other == profile or not pkg()._unit_active(_gateway_unit(other)):
            continue
        try:
            running = load_config(path, profile=other)
        except (OSError, ValueError):
            continue
        if running.agent_id == config.agent_id:
            raise ConfigError(
                f"agent {config.agent_id} is already used by running profile {other}"
            )
        if running.proxy_host == config.proxy_host and running.proxy_port == config.proxy_port:
            raise ConfigError(
                f"LLM proxy port {config.proxy_host}:{config.proxy_port} "
                f"is already used by running profile {other}"
            )
        other_socket = (
            Path(running.socket).expanduser()
            if running.socket
            else default_socket_path(other)
        )
        if other_socket == socket_path:
            raise ConfigError(f"socket {socket_path} is already used by running profile {other}")
    return config, socket_path



def _health_ready(socket_path: Path, timeout: float = 30.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            client = GatewayClient(socket_path)
            try:
                client.connect(timeout=0.5)
                client.handshake()
                health = client.call("aki.health", {}, timeout=1.0)
                if isinstance(health, dict) and health.get("ready"):
                    return health
            except (OSError, ConnectionError, TimeoutError, RpcError, ValueError):
                pass
            finally:
                client.close()
        time.sleep(0.25)
    return None



def _service_status(
    profile: str, *, json_mode: bool = False, config_path: Path | str | None = None
) -> int:
    units: dict[str, Any] = {}
    try:
        for unit in (_proxy_unit(profile), _gateway_unit(profile)):
            result = pkg()._systemctl(
                "show", unit, "-p", "ActiveState,SubState,MainPID", "--no-pager"
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
            units[unit] = values
    except FileNotFoundError:
        pkg().print("systemctl is not available", file=sys.stderr)
        return 1
    try:
        config = _profile_config(profile, config_path)
        socket_path = (
            Path(config.socket).expanduser()
            if config.socket
            else default_socket_path(profile)
        )
        proxy = {"host": config.proxy_host, "port": config.proxy_port}
    except (OSError, ValueError) as exc:
        if json_mode:
            pkg().print(json.dumps({"profile": profile, "error": str(exc)}, ensure_ascii=False))
        else:
            pkg().print(f"error: {exc}", file=sys.stderr)
        return 1
    health = pkg()._health_ready(socket_path, timeout=0.5)
    payload = {
        "profile": profile,
        "socket": str(socket_path),
        "proxy": proxy,
        "units": units,
        "healthy": health is not None,
        "health": health,
    }
    if json_mode:
        pkg().print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for unit, values in units.items():
            state = values.get("ActiveState", "unknown")
            substate = values.get("SubState", "")
            pid = values.get("MainPID", "")
            pkg().print(f"{unit}: {state}{('/' + substate) if substate else ''}{(' pid=' + pid) if pid and pid != '0' else ''}")
        pkg().print(f"socket: {socket_path} ({'ready' if health else 'unavailable'})")
        pkg().print(f"proxy: {proxy['host']}:{proxy['port']}")
    return 0 if health is not None else 1



def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pkg()._port_in_use(host, port):
            return True
        time.sleep(0.1)
    return False



def _profile_up(
    profile: str, *, timeout: float = 30.0, config_path: Path | str | None = None
) -> int:
    try:
        _config, socket_path = pkg()._preflight_profile(profile, config_path)
    except (ConfigError, OSError) as exc:
        pkg().print(f"cannot start profile {profile}: {exc}", file=sys.stderr)
        return 1
    try:
        result = pkg()._systemctl("enable", "--now", _proxy_unit(profile))
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
        if not pkg()._wait_port(_config.proxy_host, _config.proxy_port):
            raise RuntimeError(
                f"LLM proxy did not listen on {_config.proxy_host}:{_config.proxy_port}"
            )
        result = pkg()._systemctl("enable", "--now", _gateway_unit(profile))
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
    except (FileNotFoundError, RuntimeError) as exc:
        pkg().print(f"cannot start profile {profile}: {exc}", file=sys.stderr)
        return 1
    if pkg()._health_ready(socket_path, timeout=timeout) is None:
        pkg().print(
            f"profile {profile} did not become ready; inspect "
            f"journalctl --user -u {_gateway_unit(profile)} -u {_proxy_unit(profile)}",
            file=sys.stderr,
        )
        return 1
    pkg().print(f"profile {profile} is ready on {socket_path}")
    return 0



def _profile_down(profile: str) -> int:
    try:
        result = pkg()._systemctl("disable", "--now", _gateway_unit(profile), _proxy_unit(profile))
    except FileNotFoundError:
        pkg().print("systemctl is not available", file=sys.stderr)
        return 1
    if result.returncode != 0:
        pkg().print((result.stderr or result.stdout).strip(), file=sys.stderr)
        return result.returncode
    pkg().print(f"profile {profile} stopped; configuration and history were left untouched")
    return 0



def _profile_restart(
    profile: str, *, timeout: float = 30.0, config_path: Path | str | None = None
) -> int:
    try:
        _config, socket_path = pkg()._preflight_profile(profile, config_path)
        for unit in (_proxy_unit(profile), _gateway_unit(profile)):
            result = pkg()._systemctl("restart", unit)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
    except (ConfigError, OSError, FileNotFoundError, RuntimeError) as exc:
        pkg().print(f"cannot restart profile {profile}: {exc}", file=sys.stderr)
        return 1
    if pkg()._health_ready(socket_path, timeout=timeout) is None:
        pkg().print(f"profile {profile} did not become ready", file=sys.stderr)
        return 1
    pkg().print(f"profile {profile} restarted")
    return 0



def _profile_logs(profile: str, *, lines: int = 200, follow: bool = False) -> int:
    command = [
        "journalctl",
        "--user",
        "-u",
        _gateway_unit(profile),
        "-u",
        _proxy_unit(profile),
        "-n",
        str(lines),
        "--no-pager",
    ]
    if follow:
        command.append("-f")
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        pkg().print("journalctl is not available", file=sys.stderr)
        return 1



def _profiles(*, json_mode: bool = False) -> int:
    rows: list[dict[str, Any]] = []
    for name, path in discover_profiles().items():
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError) as exc:
            rows.append({"profile": name, "path": str(path), "error": str(exc)})
            continue
        socket_path = (
            Path(config.socket).expanduser()
            if config.socket
            else default_socket_path(name)
        )
        rows.append(
            {
                "profile": name,
                "default": name == "aki",
                "path": str(path),
                "agent_id": config.agent_id,
                "channel_id": config.channel.id,
                "socket": str(socket_path),
                "proxy": f"{config.proxy_host}:{config.proxy_port}",
                "active": pkg()._unit_active(_gateway_unit(name)),
            }
        )
    if json_mode:
        pkg().print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        pkg().print("no profiles found under ~/.aki")
        return 0
    for row in rows:
        if "error" in row:
            pkg().print(f"{row['profile']}	ERROR	{row['error']}")
            continue
        marker = "*" if row["default"] else " "
        state = "running" if row["active"] else "stopped"
        pkg().print(
            f"{marker} {row['profile']}	{state}	{row['channel_id']}	"
            f"{row['proxy']}	{row['socket']}	{row['path']}"
        )
    return 0
