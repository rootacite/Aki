"""Managed local OpenAI-compatible backend for Aki solo mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from engine.aki_config import SoloBackend, split_startup_script, state_dir


LOGGER = logging.getLogger("akid.solo-backend")

STATE_SCHEMA = "akid.solo-backend.v1"
DEFAULT_STARTUP_TIMEOUT = 300.0
DEFAULT_SHUTDOWN_TIMEOUT = 15.0
DEFAULT_IDLE_TIMEOUT = 900.0
POLL_INTERVAL = 0.5
HTTP_TIMEOUT = 5.0
ACTIVITY_HEARTBEAT_SECONDS = 60.0


class SoloBackendError(RuntimeError):
    """A solo local backend could not be started, discovered, or stopped."""


@dataclass(frozen=True)
class SoloRoute:
    model_id: str
    upstream_model: str
    context_window: int
    max_tokens: int
    inputs: tuple[str, ...]
    endpoint: str
    api: str = "openai-completions"
    path: str = "/chat/completions"


def default_state_path(profile: str, home: Path | None = None) -> Path:
    return state_dir(home) / "solo" / f"{profile}.backend.json"


def default_activity_path(profile: str, home: Path | None = None) -> Path:
    return state_dir(home) / "solo" / f"{profile}.activity"


def default_log_path(profile: str, home: Path | None = None) -> Path:
    return state_dir(home) / "solo" / f"{profile}.backend.log"


def _pid_start_ticks(pid: int | None) -> int | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _group_alive(pgid: int | None) -> bool:
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _prepare_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _prepare_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_json(url: str, *, timeout: float = HTTP_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise SoloBackendError(f"cannot read {url}: {exc}") from exc


def _endpoint_reachable(endpoint: str, *, timeout: float = 1.0) -> bool:
    try:
        payload = _fetch_json(endpoint.rstrip("/") + "/models", timeout=timeout)
    except SoloBackendError:
        return False
    return isinstance(payload, dict)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _sanitize_model_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("._-")
    if not text:
        raise SoloBackendError("cannot derive a Letta model id from the server model")
    return text


def _model_id_from_path(raw: str) -> str:
    name = Path(raw).stem or Path(raw).name or raw
    return _sanitize_model_id(name)


def _model_inputs(props: Mapping[str, Any], model: Mapping[str, Any]) -> tuple[str, ...]:
    modalities = props.get("modalities")
    vision = isinstance(modalities, Mapping) and modalities.get("vision") is True
    architecture = model.get("architecture")
    declared = (
        architecture.get("input_modalities")
        if isinstance(architecture, Mapping)
        else None
    )
    if vision or (isinstance(declared, list) and "image" in declared):
        return ("text", "image")
    return ("text",)


def discover_solo_route(endpoint: str, *, timeout: float = HTTP_TIMEOUT) -> SoloRoute:
    """Read the live model id, context window, and output cap from llama.cpp."""
    models_payload = _fetch_json(endpoint.rstrip("/") + "/models", timeout=timeout)
    entries = models_payload.get("data") if isinstance(models_payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SoloBackendError("solo backend returned no models")
    model = entries[0]
    if not isinstance(model, Mapping):
        raise SoloBackendError("solo backend returned an invalid model entry")
    upstream_model = model.get("id")
    if not isinstance(upstream_model, str) or not upstream_model.strip():
        raise SoloBackendError("solo backend model entry has no id")
    upstream_model = upstream_model.strip()

    parsed = urlsplit(endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    props: Mapping[str, Any] = {}
    with contextlib.suppress(SoloBackendError):
        loaded = _fetch_json(origin + "/props", timeout=timeout)
        if isinstance(loaded, Mapping):
            props = loaded

    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, Mapping) else {}
    meta = model.get("meta")
    meta = meta if isinstance(meta, Mapping) else {}
    context_window = (
        _positive_int(settings.get("n_ctx"))
        or _positive_int(props.get("n_ctx"))
        or _positive_int(meta.get("n_ctx"))
    )
    if context_window is None:
        raise SoloBackendError(
            "solo backend did not report context length via /props or /v1/models"
        )

    params = settings.get("params")
    params = params if isinstance(params, Mapping) else {}
    predicted = _positive_int(params.get("n_predict"))
    max_tokens = min(predicted, context_window) if predicted else context_window

    model_path = props.get("model_path") or props.get("model_alias") or upstream_model
    if not isinstance(model_path, str) or not model_path.strip():
        model_path = upstream_model
    return SoloRoute(
        model_id=_model_id_from_path(model_path.strip()),
        upstream_model=upstream_model,
        context_window=context_window,
        max_tokens=max_tokens,
        inputs=_model_inputs(props, model),
        endpoint=endpoint,
    )


def _route_to_mapping(route: SoloRoute) -> dict[str, Any]:
    payload = asdict(route)
    payload["inputs"] = list(route.inputs)
    return payload


def _route_from_mapping(value: Any) -> SoloRoute | None:
    if not isinstance(value, Mapping):
        return None
    try:
        inputs = value.get("inputs", ["text"])
        if not isinstance(inputs, list) or not all(
            isinstance(item, str) for item in inputs
        ):
            return None
        route = SoloRoute(
            model_id=str(value["model_id"]),
            upstream_model=str(value["upstream_model"]),
            context_window=int(value["context_window"]),
            max_tokens=int(value["max_tokens"]),
            inputs=tuple(inputs),
            endpoint=str(value["endpoint"]),
            api=str(value.get("api") or "openai-completions"),
            path=str(value.get("path") or "/chat/completions"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        not route.model_id
        or "/" in route.model_id
        or not route.upstream_model
        or route.context_window <= 0
        or route.max_tokens <= 0
        or not route.endpoint
    ):
        return None
    return route


class SoloBackendManager:
    """Own the local model process used only while solo mode needs it."""

    def __init__(
        self,
        config: SoloBackend | None,
        *,
        profile: str,
        state_path: Path | None = None,
        activity_path: Path | None = None,
        log_path: Path | None = None,
        busy_check: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.state_path = state_path or default_state_path(profile)
        self.activity_path = activity_path or default_activity_path(profile)
        self.log_path = log_path or default_log_path(profile)
        self.busy_check = busy_check or (lambda: False)
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None
        self._leader_pid: int | None = None
        self._leader_start_ticks: int | None = None
        self._running = False
        self._last_route: SoloRoute | None = None
        self._started_at = ""
        self._last_activity = 0.0
        self._idle_task: asyncio.Task[None] | None = None
        self._load_state()

    @property
    def configured(self) -> bool:
        return bool(self.config and self.config.configured)

    @property
    def endpoint(self) -> str:
        return self.config.endpoint if self.config is not None else ""

    def _load_state(self) -> None:
        state = _read_json(self.state_path)
        self._last_route = _route_from_mapping(state.get("route"))
        pgid = state.get("pgid")
        leader_pid = state.get("leader_pid")
        leader_ticks = state.get("leader_start_ticks")
        adopted = (
            state.get("running") is True
            and isinstance(pgid, int)
            and not isinstance(pgid, bool)
            and isinstance(leader_pid, int)
            and not isinstance(leader_pid, bool)
            and isinstance(leader_ticks, int)
            and not isinstance(leader_ticks, bool)
            and _group_alive(pgid)
            and _pid_start_ticks(leader_pid) == leader_ticks
        )
        if adopted:
            self._pgid = pgid
            self._leader_pid = leader_pid
            self._leader_start_ticks = leader_ticks
            self._running = True
            started_at = state.get("started_at")
            self._started_at = started_at if isinstance(started_at, str) else ""
            self._activate_idle_monitor()
        elif state.get("running") is True:
            self._running = False
            self._write_state()

    def _write_state(self) -> None:
        payload = {
            "schema": STATE_SCHEMA,
            "profile": self.profile,
            "running": self._running,
            "pgid": self._pgid,
            "leader_pid": self._leader_pid,
            "leader_start_ticks": self._leader_start_ticks,
            "endpoint": self.endpoint,
            "started_at": self._started_at,
            "log_path": str(self.log_path),
            "route": _route_to_mapping(self._last_route)
            if self._last_route is not None
            else None,
        }
        _atomic_write_json(self.state_path, payload)

    def _touch_activity(self) -> None:
        _prepare_private_dir(self.activity_path.parent)
        now = time.time()
        self.activity_path.touch(exist_ok=True)
        with contextlib.suppress(OSError):
            os.utime(self.activity_path, (now, now))
            os.chmod(self.activity_path, 0o600)
        self._last_activity = now

    def mark_activity(self) -> None:
        self._touch_activity()

    def _activity_epoch(self) -> float:
        try:
            value = self.activity_path.stat().st_mtime
        except OSError:
            value = 0.0
        return max(value, self._last_activity)

    def _activate_idle_monitor(self) -> None:
        timeout = self.idle_timeout
        if timeout <= 0 or not self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._idle_task
        if task is None or task.done():
            self._idle_task = loop.create_task(
                self._idle_monitor(), name=f"solo-backend-idle-{self.profile}"
            )

    @property
    def idle_timeout(self) -> float:
        if self.config is None:
            return DEFAULT_IDLE_TIMEOUT
        return max(0.0, float(self.config.idle_timeout))

    @property
    def startup_timeout(self) -> float:
        if self.config is None:
            return DEFAULT_STARTUP_TIMEOUT
        return max(0.01, float(self.config.startup_timeout))

    @property
    def shutdown_timeout(self) -> float:
        if self.config is None:
            return DEFAULT_SHUTDOWN_TIMEOUT
        return max(0.01, float(self.config.shutdown_timeout))

    async def _idle_monitor(self) -> None:
        try:
            while self._running:
                timeout = self.idle_timeout
                if timeout <= 0:
                    return
                while self.busy_check():
                    self._touch_activity()
                    await asyncio.sleep(POLL_INTERVAL)
                delay = self._activity_epoch() + timeout - time.time()
                if delay > 0:
                    await asyncio.sleep(min(delay, 30.0))
                    continue
                async with self._lock:
                    if not self._running:
                        return
                    if self.busy_check():
                        self._touch_activity()
                        continue
                    if self._activity_epoch() + timeout > time.time():
                        continue
                    pgid = self._pgid
                    await self._stop_locked()
                    LOGGER.info(
                        "solo backend stopped after idle timeout profile=%s pgid=%s timeout=%ss",
                        self.profile,
                        pgid,
                        timeout,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("solo backend idle monitor failed profile=%s", self.profile)
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def _cancel_idle_monitor(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def status(self) -> dict[str, Any]:
        route = self._last_route
        return {
            "configured": self.configured,
            "running": self._running,
            "pid": self._leader_pid,
            "pgid": self._pgid,
            "endpoint": self.endpoint,
            "started_at": self._started_at,
            "log_path": str(self.log_path),
            "state_path": str(self.state_path),
            "activity_path": str(self.activity_path),
            "idle_timeout": self.idle_timeout,
            "model_id": route.model_id if route is not None else "",
            "context_window": route.context_window if route is not None else None,
        }

    def last_route(self) -> SoloRoute | None:
        return self._last_route

    def _reserve_model_id(self, route: SoloRoute, reserved: set[str]) -> SoloRoute:
        if route.model_id not in reserved:
            return route
        base = f"{route.model_id}-solo"
        candidate = base
        counter = 2
        while candidate in reserved:
            candidate = f"{base}-{counter}"
            counter += 1
        return SoloRoute(**{**asdict(route), "model_id": candidate})

    async def start(self, *, reserved_model_ids: set[str] | None = None) -> SoloRoute:
        if not self.configured or self.config is None:
            raise SoloBackendError("solo startup_script is not configured")
        async with self._lock:
            if self._running and _group_alive(self._pgid):
                try:
                    route = await asyncio.to_thread(
                        discover_solo_route, self.endpoint
                    )
                except SoloBackendError as exc:
                    raise SoloBackendError(
                        "the akid-managed solo backend is running but not ready; "
                        "stop it before retrying"
                    ) from exc
                self._last_route = self._reserve_model_id(
                    route, reserved_model_ids or set()
                )
                self._write_state()
                self.mark_activity()
                return self._last_route
            if await asyncio.to_thread(_endpoint_reachable, self.endpoint):
                raise SoloBackendError(
                    f"solo endpoint {self.endpoint} is already in use by a process "
                    "not started by akid; stop it first"
                )
            if self._pgid is not None:
                self._clear_process_state()

            try:
                argv = split_startup_script(self.config.startup_script)
            except ValueError as exc:
                raise SoloBackendError(
                    f"solo startup_script cannot be parsed: {exc}"
                ) from exc
            if not argv:
                raise SoloBackendError("solo startup_script is empty")
            first = Path(argv[0]).expanduser()
            if first.is_file():
                argv[0] = str(first)
            cwd = str(first.parent) if "/" in argv[0] or first.is_absolute() else None

            _prepare_private_dir(self.log_path.parent)
            with self.log_path.open("ab", buffering=0) as log:
                os.chmod(self.log_path, 0o600)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=cwd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise SoloBackendError(
                        f"cannot start solo backend: {exc}"
                    ) from exc

            self._proc = proc
            self._pgid = proc.pid
            self._leader_pid = proc.pid
            self._leader_start_ticks = _pid_start_ticks(proc.pid)
            self._running = True
            self._started_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            self._touch_activity()
            deadline = asyncio.get_running_loop().time() + self.startup_timeout
            try:
                while True:
                    if proc.returncode is not None:
                        raise SoloBackendError(
                            f"solo backend exited with code {proc.returncode}; "
                            f"log={self.log_path}"
                        )
                    try:
                        route = await asyncio.to_thread(
                            discover_solo_route, self.endpoint
                        )
                    except SoloBackendError:
                        route = None
                    if route is not None:
                        self._last_route = self._reserve_model_id(
                            route, reserved_model_ids or set()
                        )
                        self._write_state()
                        self.mark_activity()
                        self._activate_idle_monitor()
                        return self._last_route
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SoloBackendError(
                            f"solo backend startup timed out after "
                            f"{self.startup_timeout:g}s; log={self.log_path}"
                        )
                    await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                await self._stop_group(graceful=False)
                self._clear_process_state()
                self._write_state()
                raise
            except Exception:
                await self._stop_group(graceful=False)
                self._clear_process_state()
                self._write_state()
                raise

    def _clear_process_state(self) -> None:
        self._proc = None
        self._pgid = None
        self._leader_pid = None
        self._leader_start_ticks = None
        self._running = False
        self._started_at = ""

    async def _stop_group(self, *, graceful: bool) -> str:
        pgid = self._pgid
        if not _group_alive(pgid):
            return "SIGINT"
        assert pgid is not None
        wait_task: asyncio.Task[Any] | None = None
        if self._proc is not None and self._proc.returncode is None:
            wait_task = asyncio.create_task(self._proc.wait())
        first = signal.SIGINT if graceful else signal.SIGKILL
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, first)
        if not graceful:
            return "SIGKILL"
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout
        while asyncio.get_running_loop().time() < deadline:
            if wait_task is not None and wait_task.done():
                with contextlib.suppress(Exception):
                    wait_task.result()
            if not _group_alive(pgid):
                return "SIGINT"
            await asyncio.sleep(0.1)
        if _group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            return "SIGKILL"
        return "SIGINT"

    async def _stop_locked(self) -> dict[str, Any]:
        if not self._running:
            self._clear_process_state()
            return {"status": "stopped", "signal": None}
        pgid = self._pgid
        pid = self._leader_pid
        used_signal = await self._stop_group(graceful=True)
        if _group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        if self._proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
        self._clear_process_state()
        self._write_state()
        with contextlib.suppress(FileNotFoundError):
            self.activity_path.unlink()
        return {"status": "stopped", "pid": pid, "pgid": pgid, "signal": used_signal}

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            result = await self._stop_locked()
        await self._cancel_idle_monitor()
        return result

    async def close(self) -> None:
        if self._running:
            with contextlib.suppress(Exception):
                await self.stop()
        await self._cancel_idle_monitor()
