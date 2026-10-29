"""Small akictl helpers shared by the command modules."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from engine.aki_config import (
    load_config,
    resolve_config_path,
)
from engine.client import default_socket_path
from engine.protocol import RpcError

from .access import pkg



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
        pkg().print(f"error: {exc.message}", file=sys.stderr)
        if exc.data:
            pkg().print(json.dumps(exc.data, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        pkg().print(f"error: {exc}", file=sys.stderr)



def _b64url(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")



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



def _model_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.model.get")
    if rest[0] != "set" or len(rest) < 2:
        raise ValueError("usage: /model [status] | /model set <id>")
    return _rpc_call(client, "aki.model.set", {"model": rest[1]})



def _reasoning_call(client: GatewayClient, rest: list[str]) -> Any:
    if not rest or rest[0] == "status":
        return _rpc_call(client, "aki.reasoning.get")
    if rest[0] != "set":
        raise ValueError("usage: /reasoning [status] | /reasoning set [low|medium|high]")
    effort = rest[1] if len(rest) > 1 else "low"
    return _rpc_call(client, "aki.reasoning.set", {"effort": effort})
