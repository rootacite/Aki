"""Synchronous client for the Aki gateway, used by ``bin/aki``."""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from engine.protocol import PROTOCOL_VERSION, ErrorCode, RpcError

CLIENT_NAME = "aki-cli"
CLIENT_VERSION = "0.1.0"


def default_socket_path() -> Path:
    configured = os.environ.get("AKI_SOCKET")
    if configured:
        return Path(configured).expanduser()
    config_path = Path(os.environ["AKI_CONFIG"]).expanduser() if os.environ.get("AKI_CONFIG") else Path.home() / ".aki" / "aki.conf"
    if config_path.is_file():
        try:
            from engine.aki_config import load_config

            socket = load_config(config_path).socket
        except (OSError, ValueError):
            socket = ""
        if socket:
            return Path(socket).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "aki" / "akid.sock"
    return Path(f"/tmp/aki-{os.getuid()}") / "akid.sock"


@dataclass
class Event:
    raw: str
    params: dict[str, Any]

    @property
    def turn_id(self) -> str:
        return str(self.params.get("turn_id") or "")

    @property
    def type(self) -> str:
        return str(self.params.get("type") or "")

    @property
    def data(self) -> dict[str, Any]:
        data = self.params.get("data")
        return data if isinstance(data, dict) else {}


class GatewayClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        client_name: str = CLIENT_NAME,
        client_version: str = CLIENT_VERSION,
        raw_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.client_name = client_name
        self.client_version = client_version
        self.raw_sink = raw_sink
        self.events: queue.Queue[Event] = queue.Queue()
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = threading.Event()
        self._error: BaseException | None = None

    def connect(self, timeout: float = 5.0) -> "GatewayClient":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.socket_path))
        sock.settimeout(None)
        self._sock = sock
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="aki-client-reader", daemon=True
        )
        self._reader_thread.start()
        return self

    def handshake(self) -> dict[str, Any]:
        return self.call(
            "aki.hello",
            {
                "client_name": self.client_name,
                "client_version": self.client_version,
                "protocol_version": PROTOCOL_VERSION,
            },
        )

    def _read_loop(self) -> None:
        assert self._sock is not None
        file = self._sock.makefile("rb")
        try:
            while True:
                raw = file.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip("\n")
                if self.raw_sink is not None:
                    self.raw_sink(text)
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "aki.event":
                    params = message.get("params")
                    if isinstance(params, dict):
                        self.events.put(Event(raw=text, params=params))
                    continue
                request_id = message.get("id")
                if request_id is None:
                    continue
                with self._pending_lock:
                    entry = self._pending.pop(str(request_id), None)
                if entry is not None:
                    waiter, box = entry
                    box["message"] = message
                    waiter.set()
        except (OSError, ValueError) as exc:
            self._error = exc
        finally:
            self._closed.set()
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for waiter, box in pending:
                box["message"] = {
                    "error": {"code": ErrorCode.BACKEND_UNAVAILABLE, "message": "gateway connection closed"}
                }
                waiter.set()

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("client is not connected")
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(payload)

    def call(self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if self._closed.is_set():
            raise ConnectionError(self._error or "gateway connection closed")
        self._next_id += 1
        request_id = f"c{self._next_id}"
        waiter = threading.Event()
        box: dict[str, Any] = {}
        with self._pending_lock:
            self._pending[request_id] = (waiter, box)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
            }
        )
        if not waiter.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"gateway request timed out: {method}")
        message = box.get("message") or {}
        if "error" in message:
            error = message["error"] or {}
            raise RpcError(
                int(error.get("code", ErrorCode.INTERNAL_ERROR)),
                str(error.get("message") or "gateway error"),
                error.get("data"),
            )
        return message.get("result")

    def next_event(self, timeout: float | None = None) -> Event | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "GatewayClient":
        return self.connect()

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _approval_prompt(event: Event) -> tuple[bool, str | None]:
    data = event.data
    tool = data.get("tool_name") or "unknown"
    tool_input = data.get("input")
    print("", file=sys.stderr)
    print(f"approval required: {tool}", file=sys.stderr)
    if tool_input is not None:
        rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "\n... (truncated)"
        print(rendered, file=sys.stderr)
    if not sys.stdin.isatty():
        print("non-interactive stdin: denying tool", file=sys.stderr)
        return False, "non-interactive client"
    try:
        answer = input("allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return False, "approval input cancelled"
    if answer in ("y", "yes"):
        return True, None
    return False, "denied by user"


def _format_message(message: Mapping[str, Any]) -> str:
    kind = str(message.get("message_type") or message.get("type") or "message")
    if kind in ("assistant_message", "user_message"):
        return f"{kind}: {_content_text(message.get('content'))}"
    if kind == "reasoning_message":
        return f"reasoning: {message.get('reasoning') or ''}"
    if kind == "tool_call_message":
        call = message.get("tool_call") or {}
        return f"tool_call: {call.get('name')} {call.get('arguments', '')}"
    if kind == "tool_return_message":
        return f"tool_return({message.get('status')}): {message.get('tool_return')}"
    if kind == "summary_message":
        return f"summary: {message.get('summary')}"
    text = _content_text(message.get("content"))
    return f"{kind}: {text}" if text else kind


class StreamPrinter:
    def __init__(self, *, json_mode: bool, show_reasoning: bool) -> None:
        self.json_mode = json_mode
        self.show_reasoning = show_reasoning
        self.streamed = False
        self.at_line_start = True
        self.final_message_printed = False

    def delta(self, text: str, *, reasoning: bool = False) -> None:
        if self.json_mode:
            return
        if reasoning and not self.show_reasoning:
            return
        if reasoning:
            if not self.at_line_start:
                print("", file=sys.stderr)
                self.at_line_start = True
            print(f"[thinking] {text}", file=sys.stderr, end="", flush=True)
            return
        sys.stdout.write(text)
        sys.stdout.flush()
        self.streamed = True
        self.at_line_start = text.endswith("\n")

    def final_assistant(self, text: str) -> None:
        if self.json_mode or self.final_message_printed:
            return
        if self.streamed:
            if not self.at_line_start:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self.at_line_start = True
            self.final_message_printed = True
            return
        if text:
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
            self.at_line_start = True
        self.final_message_printed = True

    def finish(self) -> None:
        if not self.json_mode and not self.at_line_start:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.at_line_start = True


def run_turn(
    client: GatewayClient,
    prompt: str,
    *,
    json_mode: bool,
    show_reasoning: bool,
    client_message_id: str | None = None,
) -> bool:
    params: dict[str, Any] = {
        "message": prompt,
        "include_reasoning": show_reasoning,
    }
    if client_message_id:
        params["client_message_id"] = client_message_id
    response = client.call("aki.chat.send", params)
    turn_id = str(response.get("turn_id") or "")
    state = response.get("state")
    if not json_mode:
        if state == "queued":
            print(
                f"[queued] turn {turn_id} position {response.get('queue_position')}",
                file=sys.stderr,
            )
    printer = StreamPrinter(json_mode=json_mode, show_reasoning=show_reasoning)
    try:
        while True:
            event = client.next_event(timeout=1.0)
            if event is None:
                if client._closed.is_set():
                    print("gateway connection closed", file=sys.stderr)
                    return False
                continue
            if event.turn_id != turn_id:
                continue
            data = event.data
            kind = event.type
            if kind == "assistant_delta":
                printer.delta(
                    str(data.get("text") or ""), reasoning=bool(data.get("reasoning"))
                )
            elif kind == "assistant_message":
                printer.final_assistant(str(data.get("content") or data.get("text") or ""))
            elif kind == "approval_request":
                allow, message = _approval_prompt(event)
                try:
                    client.call(
                        "aki.approval.respond",
                        {
                            "approval_id": data.get("approval_id"),
                            "decision": "allow" if allow else "deny",
                            **({"message": message} if message else {}),
                        },
                    )
                except RpcError as exc:
                    print(f"approval response failed: {exc}", file=sys.stderr)
                except (ConnectionError, TimeoutError) as exc:
                    print(f"approval response failed: {exc}", file=sys.stderr)
                    return False
            elif kind == "tool_call":
                if not json_mode:
                    print(
                        f"[tool] {data.get('name')} ({data.get('tool_call_id')})",
                        file=sys.stderr,
                    )
            elif kind == "tool_result":
                if not json_mode:
                    status = data.get("status")
                    print(f"[tool result] {status}", file=sys.stderr)
            elif kind == "usage":
                if not json_mode and data:
                    context = data.get("context_tokens")
                    total = data.get("total_tokens")
                    if context is not None or total is not None:
                        print(
                            f"[usage] total={total} context={context}",
                            file=sys.stderr,
                        )
            elif kind == "compaction":
                if not json_mode:
                    print("[compaction] context compacted", file=sys.stderr)
            elif kind == "turn_queued":
                if not json_mode:
                    print(
                        f"[queued] position {data.get('queue_position')}",
                        file=sys.stderr,
                    )
            elif kind == "error":
                message = data.get("message") or data.get("code") or "unknown error"
                print(f"[error] {message}", file=sys.stderr)
            elif kind == "turn_end":
                printer.finish()
                return data.get("reason") not in (
                    "error",
                    "backend_error",
                    "backend_unavailable",
                    "internal_error",
                    "sandbox_restart",
                )
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        print("cancelling turn...", file=sys.stderr)
        try:
            client.call("aki.chat.cancel", {"turn_id": turn_id})
        except (RpcError, ConnectionError, TimeoutError) as exc:
            print(f"cancel failed: {exc}", file=sys.stderr)
            return False
        while True:
            event = client.next_event(timeout=10.0)
            if event is None:
                return False
            if event.turn_id == turn_id and event.type == "turn_end":
                printer.finish()
                return True


def _memory_command(client: GatewayClient, rest: list[str]) -> None:
    if not rest:
        print("usage: /memory tree|read|search|tokens|status ...", file=sys.stderr)
        return
    sub = rest[0]
    try:
        if sub == "tree":
            depth = int(rest[1]) if len(rest) > 1 else 4
            result = client.call("aki.memory.tree", {"depth": depth})
            for entry in result.get("entries", []):
                size = "" if entry.get("size") is None else f" {entry['size']}B"
                print(f"{entry.get('type')} {entry.get('path')}{size}")
        elif sub == "read":
            if len(rest) < 2:
                print("usage: /memory read <path>", file=sys.stderr)
                return
            result = client.call("aki.memory.read", {"path": rest[1]})
            print(result.get("content", ""), end="" if str(result.get("content", "")).endswith("\n") else "\n")
        elif sub == "search":
            if len(rest) < 2:
                print("usage: /memory search <query>", file=sys.stderr)
                return
            result = client.call("aki.memory.search", {"query": " ".join(rest[1:])})
            for match in result.get("matches", []):
                print(f"{match.get('path')}:{match.get('line')}: {match.get('text')}")
        elif sub == "tokens":
            _print_json(client.call("aki.memory.tokens"))
        elif sub == "status":
            _print_json(client.call("aki.memory.status"))
        else:
            print(f"unknown memory command: {sub}", file=sys.stderr)
    except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
        print(f"memory error: {exc}", file=sys.stderr)


def repl(client: GatewayClient, *, json_mode: bool, show_reasoning: bool) -> int:
    print("Aki gateway REPL. Commands: /status /history [n] /memory ... /quit", file=sys.stderr)
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
                _print_json(client.call("aki.status"))
            elif prompt.startswith("/history"):
                parts = prompt.split()
                limit = int(parts[1]) if len(parts) > 1 else 50
                result = client.call("aki.chat.history", {"limit": limit})
                for message in result.get("messages", []):
                    print(_format_message(message))
            elif prompt.startswith("/memory"):
                _memory_command(client, prompt.split()[1:])
            else:
                run_turn(
                    client,
                    line,
                    json_mode=json_mode,
                    show_reasoning=show_reasoning,
                )
        except (RpcError, ConnectionError, TimeoutError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to Aki through the local gateway")
    parser.add_argument("prompt_text", nargs="*", help="prompt (alternative to -p)")
    parser.add_argument("-p", "--prompt", help="send one prompt and exit")
    parser.add_argument("--json", action="store_true", help="print raw server NDJSON")
    parser.add_argument("--include-reasoning", action="store_true", help="stream reasoning deltas")
    parser.add_argument("--socket", help="Unix socket path")
    parser.add_argument("--client-message-id", help="idempotency key for one-shot prompts")
    args = parser.parse_args(argv)

    socket_path = Path(args.socket).expanduser() if args.socket else default_socket_path()
    prompt = args.prompt
    if prompt is None and args.prompt_text:
        prompt = " ".join(args.prompt_text)

    raw_lock = threading.Lock()

    def raw_sink(line: str) -> None:
        with raw_lock:
            print(line, flush=True)

    client = GatewayClient(socket_path, raw_sink=raw_sink if args.json else None)
    try:
        client.connect()
        client.handshake()
    except (OSError, RpcError, ConnectionError) as exc:
        print(f"cannot connect to akid at {socket_path}: {exc}", file=sys.stderr)
        return 1

    try:
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
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0 if ok else 1
        return repl(client, json_mode=args.json, show_reasoning=args.include_reasoning)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
