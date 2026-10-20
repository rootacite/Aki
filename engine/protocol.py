"""Wire protocol helpers for the Aki gateway (JSON-RPC 2.0 over NDJSON)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = 1
SERVER_VERSION = "0.1.0"
MAX_LINE_BYTES = 1024 * 1024


class ErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    QUEUE_FULL = 1001
    BACKEND_UNAVAILABLE = 1002
    INVALID_PATH = 1003
    NOT_INITIATOR = 1004
    TURN_NOT_FOUND = 1005
    PROTOCOL_VERSION = 1006
    SANDBOX_UNAVAILABLE = 1007
    PATH_ALREADY_REGISTERED = 1008
    PATH_NOT_REGISTERED = 1009
    SANDBOX_RESTART_FAILED = 1010
    PATH_IS_DEFAULT = 1011
    RESET_FAILED = 1012
    USERNAME_FAILED = 1013
    RECOMPILE_FAILED = 1014


class RpcError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data) if data is not None else None

    def as_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class FrameError(Exception):
    """A malformed or oversized NDJSON frame."""


@dataclass(frozen=True)
class Request:
    id: Any
    method: str
    params: dict[str, Any]

    @property
    def is_notification(self) -> bool:
        return self.id is None


def encode_message(message: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > MAX_LINE_BYTES:
        raise FrameError("outgoing frame exceeds 1 MiB")
    return payload + b"\n"


def decode_message(raw: bytes) -> Any:
    if len(raw) > MAX_LINE_BYTES + 1:
        raise FrameError("frame exceeds 1 MiB")
    text = raw.decode("utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise FrameError("invalid JSON") from exc


async def read_message(reader: asyncio.StreamReader) -> bytes | None:
    try:
        raw = await reader.readline()
    except ValueError as exc:  # asyncio raises when the stream limit is crossed
        raise FrameError("frame exceeds 1 MiB") from exc
    if not raw:
        return None
    if len(raw) > MAX_LINE_BYTES + 1:
        raise FrameError("frame exceeds 1 MiB")
    return raw


def parse_request(value: Any) -> Request:
    if not isinstance(value, dict):
        raise RpcError(ErrorCode.INVALID_REQUEST, "request must be an object")
    if value.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(ErrorCode.INVALID_REQUEST, "jsonrpc must be '2.0'")
    if "id" not in value:
        raise RpcError(ErrorCode.INVALID_REQUEST, "id is required")
    request_id = value.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise RpcError(ErrorCode.INVALID_REQUEST, "id must be a string, number, or null")
    method = value.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(ErrorCode.INVALID_REQUEST, "method must be a non-empty string")
    params = value.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(ErrorCode.INVALID_PARAMS, "params must be an object")
    return Request(id=request_id, method=method, params=params)


def make_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_error(
    request_id: Any,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def make_event(
    turn_id: str,
    seq: int,
    event_type: str,
    data: Mapping[str, Any] | None,
    timestamp: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "method": "aki.event",
        "params": {
            "turn_id": turn_id,
            "seq": seq,
            "type": event_type,
            "data": dict(data or {}),
            "timestamp": timestamp,
        },
    }


def require_string(
    params: Mapping[str, Any], name: str, *, allow_empty: bool = False
) -> str:
    value = params.get(name)
    if not isinstance(value, str):
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must not be empty")
    return value


def optional_string(params: Mapping[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be a string")
    return value


def require_int(
    params: Mapping[str, Any],
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be <= {maximum}")
    return value


def optional_bool(params: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise RpcError(ErrorCode.INVALID_PARAMS, f"{name} must be a boolean")
    return value
