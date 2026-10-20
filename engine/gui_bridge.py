"""Local WebSocket bridge from GUI clients to the akid Unix socket.

The bridge is intentionally a transparent JSON-RPC proxy: one WebSocket text
message carries one JSON object, and the bridge exchanges it with akid as a
single NDJSON line.  It does not interpret chat content or log message bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import signal
import tempfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

LOGGER = logging.getLogger("akid.gui_bridge")

MAX_FRAME_BYTES = 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def default_socket_path() -> Path:
    configured = os.environ.get("AKI_SOCKET")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "aki" / "akid.sock"
    return Path(f"/tmp/aki-{os.getuid()}") / "akid.sock"


def default_state_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "aki"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def ensure_token(path: Path) -> str:
    try:
        value = path.read_text("utf-8").strip()
    except FileNotFoundError:
        value = ""
    except OSError as exc:
        raise RuntimeError(f"cannot read GUI token file: {exc}") from exc
    if value:
        return value
    token = secrets.token_urlsafe(32)
    try:
        _write_private(path, token + "\n")
    except OSError as exc:
        raise RuntimeError(f"cannot write GUI token file: {exc}") from exc
    return token


def write_endpoint(path: Path, *, host: str, port: int, token: str) -> None:
    payload = {
        "version": 1,
        "host": host,
        "port": port,
        "url": f"ws://{host}:{port}/?token={token}",
        "token": token,
    }
    try:
        _write_private(path, json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RuntimeError(f"cannot write GUI endpoint file: {exc}") from exc


class GuiBridge:
    def __init__(
        self,
        *,
        socket_path: Path,
        host: str,
        port: int,
        token: str,
        endpoint_path: Path,
    ) -> None:
        self.socket_path = socket_path
        self.host = host
        self.port = port
        self.token = token
        self.endpoint_path = endpoint_path
        self._server = None

    def _authorized(self, request) -> bool:
        query = parse_qs(urlsplit(request.path or "").query)
        supplied = (query.get("token") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _reject_unauthorized(self, connection, request):
        if self._authorized(request):
            return None
        LOGGER.warning("rejected unauthorized GUI bridge connection")
        return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    async def _forward_client_to_akid(self, websocket, writer: asyncio.StreamWriter) -> None:
        async for message in websocket:
            if isinstance(message, bytes):
                try:
                    message = message.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("binary WebSocket message is not valid UTF-8") from exc
            if len(message.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ValueError("WebSocket message exceeds 1 MiB")
            payload = message.rstrip("\r\n")
            if not payload:
                continue
            writer.write((payload + "\n").encode("utf-8"))
            await writer.drain()

    async def _forward_akid_to_client(
        self, websocket, reader: asyncio.StreamReader
    ) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            if len(line) > MAX_FRAME_BYTES + 1:
                raise ValueError("akid frame exceeds 1 MiB")
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text:
                await websocket.send(text)

    async def handler(self, websocket) -> None:
        peer = getattr(websocket, "remote_address", None)
        LOGGER.info("GUI client connected peer=%s", peer)
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.socket_path), limit=MAX_FRAME_BYTES + 2
            )
        except OSError as exc:
            LOGGER.warning("cannot connect to akid socket: %s", exc)
            await websocket.close(1011, "akid unavailable")
            return
        tasks = {
            asyncio.create_task(
                self._forward_client_to_akid(websocket, writer),
                name="gui-bridge-client-to-akid",
            ),
            asyncio.create_task(
                self._forward_akid_to_client(websocket, reader),
                name="gui-bridge-akid-to-client",
            ),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
                    task.result()
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except ValueError as exc:
            LOGGER.warning("GUI bridge framing error: %s", exc)
            with contextlib.suppress(Exception):
                await websocket.close(1009, "frame too large or malformed")
        except Exception:
            LOGGER.exception("GUI bridge connection failed")
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await websocket.close()
            LOGGER.info("GUI client disconnected peer=%s", peer)

    async def start(self) -> None:
        self._server = await serve(
            self.handler,
            self.host,
            self.port,
            process_request=self._reject_unauthorized,
            max_size=MAX_FRAME_BYTES,
            max_queue=8,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        write_endpoint(
            self.endpoint_path,
            host=self.host,
            port=self.port,
            token=self.token,
        )
        LOGGER.info(
            "GUI bridge listening host=%s port=%s socket=%s",
            self.host,
            self.port,
            self.socket_path,
        )
        await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def build_bridge() -> GuiBridge:
    token_path = Path(
        os.environ.get("AKI_GUI_TOKEN_FILE")
        or default_state_dir() / "gui-token"
    ).expanduser()
    endpoint_path = Path(
        os.environ.get("AKI_GUI_ENDPOINT_FILE")
        or default_state_dir() / "gui-bridge.json"
    ).expanduser()
    token = ensure_token(token_path)
    return GuiBridge(
        socket_path=Path(
            os.environ.get("AKI_SOCKET") or default_socket_path()
        ).expanduser(),
        host=os.environ.get("AKI_GUI_BRIDGE_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=int(os.environ.get("AKI_GUI_BRIDGE_PORT", DEFAULT_PORT)),
        token=token,
        endpoint_path=endpoint_path,
    )


async def _main() -> None:
    bridge = build_bridge()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    runner = asyncio.create_task(bridge.start(), name="gui-bridge-server")
    stopper = asyncio.create_task(stop.wait(), name="gui-bridge-stop")
    done, pending = await asyncio.wait(
        {runner, stopper}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    if runner in done:
        runner.result()
    await bridge.stop()


def main() -> int:
    logging.basicConfig(
        level=getattr(
            logging, os.environ.get("AKI_LOG_LEVEL", "INFO").upper(), logging.INFO
        ),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        return 130
    except Exception:
        LOGGER.exception("GUI bridge stopped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
