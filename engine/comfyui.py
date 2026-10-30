"""Managed local ComfyUI lifecycle and workflow execution for Haru."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import secrets
import shlex
import signal
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping

from engine.aki_config import ComfyUI, state_dir
from engine.tokifactor import detect_image_mime


LOGGER = logging.getLogger("akid.comfyui")

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / "misc" / "Haru.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:8188"
DEFAULT_OUTPUT_DIR = "/tmp/aki/comfyui"
DEFAULT_STARTUP_TIMEOUT = 180.0
DEFAULT_SHUTDOWN_TIMEOUT = 15.0
DEFAULT_IDLE_TIMEOUT = 1800.0
GENERATION_TIMEOUT = 300.0
POLL_INTERVAL = 0.5
MAX_HTTP_JSON_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
MAX_DIMENSION = 4096
MIN_DIMENSION = 64
MAX_STEPS = 200
MAX_CFG = 100.0
NOT_CONFIGURED_MESSAGE = "用户未正确配置ComfyUI"


class ComfyUIError(RuntimeError):
    """A model-facing ComfyUI failure."""


class ComfyUINotConfigured(ComfyUIError):
    """The profile has no usable ComfyUI startup script."""

    def __init__(self) -> None:
        super().__init__(NOT_CONFIGURED_MESSAGE)


def _prepare_private_dir(path: Path) -> None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ComfyUIError("ComfyUI output directory must not be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ComfyUIError("ComfyUI output path is not a directory")
        if info.st_uid != os.getuid():
            raise ComfyUIError(
                "ComfyUI output directory is not owned by the current user"
            )
        os.chmod(path, 0o700)
    except ComfyUIError:
        raise
    except OSError as exc:
        raise ComfyUIError(f"cannot prepare ComfyUI output directory: {exc}") from exc


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


def _read_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_start_ticks(pid: int) -> int | None:
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


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _node(nodes: Mapping[str, Any], node_id: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(node_id, str) or node_id not in nodes:
        raise ComfyUIError(f"workflow {field} node is missing")
    value = nodes[node_id]
    if not isinstance(value, dict):
        raise ComfyUIError(f"workflow {field} node is invalid")
    return value


def _linked_node(
    nodes: Mapping[str, Any], link: Any, field: str
) -> tuple[str, Mapping[str, Any]]:
    if (
        not isinstance(link, list)
        or len(link) < 1
        or not isinstance(link[0], str)
    ):
        raise ComfyUIError(f"workflow {field} link is invalid")
    node_id = link[0]
    return node_id, _node(nodes, node_id, field)


def _optional_number(
    value: Any,
    field: str,
    *,
    integer: bool,
    minimum: float,
    maximum: float,
) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be {'an integer' if integer else 'a number'}")
    if integer:
        if not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
    elif not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = int(value) if integer else float(value)
    if number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    if integer and field in {"width", "height"} and number % 8 != 0:
        raise ValueError(f"{field} must be a multiple of 8")
    return number


def render_workflow(
    workflow_path: Path, arguments: Mapping[str, Any]
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    """Load Haru.json and apply the supported first-pass parameters."""

    positive = arguments.get("positive")
    if not isinstance(positive, str) or not positive.strip():
        raise ValueError("positive must be a non-empty string")
    positive = positive.strip()
    if len(positive) > MAX_PROMPT_CHARS:
        raise ValueError(f"positive exceeds {MAX_PROMPT_CHARS} characters")
    negative = arguments.get("negative")
    if negative is not None:
        if not isinstance(negative, str):
            raise ValueError("negative must be a string")
        if len(negative) > MAX_PROMPT_CHARS:
            raise ValueError(f"negative exceeds {MAX_PROMPT_CHARS} characters")

    seed = arguments.get("seed")
    if seed is None:
        seed = secrets.randbits(63)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**64 - 1:
        raise ValueError("seed must be an integer from 0 to 18446744073709551615")
    cfg = _optional_number(
        arguments.get("cfg"),
        "cfg",
        integer=False,
        minimum=0.0,
        maximum=MAX_CFG,
    )
    steps = _optional_number(
        arguments.get("steps"),
        "steps",
        integer=True,
        minimum=1,
        maximum=MAX_STEPS,
    )
    width = _optional_number(
        arguments.get("width"),
        "width",
        integer=True,
        minimum=MIN_DIMENSION,
        maximum=MAX_DIMENSION,
    )
    height = _optional_number(
        arguments.get("height"),
        "height",
        integer=True,
        minimum=MIN_DIMENSION,
        maximum=MAX_DIMENSION,
    )

    try:
        if workflow_path.stat().st_size > MAX_WORKFLOW_BYTES:
            raise ComfyUIError("ComfyUI workflow is too large")
        loaded = json.loads(workflow_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ComfyUIError(f"cannot read ComfyUI workflow: {exc}") from exc
    except ValueError as exc:
        raise ComfyUIError("ComfyUI workflow is not valid JSON") from exc
    if not isinstance(loaded, dict) or not loaded:
        raise ComfyUIError("ComfyUI workflow must be a non-empty JSON object")
    workflow = copy.deepcopy(loaded)
    nodes = _mapping(workflow, "workflow")
    samplers = [
        (node_id, node)
        for node_id, node in nodes.items()
        if isinstance(node, dict) and node.get("class_type") == "KSampler"
    ]
    if len(samplers) != 1:
        raise ComfyUIError("ComfyUI workflow must contain exactly one KSampler")
    _sampler_id, sampler = samplers[0]
    sampler_inputs = _mapping(sampler.get("inputs"), "KSampler inputs")

    _positive_id, positive_node = _linked_node(
        nodes, sampler_inputs.get("positive"), "positive"
    )
    positive_inputs = _mapping(positive_node.get("inputs"), "positive inputs")
    positive_inputs["text"] = positive

    if negative is not None:
        _negative_id, negative_node = _linked_node(
            nodes, sampler_inputs.get("negative"), "negative"
        )
        negative_inputs = _mapping(negative_node.get("inputs"), "negative inputs")
        negative_inputs["text"] = negative

    seed_link = sampler_inputs.get("seed")
    if isinstance(seed_link, list):
        _seed_id, seed_node = _linked_node(nodes, seed_link, "seed")
        seed_inputs = _mapping(seed_node.get("inputs"), "seed inputs")
        seed_inputs["seed"] = seed
    else:
        sampler_inputs["seed"] = seed
    if cfg is not None:
        sampler_inputs["cfg"] = cfg
    if steps is not None:
        sampler_inputs["steps"] = steps

    if width is not None or height is not None:
        _latent_id, latent_node = _linked_node(
            nodes, sampler_inputs.get("latent_image"), "latent_image"
        )
        latent_inputs = _mapping(latent_node.get("inputs"), "latent inputs")
        if width is not None:
            latent_inputs["width"] = width
        if height is not None:
            latent_inputs["height"] = height

    applied = {
        "positive": positive,
        "negative": negative,
        "seed": seed,
        "cfg": cfg,
        "steps": steps,
        "width": width,
        "height": height,
    }
    return workflow, seed, applied


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))


def _error_detail(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("details")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        node_errors = payload.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            return f"node errors: {', '.join(sorted(node_errors))}"
    return fallback


def _request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    payload: Mapping[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json", "User-Agent": "Aki-comfyui/1.0"}
    method = "GET"
    if payload is not None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(
        url, data=body, method=method, headers=headers
    )
    try:
        with opener.open(request, timeout=max(0.1, timeout)) as response:
            raw = response.read(MAX_HTTP_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(256 * 1024)
        detail: object = None
        with contextlib.suppress(UnicodeDecodeError, ValueError):
            detail = json.loads(raw.decode("utf-8"))
        raise ComfyUIError(
            f"ComfyUI HTTP {exc.code}: {_error_detail(detail, 'request failed')}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ComfyUIError(f"ComfyUI request failed: {exc.reason}") from exc
    except OSError as exc:
        raise ComfyUIError(f"ComfyUI request failed: {exc}") from exc
    if len(raw) > MAX_HTTP_JSON_BYTES:
        raise ComfyUIError("ComfyUI response is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ComfyUIError("ComfyUI returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an invalid response")
    return value


def _probe_health(endpoint: str, timeout: float = 3.0) -> bool:
    try:
        _request_json(
            _opener(),
            endpoint.rstrip("/") + "/system_stats",
            timeout=timeout,
        )
        return True
    except ComfyUIError:
        return False


def _prepare_output_dir(path: Path) -> None:
    _prepare_private_dir(path)


def _write_image(directory: Path, data: bytes, mime: str) -> Path:
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[mime]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"comfyui-{stamp}-{uuid.uuid4().hex[:10]}{extension}"
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return target


def _download_image(
    opener: urllib.request.OpenerDirector,
    endpoint: str,
    output_dir: Path,
    image: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    filename = image.get("filename")
    subfolder = image.get("subfolder", "")
    image_type = image.get("type", "output")
    if not isinstance(filename, str) or not filename:
        raise ComfyUIError("ComfyUI output image has no filename")
    if not isinstance(subfolder, str) or not isinstance(image_type, str):
        raise ComfyUIError("ComfyUI output image metadata is invalid")
    query = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": image_type}
    )
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/view?" + query,
        headers={"Accept": "image/*", "User-Agent": "Aki-comfyui/1.0"},
    )
    try:
        with opener.open(request, timeout=max(0.1, timeout)) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                raise ComfyUIError("ComfyUI image is too large")
            data = response.read(MAX_IMAGE_BYTES + 1)
    except ComfyUIError:
        raise
    except urllib.error.HTTPError as exc:
        raise ComfyUIError(f"ComfyUI image download failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ComfyUIError(f"ComfyUI image download failed: {exc.reason}") from exc
    except OSError as exc:
        raise ComfyUIError(f"ComfyUI image download failed: {exc}") from exc
    if not data:
        raise ComfyUIError("ComfyUI returned an empty image")
    if len(data) > MAX_IMAGE_BYTES:
        raise ComfyUIError("ComfyUI image is too large")
    mime = detect_image_mime(data)
    if mime is None:
        raise ComfyUIError("ComfyUI image has an unsupported format")
    _prepare_output_dir(output_dir)
    path = _write_image(output_dir, data, mime)
    return {"path": str(path), "bytes": len(data), "mime": mime}


def _execute_workflow(
    *,
    endpoint: str,
    output_dir: Path,
    workflow: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    opener = _opener()
    started = time.monotonic()
    payload = _request_json(
        opener,
        endpoint.rstrip("/") + "/prompt",
        payload={"prompt": workflow, "client_id": uuid.uuid4().hex},
        timeout=min(30.0, timeout),
    )
    prompt_id = payload.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ComfyUIError(
            f"ComfyUI rejected the workflow: {_error_detail(payload, 'no prompt_id')}"
        )

    history: dict[str, Any] | None = None
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ComfyUIError(f"ComfyUI generation timed out after {timeout:g}s")
        result = _request_json(
            opener,
            endpoint.rstrip("/") + "/history/" + urllib.parse.quote(prompt_id),
            timeout=min(10.0, remaining),
        )
        item = result.get(prompt_id)
        if isinstance(item, dict):
            status = item.get("status")
            if isinstance(status, dict) and status.get("completed") is False:
                item = None
            elif isinstance(status, dict) and status.get("status_str") == "error":
                messages = status.get("messages")
                detail = json.dumps(messages, ensure_ascii=False)[:1000] if messages else ""
                raise ComfyUIError(f"ComfyUI generation failed: {detail or 'worker error'}")
            if item is not None:
                history = item
                break
        if time.monotonic() - started >= timeout:
            raise ComfyUIError(f"ComfyUI generation timed out after {timeout:g}s")
        time.sleep(POLL_INTERVAL)

    outputs = history.get("outputs") if isinstance(history, dict) else None
    images: list[Mapping[str, Any]] = []
    if isinstance(outputs, dict):
        for value in outputs.values():
            if not isinstance(value, dict):
                continue
            for image in value.get("images") or []:
                if (
                    isinstance(image, dict)
                    and image.get("type", "output") == "output"
                ):
                    images.append(image)
    if not images:
        raise ComfyUIError("ComfyUI completed without a saved output image")
    remaining = max(0.1, timeout - (time.monotonic() - started))
    saved = _download_image(opener, endpoint, output_dir, images[0], remaining)
    return {"prompt_id": prompt_id, **saved, "image_count": len(images)}


class ComfyUIManager:
    """Own the managed ComfyUI process and execute Haru's workflow."""

    def __init__(
        self,
        config: ComfyUI | None,
        *,
        profile: str,
        state_path: Path | None = None,
        workflow_path: Path | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.state_path = state_path or state_dir() / "comfyui" / f"{profile}.json"
        self.workflow_path = workflow_path or WORKFLOW_PATH
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._pgid: int | None = None
        self._leader_pid: int | None = None
        self._leader_start_ticks: int | None = None
        self._state_endpoint: str | None = None
        self._log_path: Path | None = None
        self._started_at: str | None = None
        self._idle_timeout = (
            max(0.0, float(config.idle_timeout))
            if config is not None
            else DEFAULT_IDLE_TIMEOUT
        )
        self._last_activity: float | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._load_state()
        if _group_alive(self._pgid):
            self._activate_idle_monitor()

    @property
    def endpoint(self) -> str:
        return self._state_endpoint or (
            self.config.endpoint if self.config is not None else DEFAULT_ENDPOINT
        )

    @property
    def output_dir(self) -> Path:
        value = (
            self.config.output_dir if self.config is not None else DEFAULT_OUTPUT_DIR
        )
        return Path(value).expanduser()

    @property
    def startup_script(self) -> str:
        return self.config.startup_script.strip() if self.config is not None else ""

    @property
    def require_approval(self) -> bool:
        return bool(self.config and self.config.require_approval)

    def _load_state(self) -> None:
        state = _read_state(self.state_path)
        pgid = state.get("pgid")
        leader_pid = state.get("leader_pid")
        leader_ticks = state.get("leader_start_ticks")
        if (
            isinstance(pgid, bool)
            or not isinstance(pgid, int)
            or isinstance(leader_pid, bool)
            or not isinstance(leader_pid, int)
            or isinstance(leader_ticks, bool)
            or not isinstance(leader_ticks, int)
            or not _group_alive(pgid)
            or _pid_start_ticks(leader_pid) != leader_ticks
        ):
            with contextlib.suppress(FileNotFoundError):
                self.state_path.unlink()
            return
        self._pgid = pgid
        self._leader_pid = leader_pid
        self._leader_start_ticks = leader_ticks
        endpoint = state.get("endpoint")
        log_path = state.get("log_path")
        started_at = state.get("started_at")
        if isinstance(endpoint, str) and endpoint:
            self._state_endpoint = endpoint
        if isinstance(log_path, str) and log_path:
            self._log_path = Path(log_path)
        if isinstance(started_at, str):
            self._started_at = started_at

    def _save_state(self, *, endpoint: str, log_path: Path) -> None:
        if self._pgid is None or self._leader_pid is None:
            raise ComfyUIError("ComfyUI process state is incomplete")
        ticks = _pid_start_ticks(self._leader_pid)
        if ticks is None:
            raise ComfyUIError("cannot verify the ComfyUI process")
        self._leader_start_ticks = ticks
        self._state_endpoint = endpoint
        self._log_path = log_path
        self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write_json(
            self.state_path,
            {
                "pgid": self._pgid,
                "leader_pid": self._leader_pid,
                "leader_start_ticks": ticks,
                "endpoint": endpoint,
                "log_path": str(log_path),
                "started_at": self._started_at,
                "startup_script": self.startup_script,
            },
        )

    def _clear_state(self) -> None:
        self._proc = None
        self._pgid = None
        self._leader_pid = None
        self._leader_start_ticks = None
        self._state_endpoint = None
        self._started_at = None
        self._log_path = None
        self._last_activity = None
        with contextlib.suppress(FileNotFoundError):
            self.state_path.unlink()

    def _activate_idle_monitor(self) -> None:
        if self._idle_timeout <= 0 or not _group_alive(self._pgid):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._last_activity is None:
            self._last_activity = loop.time()
        task = self._idle_task
        if task is None or task.done():
            self._idle_task = loop.create_task(
                self._idle_monitor(), name=f"comfyui-idle-{self.profile}"
            )

    def _mark_activity(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()
        self._activate_idle_monitor()

    async def _cancel_idle_monitor(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _idle_monitor(self) -> None:
        try:
            while True:
                timeout = self._idle_timeout
                if timeout <= 0 or not _group_alive(self._pgid):
                    return
                loop = asyncio.get_running_loop()
                last = self._last_activity
                if last is None:
                    last = loop.time()
                    self._last_activity = last
                delay = last + timeout - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                async with self._lock:
                    if not _group_alive(self._pgid):
                        return
                    last = self._last_activity
                    if last is None:
                        last = loop.time()
                        self._last_activity = last
                    if loop.time() - last < timeout:
                        continue
                    pgid = self._pgid
                    pid = self._leader_pid
                    await self._stop_locked()
                    LOGGER.info(
                        "ComfyUI stopped after idle timeout profile=%s pid=%s "
                        "pgid=%s timeout=%ss",
                        self.profile,
                        pid,
                        pgid,
                        timeout,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("ComfyUI idle monitor failed profile=%s", self.profile)
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def _healthy(self) -> bool:
        return await asyncio.to_thread(_probe_health, self.endpoint)

    def status(self) -> dict[str, Any]:
        running = _group_alive(self._pgid)
        return {
            "configured": bool(self.config and self.config.configured),
            "startup_script": self.startup_script,
            "endpoint": self.endpoint,
            "output_dir": str(self.output_dir),
            "workflow": str(self.workflow_path),
            "require_approval": self.require_approval,
            "running": running,
            "pid": self._leader_pid,
            "pgid": self._pgid,
            "started_at": self._started_at,
            "log_path": str(self._log_path) if self._log_path else None,
            "state_path": str(self.state_path),
            "idle_timeout": self._idle_timeout,
        }

    async def start(self) -> dict[str, Any]:
        if not self.config or not self.config.configured:
            raise ComfyUINotConfigured()
        async with self._lock:
            if _group_alive(self._pgid):
                if await self._healthy():
                    self._mark_activity()
                    return {
                        "status": "already_running",
                        "pid": self._leader_pid,
                        "pgid": self._pgid,
                        "endpoint": self.endpoint,
                    }
                raise ComfyUIError(
                    "the ComfyUI process started by comfyui_startup is not responding; "
                    "run comfyui_end before starting it again"
                )
            if self._pgid is not None:
                self._clear_state()
            if await self._healthy():
                raise ComfyUIError(
                    "a ComfyUI endpoint is already running but was not started by "
                    "comfyui_startup"
                )

            command = shlex.split(self.config.startup_script)
            if not command:
                raise ComfyUINotConfigured()
            script_token = command[0]
            expanded = Path(script_token).expanduser()
            if expanded.is_file():
                command[0] = str(expanded)
            cwd: str | None = None
            if "/" in script_token or expanded.is_absolute():
                cwd = str(expanded.parent)

            _prepare_private_dir(self.output_dir)
            log_path = self.output_dir / "startup.log"
            with log_path.open("ab", buffering=0) as log:
                os.chmod(log_path, 0o600)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=cwd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise ComfyUIError(f"cannot start ComfyUI: {exc}") from exc

            self._proc = proc
            self._pgid = proc.pid
            self._leader_pid = proc.pid
            self._leader_start_ticks = _pid_start_ticks(proc.pid)
            self._state_endpoint = self.config.endpoint
            self._log_path = log_path
            deadline = asyncio.get_running_loop().time() + self.config.startup_timeout
            try:
                while True:
                    if await self._healthy():
                        self._save_state(endpoint=self.config.endpoint, log_path=log_path)
                        self._mark_activity()
                        return {
                            "status": "started",
                            "pid": self._leader_pid,
                            "pgid": self._pgid,
                            "endpoint": self.endpoint,
                            "log_path": str(log_path),
                        }
                    if not _group_alive(self._pgid):
                        code = proc.returncode
                        detail = f" with exit code {code}" if code is not None else ""
                        raise ComfyUIError(
                            f"ComfyUI startup script exited{detail} before the endpoint "
                            f"became ready; log={log_path}"
                        )
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ComfyUIError(
                            f"ComfyUI startup timed out after "
                            f"{self.config.startup_timeout:g}s; log={log_path}"
                        )
                    await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                await self._stop_group(graceful=False)
                self._clear_state()
                raise
            except Exception:
                await self._stop_group(graceful=False)
                self._clear_state()
                raise

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
        timeout = (
            self.config.shutdown_timeout
            if self.config is not None
            else DEFAULT_SHUTDOWN_TIMEOUT
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if wait_task is not None and wait_task.done():
                with contextlib.suppress(Exception):
                    wait_task.result()
            if not _group_alive(pgid):
                return "SIGINT"
            await asyncio.sleep(0.2)
        if _group_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                if wait_task is not None and wait_task.done():
                    with contextlib.suppress(Exception):
                        wait_task.result()
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.1)
            return "SIGKILL"
        return "SIGINT"

    async def _stop_locked(self) -> dict[str, Any]:
        if not _group_alive(self._pgid):
            self._clear_state()
            raise ComfyUIError(
                "ComfyUI is not running from comfyui_startup"
            )
        pgid = self._pgid
        pid = self._leader_pid
        used_signal = await self._stop_group(graceful=True)
        if _group_alive(pgid):
            raise ComfyUIError("ComfyUI process group did not stop")
        if self._proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
        self._clear_state()
        return {
            "status": "stopped",
            "pid": pid,
            "pgid": pgid,
            "signal": used_signal,
        }

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            result = await self._stop_locked()
        await self._cancel_idle_monitor()
        return result

    async def close(self) -> None:
        await self._cancel_idle_monitor()

    async def _require_running(self) -> None:
        if not _group_alive(self._pgid):
            self._clear_state()
            raise ComfyUIError(
                "ComfyUI is not running; call comfyui_startup first"
            )
        if not await self._healthy():
            raise ComfyUIError(
                "ComfyUI is not responding; run comfyui_end and comfyui_startup again"
            )

    async def generate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        workflow, seed, applied = await asyncio.to_thread(
            render_workflow, self.workflow_path, arguments
        )
        async with self._lock:
            self._mark_activity()
            try:
                await self._require_running()
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        _execute_workflow,
                        endpoint=self.endpoint,
                        output_dir=self.output_dir,
                        workflow=workflow,
                        timeout=GENERATION_TIMEOUT,
                    ),
                    timeout=GENERATION_TIMEOUT,
                )
            except asyncio.TimeoutError as exc:
                raise ComfyUIError(
                    f"ComfyUI generation timed out after {GENERATION_TIMEOUT:g}s"
                ) from exc
            finally:
                self._mark_activity()
        return {**result, "seed": seed, "parameters": applied}
