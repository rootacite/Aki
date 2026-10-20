"""akictl: chat and local-socket management for Aki."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from engine.client import (
    GatewayClient,
    _format_message,
    _memory_command,
    _print_json,
    default_socket_path,
    run_turn,
)
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
    "avatar",
    "reset",
    "username",
    "set-username",
}


def _socket_from(args: argparse.Namespace) -> Path:
    return Path(args.socket).expanduser() if getattr(args, "socket", None) else default_socket_path()


def _rpc_call(client: GatewayClient, method: str, params: dict[str, Any] | None = None) -> Any:
    return client.call(method, params or {})


def _print_error(exc: BaseException) -> None:
    if isinstance(exc, RpcError):
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.data:
            print(json.dumps(exc.data, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print(f"error: {exc}", file=sys.stderr)


def repl(client: GatewayClient, *, json_mode: bool, show_reasoning: bool) -> int:
    print(
        "akictl REPL. Commands: /status /health /history [n] /allow <dir> "
        "/disallow <dir> /allowlist /memory ... /mqtt ... /avatar /username "
        "/set-username <name> /reset /quit",
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


def _chat(args: argparse.Namespace) -> int:
    socket_path = _socket_from(args)
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


def _management(args: argparse.Namespace, method: str, params: dict[str, Any] | None = None) -> int:
    socket_path = _socket_from(args)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        result = _rpc_call(client, method, params)
        _print_json(result)
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()


def _history(args: argparse.Namespace) -> int:
    socket_path = _socket_from(args)
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


def _memory(args: argparse.Namespace) -> int:
    socket_path = _socket_from(args)
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


def _mqtt_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.mqtt.status")
    if rest[0] != "set":
        raise ValueError("usage: /mqtt [status] | /mqtt set <host> [port]")
    host, port = _parse_mqtt_server(rest[1:])
    return _rpc_call(client, "aki.mqtt.set", {"host": host, "port": port})


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


def _mqtt(args: argparse.Namespace) -> int:
    socket_path = _socket_from(args)
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
        parsers[name] = parser
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
    parser = argparse.ArgumentParser(prog="akictl avatar")
    _add_common(parser)
    parsers["avatar"] = parser
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
    return parsers


def main(argv: list[str] | None = None, *, default_command: str = "chat") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(
            "usage: akictl [chat|status|health|history|allow|disallow|allowlist|memory|mqtt|avatar|reset|username|set-username] [options]\n"
            "\n"
            "  akictl                     streaming chat REPL\n"
            "  akictl -p '...'            one-shot chat\n"
            "  akictl allow <dir>         register a writable directory\n"
            "  akictl disallow <dir>      revoke a writable directory\n"
            "  akictl disallow --all      revoke all registered directories\n"
            "  akictl allowlist           show the effective writable policy\n"
            "  akictl status | health     gateway status\n"
            "  akictl history [n]         conversation history\n"
            "  akictl memory ...          read-only memory commands\n"
            "  akictl mqtt [status]       show the active MQTT broker\n"
            "  akictl mqtt set <server>   persist and reconnect to host:port\n"
            "  akictl avatar              show Aki's avatar asset status\n"
            "  akictl reset               reset the default conversation (backs up first)\n"
            "  akictl username            show how Aki addresses the user\n"
            "  akictl set-username NAME   set how Aki addresses the user (e.g. Master)\n"
            "\nUse 'akictl <command> --help' for command-specific options."
        )
        return 0
    command = default_command
    if argv and argv[0] in COMMANDS:
        command = argv.pop(0)
    parsers = build_parsers()
    args = parsers[command].parse_args(argv)
    if command == "chat":
        return _chat(args)
    if command == "status":
        return _management(args, "aki.status")
    if command == "health":
        return _management(args, "aki.health")
    if command == "allowlist":
        return _management(args, "aki.fs.writable.list")
    if command == "allow":
        return _management(args, "aki.fs.writable.add", {"path": args.path})
    if command == "disallow":
        if args.all:
            return _management(args, "aki.fs.writable.clear")
        if not args.path:
            print("akictl disallow: path required (or use --all)", file=sys.stderr)
            return 2
        return _management(args, "aki.fs.writable.remove", {"path": args.path})
    if command == "history":
        return _history(args)
    if command == "memory":
        return _memory(args)
    if command == "mqtt":
        return _mqtt(args)
    if command == "avatar":
        return _management(args, "aki.avatar.status")
    if command == "reset":
        return _management(args, "aki.conversation.reset")
    if command == "username":
        return _management(args, "aki.username.get")
    if command == "set-username":
        return _management(args, "aki.username.set", {"username": args.name})
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
