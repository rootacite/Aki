"""akictl: chat and local-socket management for Aki."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
    profile_config_path,
    resolve_config_path,
    validate_profile_name,
)
from engine.client import (
    GatewayClient,
    _format_message,
    _memory_command,
    _print_json,
    default_socket_path,
    run_turn,
)
from engine.letta_compact import COMPACT_MODES
from engine.member_identity import legacy_member_id
from engine.protocol import RpcError

COMMANDS = {
    "chat",
    "status",
    "health",
    "history",
    "allow",
    "disallow",
    "allowlist",
    "memory",
    "mqtt",
    "mcp",
    "avatar",
    "identity",
    "reset",
    "username",
    "set-username",
    "recompile",
    "compact",
    "reasoning",
    "model",
    "solo",
    "help",
}

SERVICE_COMMANDS = {"up", "down", "restart", "status", "logs", "profiles"}
ALL_COMMANDS = COMMANDS | SERVICE_COMMANDS


HELP_TEXT = (
    "usage: akictl [--profile NAME] [profile] <command> [options]\n"
    "       akictl [chat|status|health|history|allow|disallow|allowlist|memory|mqtt|mcp|model|reasoning|avatar|identity|reset|username|set-username|recompile|compact|solo|help] [options]\n"
    "\n"
    "  akictl <profile> up|down|restart|status|logs   manage one profile service\n"
    "  akictl profiles            list ~/.aki profiles and service state\n"
    "  akictl --profile fuyu ...  run any command against profile fuyu\n"
    "  akictl fuyu chat           chat with profile fuyu\n"
    "  akictl                     streaming chat REPL (default profile aki)\n"
    "  akictl -p '...'            one-shot chat\n"
    "  akictl help [command]      show this help or help for one command\n"
    "  akictl allow <dir>         register a writable directory\n"
    "  akictl disallow <dir>      revoke a writable directory\n"
    "  akictl disallow --all      revoke all registered directories\n"
    "  akictl allowlist           show the effective writable policy\n"
    "  akictl status              show the default profile's gateway status\n"
    "  akictl health              gateway health\n"
    "  akictl history [n]         conversation history\n"
    "  akictl memory ...          read-only memory commands\n"
    "  akictl mqtt [status]       show the active MQTT broker\n"
    "  akictl mqtt set <server>   persist and reconnect to host:port\n"
    "  akictl mcp                 show native MCP tool status\n"
    "  akictl mcp local on|off    enable/disable local recon tools as native tools\n"
    "  akictl model               list the active model and configured models\n"
    "  akictl model set ID        switch model and restart the backend\n"
    "  akictl reasoning           show reasoning_effort (default low)\n"
    "  akictl reasoning set LEVEL set reasoning_effort; LEVEL defaults to low\n"
    "  akictl avatar              show Aki's avatar asset status\n"
    "  akictl identity show       show this profile's immutable member id\n"
    "  akictl identity rename N   rename this identity; old labels stay aliases\n"
    "  akictl reset               reset the default conversation (backs up first)\n"
    "  akictl username            show how Aki addresses the user\n"
    "  akictl set-username NAME   set how Aki addresses the user (e.g. Master)\n"
    "  akictl recompile           rebuild the current conversation prompt from MemFS\n"
    "  akictl compact [mode]      compact context and trigger memory reflection\n"
    "  akictl solo <member>       enter private solo mode with one member\n"
    "  akictl solo status         show the active solo session\n"
    "  akictl solo off            end solo mode\n"
    "  akictl up | down | restart manage the default profile service\n"
    "  akictl logs                show profile service logs\n"
    "\nUse 'akictl <command> --help' for command-specific options."
)


def _print_help() -> None:
    print(HELP_TEXT)


def _socket_from(args: argparse.Namespace, profile: str = "aki") -> Path:
    if getattr(args, "socket", None):
        return Path(args.socket).expanduser()
    if os.environ.get("AKI_SOCKET"):
        return Path(os.environ["AKI_SOCKET"]).expanduser()
    override = getattr(args, "config_path", None)
    if override:
        name, config_path = profile, Path(override)
    else:
        name, config_path = resolve_config_path(profile)
    if config_path.is_file():
        try:
            configured = load_config(config_path, profile=name).socket
        except (OSError, ValueError):
            configured = ""
        if configured:
            return Path(configured).expanduser()
    return default_socket_path(profile)


def _rpc_call(
    client: GatewayClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    return client.call(method, params or {}, timeout=timeout)


def _print_error(exc: BaseException) -> None:
    if isinstance(exc, RpcError):
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.data:
            print(json.dumps(exc.data, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print(f"error: {exc}", file=sys.stderr)


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
        return _systemctl("is-active", "--quiet", unit).returncode == 0
    except FileNotFoundError:
        return False


def _b64url(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


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
    own_active = _unit_active(_gateway_unit(profile))
    if not own_active:
        conflict = _socket_conflict(socket_path)
        if conflict:
            raise ConfigError(conflict)
    own_proxy_active = _unit_active(_proxy_unit(profile))
    if not own_proxy_active and _port_in_use(config.proxy_host, config.proxy_port):
        raise ConfigError(
            f"LLM proxy port {config.proxy_host}:{config.proxy_port} is already in use"
        )
    for other, path in discover_profiles().items():
        if other == profile or not _unit_active(_gateway_unit(other)):
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
            result = _systemctl(
                "show", unit, "-p", "ActiveState,SubState,MainPID", "--no-pager"
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
            units[unit] = values
    except FileNotFoundError:
        print("systemctl is not available", file=sys.stderr)
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
            print(json.dumps({"profile": profile, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    health = _health_ready(socket_path, timeout=0.5)
    payload = {
        "profile": profile,
        "socket": str(socket_path),
        "proxy": proxy,
        "units": units,
        "healthy": health is not None,
        "health": health,
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for unit, values in units.items():
            state = values.get("ActiveState", "unknown")
            substate = values.get("SubState", "")
            pid = values.get("MainPID", "")
            print(f"{unit}: {state}{('/' + substate) if substate else ''}{(' pid=' + pid) if pid and pid != '0' else ''}")
        print(f"socket: {socket_path} ({'ready' if health else 'unavailable'})")
        print(f"proxy: {proxy['host']}:{proxy['port']}")
    return 0 if health is not None else 1


def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use(host, port):
            return True
        time.sleep(0.1)
    return False


def _profile_up(
    profile: str, *, timeout: float = 30.0, config_path: Path | str | None = None
) -> int:
    try:
        _config, socket_path = _preflight_profile(profile, config_path)
    except (ConfigError, OSError) as exc:
        print(f"cannot start profile {profile}: {exc}", file=sys.stderr)
        return 1
    try:
        result = _systemctl("enable", "--now", _proxy_unit(profile))
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
        if not _wait_port(_config.proxy_host, _config.proxy_port):
            raise RuntimeError(
                f"LLM proxy did not listen on {_config.proxy_host}:{_config.proxy_port}"
            )
        result = _systemctl("enable", "--now", _gateway_unit(profile))
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"cannot start profile {profile}: {exc}", file=sys.stderr)
        return 1
    if _health_ready(socket_path, timeout=timeout) is None:
        print(
            f"profile {profile} did not become ready; inspect "
            f"journalctl --user -u {_gateway_unit(profile)} -u {_proxy_unit(profile)}",
            file=sys.stderr,
        )
        return 1
    print(f"profile {profile} is ready on {socket_path}")
    return 0


def _profile_down(profile: str) -> int:
    try:
        result = _systemctl("disable", "--now", _gateway_unit(profile), _proxy_unit(profile))
    except FileNotFoundError:
        print("systemctl is not available", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print((result.stderr or result.stdout).strip(), file=sys.stderr)
        return result.returncode
    print(f"profile {profile} stopped; configuration and history were left untouched")
    return 0


def _profile_restart(
    profile: str, *, timeout: float = 30.0, config_path: Path | str | None = None
) -> int:
    try:
        _config, socket_path = _preflight_profile(profile, config_path)
        for unit in (_proxy_unit(profile), _gateway_unit(profile)):
            result = _systemctl("restart", unit)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout).strip() or "systemctl failed")
    except (ConfigError, OSError, FileNotFoundError, RuntimeError) as exc:
        print(f"cannot restart profile {profile}: {exc}", file=sys.stderr)
        return 1
    if _health_ready(socket_path, timeout=timeout) is None:
        print(f"profile {profile} did not become ready", file=sys.stderr)
        return 1
    print(f"profile {profile} restarted")
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
        print("journalctl is not available", file=sys.stderr)
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
                "active": _unit_active(_gateway_unit(name)),
            }
        )
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no profiles found under ~/.aki")
        return 0
    for row in rows:
        if "error" in row:
            print(f"{row['profile']}	ERROR	{row['error']}")
            continue
        marker = "*" if row["default"] else " "
        state = "running" if row["active"] else "stopped"
        print(
            f"{marker} {row['profile']}	{state}	{row['channel_id']}	"
            f"{row['proxy']}	{row['socket']}	{row['path']}"
        )
    return 0


def repl(client: GatewayClient, *, json_mode: bool, show_reasoning: bool) -> int:
    print(
        "akictl REPL. Commands: /status /health /history [n] /allow <dir> "
        "/disallow <dir> /allowlist /memory ... /mqtt ... /mcp ... /model [set <id>] "
        "/reasoning [set <level>] "
        "/avatar /username "
        "/set-username <name> /recompile /compact [mode] /reset /quit",
        file=sys.stderr,
    )
    while True:
        try:
            line = input("you> ")
        except EOFError:
            print("", file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print("", file=sys.stderr)
            continue
        prompt = line.strip()
        if not prompt:
            continue
        if prompt in ("/quit", "/exit"):
            return 0
        try:
            if prompt == "/status":
                _print_json(_rpc_call(client, "aki.status"))
            elif prompt == "/health":
                _print_json(_rpc_call(client, "aki.health"))
            elif prompt.startswith("/history"):
                parts = prompt.split()
                limit = int(parts[1]) if len(parts) > 1 else 50
                result = _rpc_call(client, "aki.chat.history", {"limit": limit})
                for message in result.get("messages", []):
                    print(_format_message(message))
            elif prompt.startswith("/allowlist"):
                _print_json(_rpc_call(client, "aki.fs.writable.list"))
            elif prompt.startswith("/allow "):
                _print_json(
                    _rpc_call(client, "aki.fs.writable.add", {"path": prompt[7:].strip()})
                )
            elif prompt.startswith("/disallow "):
                _print_json(
                    _rpc_call(
                        client,
                        "aki.fs.writable.remove",
                        {"path": prompt[10:].strip()},
                    )
                )
            elif prompt.startswith("/memory"):
                _memory_command(client, prompt.split()[1:])
            elif prompt.startswith("/mqtt"):
                rest = prompt.split()[1:]
                _print_json(_mqtt_call(client, rest))
            elif prompt.startswith("/mcp"):
                rest = prompt.split()[1:]
                _print_json(_mcp_call(client, rest))
            elif prompt.startswith("/reasoning"):
                rest = prompt.split()[1:]
                _print_json(_reasoning_call(client, rest))
            elif prompt.startswith("/model"):
                rest = prompt.split()[1:]
                _print_json(_model_call(client, rest))
            elif prompt == "/avatar":
                _print_json(_rpc_call(client, "aki.avatar.status"))
            elif prompt == "/reset":
                _print_json(_rpc_call(client, "aki.conversation.reset"))
            elif prompt == "/username":
                _print_json(_rpc_call(client, "aki.username.get"))
            elif prompt.startswith("/set-username "):
                _print_json(
                    _rpc_call(
                        client,
                        "aki.username.set",
                        {"username": prompt[14:].strip()},
                    )
                )
            elif prompt == "/recompile":
                _print_json(_rpc_call(client, "aki.conversation.recompile"))
            elif prompt == "/compact" or prompt.startswith("/compact "):
                rest = prompt.split()[1:]
                params: dict[str, Any] = {}
                if rest:
                    params["mode"] = rest[0]
                _print_json(
                    _rpc_call(
                        client,
                        "aki.conversation.compact",
                        params,
                        timeout=930.0,
                    )
                )
            else:
                run_turn(
                    client,
                    line,
                    json_mode=json_mode,
                    show_reasoning=show_reasoning,
                )
        except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
            _print_error(exc)


def _connect(socket_path: Path, *, json_mode: bool, client_name: str = "akictl") -> GatewayClient:
    raw_lock = threading.Lock()

    def raw_sink(line: str) -> None:
        with raw_lock:
            print(line, flush=True)

    client = GatewayClient(socket_path, client_name=client_name, raw_sink=raw_sink if json_mode else None)
    client.connect()
    client.handshake()
    return client


def _chat(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=args.json)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        prompt = args.prompt
        if prompt is None and args.prompt_text:
            prompt = " ".join(args.prompt_text)
        if prompt is not None:
            try:
                ok = run_turn(
                    client,
                    prompt,
                    json_mode=args.json,
                    show_reasoning=args.include_reasoning,
                    client_message_id=args.client_message_id,
                )
            except (RpcError, ConnectionError, TimeoutError) as exc:
                _print_error(exc)
                return 1
            return 0 if ok else 1
        return repl(client, json_mode=args.json, show_reasoning=args.include_reasoning)
    finally:
        client.close()


def _management(
    args: argparse.Namespace,
    method: str,
    params: dict[str, Any] | None = None,
    profile: str = "aki",
    *,
    timeout: float = 30.0,
) -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        result = _rpc_call(client, method, params, timeout=timeout)
        _print_json(result)
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _history(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        result = _rpc_call(client, "aki.chat.history", {"limit": args.limit})
        for message in result.get("messages", []):
            print(_format_message(message))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _identity(args: argparse.Namespace, profile: str = "aki") -> int:
    command = getattr(args, "identity_command", None) or "show"
    if command == "show":
        return _management(args, "aki.identity.get", profile=profile)
    if command == "rename":
        name = getattr(args, "name", "").strip()
        if not name:
            print("akictl identity rename: name required", file=sys.stderr)
            return 2
        return _management(
            args, "aki.identity.rename", {"name": name}, profile=profile
        )
    print(f"unknown identity command: {command}", file=sys.stderr)
    return 2


def _memory(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        _memory_command(client, args.memory_args)
        return 0
    finally:
        client.close()


def _compact(args: argparse.Namespace, profile: str = "aki") -> int:
    mode = args.mode_option or args.mode
    params: dict[str, Any] = {"timeout": args.timeout}
    if mode:
        params["mode"] = mode
    print(
        "compacting context and waiting for memory reflection...",
        file=sys.stderr,
        flush=True,
    )
    return _management(
        args,
        "aki.conversation.compact",
        params,
        profile=profile,
        timeout=float(args.timeout) + 30.0,
    )


def _mqtt_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.mqtt.status")
    if rest[0] != "set":
        raise ValueError("usage: /mqtt [status] | /mqtt set <host> [port]")
    host, port = _parse_mqtt_server(rest[1:])
    return _rpc_call(client, "aki.mqtt.set", {"host": host, "port": port})


def _mcp_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.mcp.status")
    if rest[0] != "local":
        raise ValueError("usage: /mcp [status] | /mcp local [on|off|status]")
    state = rest[1].lower() if len(rest) > 1 else "status"
    if state == "status":
        return _rpc_call(client, "aki.mcp.status")
    if state in ("on", "enable", "enabled"):
        return _rpc_call(
            client, "aki.mcp.set", {"server": "local", "enabled": True}
        )
    if state in ("off", "disable", "disabled"):
        return _rpc_call(
            client, "aki.mcp.set", {"server": "local", "enabled": False}
        )
    raise ValueError("usage: /mcp local [on|off|status]")


def _parse_mqtt_server(values: list[str]) -> tuple[str, int]:
    if not values:
        raise ValueError("usage: mqtt set <host> [port] or <host:port>")
    if len(values) >= 2:
        host = values[0]
        try:
            port = int(values[1])
        except ValueError as exc:
            raise ValueError("port must be an integer") from exc
        return host, port
    value = values[0].strip()
    if value.startswith("[") and "]" in value:
        host, _, remainder = value[1:].partition("]")
        if remainder.startswith(":"):
            return host, int(remainder[1:])
        raise ValueError("IPv6 server must include a port, e.g. [::1]:1883")
    if ":" in value:
        host, port_text = value.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError as exc:
            raise ValueError("port must be an integer") from exc
    return value, 1883


def _mqtt(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        rest = ([args.mqtt_command] if args.mqtt_command else []) + args.mqtt_values
        _print_json(_mqtt_call(client, rest))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _model_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.model.get")
    if rest[0] != "set" or len(rest) < 2:
        raise ValueError("usage: /model [status] | /model set <id>")
    return _rpc_call(client, "aki.model.set", {"model": rest[1]})


def _model(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        rest = ([args.model_command] if args.model_command else []) + args.model_values
        _print_json(_model_call(client, rest))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _reasoning_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.reasoning.get")
    if rest[0] != "set":
        raise ValueError("usage: /reasoning [status] | /reasoning set [low|medium|high]")
    effort = rest[1] if len(rest) > 1 else "low"
    return _rpc_call(client, "aki.reasoning.set", {"effort": effort})


def _reasoning(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        rest = ([args.reasoning_command] if args.reasoning_command else []) + args.reasoning_values
        _print_json(_reasoning_call(client, rest))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _mcp(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        _print_json(_mcp_call(client, list(args.mcp_args)))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _solo_target(value: str):
    wanted = value.strip()
    if not wanted:
        raise ConfigError("solo member is required")
    matches = []
    for name, path in discover_profiles().items():
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError):
            continue
        labels = {
            name.casefold(),
            config.channel.name.casefold(),
            config.channel.id.casefold(),
            *(alias.casefold() for alias in config.channel.aliases),
        }
        if wanted.casefold() in labels:
            matches.append((name, config))
    if not matches:
        raise ConfigError(f"unknown solo member: {wanted}")
    if len(matches) > 1:
        raise ConfigError(
            "solo member is ambiguous: " + ", ".join(name for name, _ in matches)
        )
    return matches[0]


def _default_solo_config():
    profiles = discover_profiles()
    for name in ("aki", *profiles):
        path = profiles.get(name)
        if path is None:
            continue
        try:
            config = load_config(path, profile=name)
        except (OSError, ValueError):
            continue
        if config.mqtt_enabled:
            return config
    raise ConfigError("no MQTT-enabled profile is available")


def _solo_control_state_path() -> Path:
    return Path.home() / ".aki" / "solo" / "control.json"


def _write_solo_control_state(result: dict[str, Any]) -> None:
    from engine.aki_config import atomic_write

    path = _solo_control_state_path()
    atomic_write(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def _read_solo_control_state() -> dict[str, Any] | None:
    try:
        data = json.loads(_solo_control_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clear_solo_control_state() -> None:
    try:
        _solo_control_state_path().unlink()
    except FileNotFoundError:
        pass


async def _solo_broker_call(
    config,
    *,
    action: str,
    target=None,
    session_id: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    from engine.mqtt_client import MqttClient
    from engine.solo_mode import SOLO_END, SOLO_READY, SOLO_START, derive_solo_magic
    from engine.tokifactor import (
        CONTROL_TOPIC,
        decode_single_payload,
        encode_text_payloads,
    )

    sender_id = legacy_member_id("akictl")
    events: list[dict[str, str]] = []
    ready: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def on_message(_topic: str, payload: bytes, _replayed: bool) -> None:
        try:
            envelope = decode_single_payload(payload.decode("utf-8"), config.mqtt_magic)
        except (UnicodeDecodeError, ValueError):
            return
        if envelope is None:
            return
        metadata = dict(envelope.metadata)
        if metadata.get("schema") != "akid.v1":
            return
        if metadata.get("kind") not in (
            SOLO_START,
            SOLO_READY,
            SOLO_END,
            "solo_active",
        ):
            return
        events.append(metadata)
        if (
            action == "start"
            and metadata.get("kind") == SOLO_READY
            and metadata.get("soloId") == session_id
            and not ready.done()
        ):
            ready.set_result(metadata)

    client = MqttClient(
        config.mqtt_host,
        config.mqtt_port,
        client_id=f"akictl-solo-{os.getpid()}-{int(time.time())}",
        clean_session=True,
        on_message=on_message,
    )
    await client.connect(timeout=timeout)
    try:
        granted = await client.subscribe(CONTROL_TOPIC, qos=1)
        if granted == 0x80:
            raise RuntimeError("broker rejected control subscription")
        if action == "status":
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            active = next(
                (item for item in reversed(events) if item.get("kind") == SOLO_START),
                None,
            )
            ended = next(
                (item for item in reversed(events) if item.get("kind") == SOLO_END),
                None,
            )
            if ended and (
                active is None
                or str(ended.get("soloId") or "") == str(active.get("soloId") or "")
            ):
                active = None
            if active is None:
                local = _read_solo_control_state()
                if local is not None:
                    return {"active": True, "session": local, "source": "local"}
            return {"active": active is not None, "session": active}

        if action == "start" and target is not None:
            started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            metadata = {
                "schema": "akid.v1",
                "kind": SOLO_START,
                "protocolVersion": "1",
                "memberId": sender_id,
                "memberName": "akictl",
                "sourceId": f"akictl:{socket.gethostname()}",
                "memberKind": "user",
                "soloId": session_id,
                "soloTargetId": target.channel.id,
                "soloTargetName": target.channel.name,
                "soloStartedAt": started_at,
            }
            for payload in encode_text_payloads(
                "akictl solo start",
                config.mqtt_magic,
                sender="akictl",
                sender_id=sender_id,
                metadata=metadata,
            ):
                await client.publish(
                    CONTROL_TOPIC, payload.encode("utf-8"), qos=1, retain=True
                )
            try:
                result = await asyncio.wait_for(ready, timeout=timeout)
            except asyncio.TimeoutError as exc:
                abort = {
                    **metadata,
                    "kind": SOLO_END,
                }
                for payload in encode_text_payloads(
                    "akictl solo abort",
                    config.mqtt_magic,
                    sender="akictl",
                    sender_id=sender_id,
                    metadata=abort,
                ):
                    await client.publish(
                        CONTROL_TOPIC, payload.encode("utf-8"), qos=1, retain=True
                    )
                raise TimeoutError(
                    f"solo target {target.channel.name} did not become ready"
                ) from exc
            return {
                "active": True,
                "session_id": session_id,
                "target_profile": target.profile,
                "target_id": target.channel.id,
                "target_name": target.channel.name,
                "solo_magic": derive_solo_magic(config.mqtt_magic, target.channel.id),
                "ready": dict(result),
            }

        if action == "off":
            deadline = time.monotonic() + min(timeout, 1.5)
            while time.monotonic() < deadline and not events:
                await asyncio.sleep(0.05)
            active = next(
                (item for item in reversed(events) if item.get("kind") == SOLO_START),
                None,
            )
            ended = next(
                (item for item in reversed(events) if item.get("kind") == SOLO_END),
                None,
            )
            local = _read_solo_control_state()
            if not active and target is None and local is not None:
                active = local
            if not active and target is None:
                return {"active": False, "session": None}
            if active and (
                ended
                and str(ended.get("soloId") or "") == str(active.get("soloId") or "")
            ):
                return {"active": False, "session": None}
            target_id = (
                str(active.get("soloTargetId") or active.get("target_id") or "")
                if active
                else target.channel.id
            )
            target_name = (
                str(
                    active.get("soloTargetName")
                    or active.get("target_name")
                    or target_id
                )
                if active
                else target.channel.name
            )
            solo_id = (
                str(active.get("soloId") or active.get("session_id") or "")
                if active
                else ""
            )
            if not target_id:
                raise RuntimeError("active solo state is incomplete")
            for magic in (
                derive_solo_magic(config.mqtt_magic, target_id),
                config.mqtt_magic,
            ):
                metadata = {
                    "schema": "akid.v1",
                    "kind": SOLO_END,
                    "protocolVersion": "1",
                    "memberId": sender_id,
                    "memberName": "akictl",
                    "sourceId": f"akictl:{socket.gethostname()}",
                    "memberKind": "user",
                    "soloId": solo_id,
                    "soloTargetId": target_id,
                    "soloTargetName": target_name,
                }
                for payload in encode_text_payloads(
                    "akictl solo end",
                    magic,
                    sender="akictl",
                    sender_id=sender_id,
                    metadata=metadata,
                ):
                    await client.publish(
                        CONTROL_TOPIC,
                        payload.encode("utf-8"),
                        qos=1,
                        retain=True,
                    )
            return {
                "active": False,
                "ended_session_id": solo_id,
                "ended_target_id": target_id,
                "target_id": target_id,
                "target_name": target_name,
            }
        raise ValueError(f"unsupported solo action: {action}")
    finally:
        await client.close()


def _solo(args: argparse.Namespace) -> int:
    command = (args.solo_command or "status").strip()
    try:
        if command == "status":
            result = asyncio.run(
                _solo_broker_call(
                    _default_solo_config(), action="status", timeout=args.timeout
                )
            )
        elif command == "off":
            _, target = _solo_target(args.solo_target) if args.solo_target else (None, None)
            result = asyncio.run(
                _solo_broker_call(
                    _default_solo_config(),
                    action="off",
                    target=target,
                    timeout=args.timeout,
                )
            )
        else:
            name, target = _solo_target(command)
            result = asyncio.run(
                _solo_broker_call(
                    target,
                    action="start",
                    target=target,
                    session_id=f"solo-{uuid.uuid4().hex}",
                    timeout=args.timeout,
                )
            )
            result["target_profile"] = name
            _write_solo_control_state(result)
        if command == "off" and result.get("ended_target_id"):
            _clear_solo_control_state()
    except (ConfigError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("active"):
        session = result.get("session") or result
        print(
            "solo active: "
            f"{session.get('soloTargetName') or session.get('target_name')} "
            f"session={session.get('soloId') or session.get('session_id')}"
        )
    elif command == "off":
        print(
            "solo mode ended"
            if result.get("ended_session_id")
            or result.get("ended_target_id")
            else "solo mode is not active"
        )
    else:
        print("solo mode is not active")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", help="Unix socket path")


def _chat_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akictl chat", description="Chat with Aki")
    _add_common(parser)
    parser.add_argument("prompt_text", nargs="*", help="prompt (alternative to -p)")
    parser.add_argument("-p", "--prompt", help="send one prompt and exit")
    parser.add_argument("--json", action="store_true", help="print raw server NDJSON")
    parser.add_argument("--include-reasoning", action="store_true")
    parser.add_argument("--client-message-id")
    return parser


def build_parsers() -> dict[str, argparse.ArgumentParser]:
    parsers: dict[str, argparse.ArgumentParser] = {"chat": _chat_parser()}
    for name in ("status", "health", "allowlist"):
        parser = argparse.ArgumentParser(prog=f"akictl {name}")
        _add_common(parser)
        if name == "status":
            parser.add_argument("--json", action="store_true")
        parsers[name] = parser
    for name in ("up", "down", "restart"):
        parser = argparse.ArgumentParser(prog=f"akictl {name}")
        parser.add_argument("--timeout", type=float, default=30.0)
        parsers[name] = parser
    parser = argparse.ArgumentParser(prog="akictl logs")
    parser.add_argument("-n", "--lines", type=int, default=200)
    parser.add_argument("-f", "--follow", action="store_true")
    parsers["logs"] = parser
    parser = argparse.ArgumentParser(prog="akictl profiles")
    parser.add_argument("--json", action="store_true")
    parsers["profiles"] = parser
    parser = argparse.ArgumentParser(prog="akictl history")
    _add_common(parser)
    parser.add_argument("limit", nargs="?", type=int, default=50)
    parsers["history"] = parser
    parser = argparse.ArgumentParser(prog="akictl allow")
    _add_common(parser)
    parser.add_argument("path")
    parsers["allow"] = parser
    parser = argparse.ArgumentParser(prog="akictl disallow")
    _add_common(parser)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--all", action="store_true", help="remove all registered directories")
    parsers["disallow"] = parser
    parser = argparse.ArgumentParser(prog="akictl memory")
    _add_common(parser)
    parser.add_argument("memory_args", nargs=argparse.REMAINDER)
    parsers["memory"] = parser
    parser = argparse.ArgumentParser(prog="akictl mqtt")
    _add_common(parser)
    parser.add_argument("mqtt_command", nargs="?")
    parser.add_argument("mqtt_values", nargs="*")
    parsers["mqtt"] = parser
    parser = argparse.ArgumentParser(prog="akictl mcp")
    _add_common(parser)
    parser.add_argument("mcp_args", nargs="*")
    parsers["mcp"] = parser
    parser = argparse.ArgumentParser(prog="akictl reasoning")
    _add_common(parser)
    parser.add_argument("reasoning_command", nargs="?")
    parser.add_argument("reasoning_values", nargs="*")
    parsers["reasoning"] = parser
    parser = argparse.ArgumentParser(prog="akictl model")
    _add_common(parser)
    parser.add_argument("model_command", nargs="?")
    parser.add_argument("model_values", nargs="*")
    parsers["model"] = parser
    parser = argparse.ArgumentParser(prog="akictl avatar")
    _add_common(parser)
    parsers["avatar"] = parser
    parser = argparse.ArgumentParser(prog="akictl identity")
    _add_common(parser)
    parser.add_argument("identity_command", nargs="?", choices=("show", "rename"), default="show")
    parser.add_argument("name", nargs="?")
    parsers["identity"] = parser
    parser = argparse.ArgumentParser(
        prog="akictl reset",
        description="Reset the default conversation (backs up the old transcript first)",
    )
    _add_common(parser)
    parsers["reset"] = parser
    parser = argparse.ArgumentParser(prog="akictl username")
    _add_common(parser)
    parsers["username"] = parser
    parser = argparse.ArgumentParser(
        prog="akictl set-username",
        description="Set how Aki addresses the user (for example: Master)",
    )
    _add_common(parser)
    parser.add_argument("name")
    parsers["set-username"] = parser
    parser = argparse.ArgumentParser(
        prog="akictl recompile",
        description="Rebuild the current conversation prompt from MemFS",
    )
    _add_common(parser)
    parsers["recompile"] = parser
    parser = argparse.ArgumentParser(
        prog="akictl compact",
        description=(
            "Compact the conversation context and trigger compaction-event "
            "memory reflection"
        ),
    )
    _add_common(parser)
    parser.add_argument("mode", nargs="?", choices=COMPACT_MODES)
    parser.add_argument(
        "--mode", dest="mode_option", choices=COMPACT_MODES
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="seconds to wait for compaction and reflection (default: 900)",
    )
    parsers["compact"] = parser
    parser = argparse.ArgumentParser(prog="akictl solo")
    parser.add_argument("solo_command", nargs="?", default="status")
    parser.add_argument("solo_target", nargs="?")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    parsers["solo"] = parser
    parser = argparse.ArgumentParser(prog="akictl help")
    parser.add_argument("help_command", nargs="?")
    parsers["help"] = parser
    return parsers


def _extract_profile(argv: list[str]) -> tuple[str | None, list[str]]:
    profile: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--profile":
            if index + 1 >= len(argv):
                raise ConfigError("--profile requires a value")
            profile = argv[index + 1]
            index += 2
            continue
        if token.startswith("--profile="):
            profile = token.split("=", 1)[1]
            index += 1
            continue
        remaining.append(token)
        index += 1
    return profile, remaining


def main(argv: list[str] | None = None, *, default_command: str = "chat") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        _print_help()
        return 0
    try:
        profile_option, argv = _extract_profile(argv)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    positional_profile: str | None = None
    if argv and argv[0] not in ALL_COMMANDS:
        candidate: str | None = None
        try:
            candidate = validate_profile_name(argv[0])
        except ConfigError:
            if len(argv) >= 2 and argv[1] in ALL_COMMANDS:
                print(
                    "error: profile must match [a-z0-9][a-z0-9_-]{0,31}",
                    file=sys.stderr,
                )
                return 2
        if candidate is not None and (
            (len(argv) >= 2 and argv[1] in ALL_COMMANDS)
            or profile_config_path(candidate).is_file()
        ):
            positional_profile = candidate
            argv.pop(0)
    command = default_command
    if argv and argv[0] in ALL_COMMANDS:
        command = argv.pop(0)
    explicit_profile = profile_option is not None or positional_profile is not None
    config_path: Path | None = None
    if explicit_profile:
        profile = profile_option or positional_profile or "aki"
    else:
        try:
            profile, config_path = resolve_config_path(None)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        profile = validate_profile_name(profile)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parsers = build_parsers()
    args = parsers[command].parse_args(argv)
    args.config_path = config_path if not explicit_profile else None
    args.explicit_profile = explicit_profile
    if command == "profiles":
        return _profiles(json_mode=args.json)
    if command == "up":
        return _profile_up(profile, timeout=args.timeout, config_path=args.config_path)
    if command == "down":
        return _profile_down(profile)
    if command == "restart":
        return _profile_restart(profile, timeout=args.timeout, config_path=args.config_path)
    if command == "logs":
        return _profile_logs(profile, lines=args.lines, follow=args.follow)
    if command == "status":
        if explicit_profile:
            return _service_status(
                profile, json_mode=args.json, config_path=args.config_path
            )
        return _management(args, "aki.status", profile=profile)
    if command == "chat":
        return _chat(args, profile)
    if command == "health":
        return _management(args, "aki.health", profile=profile)
    if command == "allowlist":
        return _management(args, "aki.fs.writable.list", profile=profile)
    if command == "allow":
        return _management(args, "aki.fs.writable.add", {"path": args.path}, profile=profile)
    if command == "disallow":
        if args.all:
            return _management(args, "aki.fs.writable.clear", profile=profile)
        if not args.path:
            print("akictl disallow: path required (or use --all)", file=sys.stderr)
            return 2
        return _management(
            args, "aki.fs.writable.remove", {"path": args.path}, profile=profile
        )
    if command == "history":
        return _history(args, profile)
    if command == "solo":
        return _solo(args)
    if command == "memory":
        return _memory(args, profile)
    if command == "mqtt":
        return _mqtt(args, profile)
    if command == "mcp":
        return _mcp(args, profile)
    if command == "reasoning":
        return _reasoning(args, profile)
    if command == "model":
        return _model(args, profile)
    if command == "avatar":
        return _management(args, "aki.avatar.status", profile=profile)
    if command == "identity":
        return _identity(args, profile)
    if command == "reset":
        return _management(args, "aki.conversation.reset", profile=profile)
    if command == "username":
        return _management(args, "aki.username.get", profile=profile)
    if command == "set-username":
        return _management(
            args, "aki.username.set", {"username": args.name}, profile=profile
        )
    if command == "recompile":
        return _management(args, "aki.conversation.recompile", profile=profile)
    if command == "compact":
        return _compact(args, profile)
    if command == "help":
        if not args.help_command:
            _print_help()
            return 0
        parser = parsers.get(args.help_command)
        if parser is None:
            print(f"unknown command: {args.help_command}", file=sys.stderr)
            return 2
        parser.print_help()
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
