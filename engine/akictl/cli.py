"""Argument parsers and the akictl command dispatch."""

from __future__ import annotations

import argparse
import sys
from engine.aki_config import (
    ConfigError,
    profile_config_path,
    resolve_config_path,
    validate_profile_name,
)
from engine.client import (
    _memory_command,
    _print_json,
)
from engine.letta_compact import COMPACT_MODES
from engine.protocol import RpcError
from .chat import (
    _channel_history_file,
    _chat,
    _connect,
    _print_history_file,
)
from .service import (
    _profile_down,
    _profile_logs,
    _profile_restart,
    _profile_up,
    _profiles,
    _service_status,
)
from .solo import _solo
from .support import (
    _mcp_call,
    _model_call,
    _mqtt_call,
    _print_error,
    _reasoning_call,
    _rpc_call,
    _socket_from,
)

from .access import pkg


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
    "  akictl                     MQTT chat REPL (default profile aki)\n"
    "  akictl -p '...'            send one MQTT message\n"
    "  akictl help [command]      show this help or help for one command\n"
    "  akictl allow <dir>         register a writable directory\n"
    "  akictl disallow <dir>      revoke a writable directory\n"
    "  akictl disallow --all      revoke all registered directories\n"
    "  akictl allowlist           show the effective writable policy\n"
    "  akictl status              show the default profile's gateway status\n"
    "  akictl health              gateway health\n"
    "  akictl history [n] [--solo]  local channel history\n"
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
    "  akictl compact [mode]      keep newest 40% by count, summarize the rest\n"
    "  akictl solo <member>       enter private solo mode with one member\n"
    "  akictl solo status         show the active solo session\n"
    "  akictl solo off            end solo mode\n"
    "  akictl up | down | restart manage the default profile service\n"
    "  akictl logs                show profile service logs\n"
    "\nUse 'akictl <command> --help' for command-specific options."
)



def _print_help() -> None:
    pkg().print(HELP_TEXT)



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
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
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
    try:
        path = _channel_history_file(args, profile)
        return _print_history_file(
            path, limit=args.limit, json_mode=bool(args.json)
        )
    except (OSError, ValueError, ConfigError) as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 1



def _identity(args: argparse.Namespace, profile: str = "aki") -> int:
    command = getattr(args, "identity_command", None) or "show"
    if command == "show":
        return pkg()._management(args, "aki.identity.get", profile=profile)
    if command == "rename":
        name = getattr(args, "name", "").strip()
        if not name:
            pkg().print("akictl identity rename: name required", file=sys.stderr)
            return 2
        return pkg()._management(
            args, "aki.identity.rename", {"name": name}, profile=profile
        )
    pkg().print(f"unknown identity command: {command}", file=sys.stderr)
    return 2



def _memory(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
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
    pkg().print(
        "compacting context and waiting for memory reflection...",
        file=sys.stderr,
        flush=True,
    )
    return pkg()._management(
        args,
        "aki.conversation.compact",
        params,
        profile=profile,
        timeout=float(args.timeout) + 30.0,
    )



def _mqtt(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
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



def _model(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
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



def _reasoning(args: argparse.Namespace, profile: str = "aki") -> int:
    socket_path = _socket_from(args, profile)
    try:
        client = _connect(socket_path, json_mode=False)
    except (OSError, RpcError, ConnectionError) as exc:
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
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
        pkg().print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1
    try:
        _print_json(_mcp_call(client, list(args.mcp_args)))
        return 0
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        _print_error(exc)
        return 1
    finally:
        client.close()



def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", help="Unix socket path")



def _chat_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="akictl chat", description="Chat over the MQTT channel"
    )
    _add_common(parser)
    parser.add_argument("prompt_text", nargs="*", help="prompt (alternative to -p)")
    parser.add_argument("-p", "--prompt", help="send one prompt and exit")
    parser.add_argument("--json", action="store_true", help="print channel event JSON")
    parser.add_argument("--include-reasoning", action="store_true")
    parser.add_argument("--client-message-id")
    parser.add_argument("--timeout", type=float, default=180.0)
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
    parser.add_argument("--solo", action="store_true", help="read solo history")
    parser.add_argument("--json", action="store_true")
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
            "Keep the newest 40% of messages by count, summarize the rest, "
            "and trigger memory reflection"
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
        pkg().print(f"error: {exc}", file=sys.stderr)
        return 2
    positional_profile: str | None = None
    if argv and argv[0] not in ALL_COMMANDS:
        candidate: str | None = None
        try:
            candidate = validate_profile_name(argv[0])
        except ConfigError:
            if len(argv) >= 2 and argv[1] in ALL_COMMANDS:
                pkg().print(
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
            pkg().print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        profile = validate_profile_name(profile)
    except ConfigError as exc:
        pkg().print(f"error: {exc}", file=sys.stderr)
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
        return pkg()._management(args, "aki.status", profile=profile)
    if command == "chat":
        return _chat(args, profile)
    if command == "health":
        return pkg()._management(args, "aki.health", profile=profile)
    if command == "allowlist":
        return pkg()._management(args, "aki.fs.writable.list", profile=profile)
    if command == "allow":
        return pkg()._management(args, "aki.fs.writable.add", {"path": args.path}, profile=profile)
    if command == "disallow":
        if args.all:
            return pkg()._management(args, "aki.fs.writable.clear", profile=profile)
        if not args.path:
            pkg().print("akictl disallow: path required (or use --all)", file=sys.stderr)
            return 2
        return pkg()._management(
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
        return pkg()._management(args, "aki.avatar.status", profile=profile)
    if command == "identity":
        return _identity(args, profile)
    if command == "reset":
        return pkg()._management(args, "aki.conversation.reset", profile=profile)
    if command == "username":
        return pkg()._management(args, "aki.username.get", profile=profile)
    if command == "set-username":
        return pkg()._management(
            args, "aki.username.set", {"username": args.name}, profile=profile
        )
    if command == "recompile":
        return pkg()._management(args, "aki.conversation.recompile", profile=profile)
    if command == "compact":
        return _compact(args, profile)
    if command == "help":
        if not args.help_command:
            _print_help()
            return 0
        parser = parsers.get(args.help_command)
        if parser is None:
            pkg().print(f"unknown command: {args.help_command}", file=sys.stderr)
            return 2
        parser.print_help()
        return 0
    pkg().print(f"unknown command: {command}", file=sys.stderr)
    return 2
