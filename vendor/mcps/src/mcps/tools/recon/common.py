"""Single subprocess entry for recon CLIs. No shell=True."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import bins
from .config import ReconConfig, get_config
from .errors import ReconError

log = logging.getLogger("mcps.recon")

CAP_NET_RAW = 13  # linux/capability.h

_SECRET_FLAGS = frozenset(
    {
        "-H",
        "--header",
        "--headers",
        "-c",
        "--cookie",
        "--cookies",
        "--api-key",
        "--apikey",
        "--token",
        "--proxy",
        "-p",
    }
)
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTHORIZATION|CREDENTIAL)", re.I)
_SECRET_TEXT_RE = re.compile(
    r'(?i)(api[_-]?key|secret|token|password|authorization)(["\s:=]+)(\S+)'
)


@dataclass
class RunResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_ms: int
    argv_redacted: list[str]
    privileged: bool
    privilege_note: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def has_net_raw() -> bool:
    if os.geteuid() == 0:
        return True
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    cap = int(line.split()[1], 16)
                    return bool(cap & (1 << CAP_NET_RAW))
    except (OSError, ValueError, IndexError):
        return False
    return False


def redact_argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("<redacted>")
            redact_next = False
            continue
        if arg in _SECRET_FLAGS:
            out.append(arg)
            redact_next = True
            continue
        if "=" in arg and _SECRET_ENV_RE.search(arg.split("=", 1)[0]):
            key, _sep, _val = arg.partition("=")
            out.append(f"{key}=<redacted>")
            continue
        out.append(_SECRET_TEXT_RE.sub(r"\1\2<redacted>", arg))
    if redact_next:
        out.append("<redacted>")
    return out


def redact_text(text: str) -> str:
    return _SECRET_TEXT_RE.sub(r"\1\2<redacted>", text)


def truncate_bytes(data: bytes, limit: int) -> tuple[bytes, bool]:
    if limit < 0:
        raise ReconError("max_output_bytes must be >= 0")
    if len(data) <= limit:
        return data, False
    return data[:limit], True


def truncate_for_model(
    text: str, *, max_lines: int | None = None, max_chars: int | None = None
) -> tuple[str, bool]:
    cfg = get_config()
    if max_lines is None:
        max_lines = cfg.limits.max_lines_to_model
    truncated = False
    lines = text.splitlines(keepends=True)
    if max_lines >= 0 and len(lines) > max_lines:
        text = "".join(lines[:max_lines])
        truncated = True
    if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, truncated


def assert_wordlist_path(path: str, *, cfg: ReconConfig | None = None) -> Path:
    cfg = cfg or get_config()
    raw = Path(os.path.expanduser(path))
    try:
        resolved = raw.resolve()
    except OSError as exc:
        raise ReconError(f"cannot resolve wordlist path: {path}") from exc
    for prefix in cfg.wordlists.allowed_prefixes:
        base = Path(os.path.expanduser(prefix)).resolve()
        try:
            if resolved.is_relative_to(base):
                return resolved
        except ValueError:
            continue
    raise ReconError(
        f"wordlist path is not under an allowed prefix: {path}"
    )


def new_js_job_dir() -> Path:
    import uuid

    dest = js_workspace_dir(uuid.uuid4().hex)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    return dest


def assert_js_workspace_file(path: str, *, cfg: ReconConfig | None = None) -> Path:
    cfg = cfg or get_config()
    root = Path(cfg.limits.js_workspace).resolve()
    resolved = Path(os.path.expanduser(path)).resolve()
    if not resolved.is_file():
        raise ReconError(f"js file not found: {path}")
    if not resolved.is_relative_to(root):
        raise ReconError("js file is outside js_workspace")
    return resolved


def js_workspace_dir(job_id: str, *, cfg: ReconConfig | None = None) -> Path:
    cfg = cfg or get_config()
    root = Path(cfg.limits.js_workspace).resolve()
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise ReconError("invalid job_id for js workspace")
    dest = (root / job_id).resolve()
    if not dest.is_relative_to(root):
        raise ReconError("js workspace path escaped root")
    return dest


def build_env(extra: dict[str, str] | None = None, *, cfg: ReconConfig | None = None) -> dict[str, str]:
    cfg = cfg or get_config()
    out: dict[str, str] = {}
    allowed = set(cfg.env.passthrough)
    prefixes = cfg.env.passthrough_prefixes
    for key, value in os.environ.items():
        if key in allowed or any(key.startswith(p) for p in prefixes):
            out[key] = value
    if "PATH" not in out and "PATH" in os.environ:
        out["PATH"] = os.environ["PATH"]
    if extra:
        for key, value in extra.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ReconError("extra env keys and values must be strings")
            out[key] = value
    return out


def resolve_scan_type(requested: str) -> tuple[str, bool, str]:
    """Map syn|connect to an executable mode. Never SYN without a privilege path."""
    if requested not in {"syn", "connect"}:
        raise ReconError("scan_type must be 'syn' or 'connect'")
    cfg = get_config()
    if requested == "connect":
        return "connect", False, "connect"
    if not cfg.privilege.allow_syn_scan:
        if cfg.privilege.fallback_connect:
            return "connect", False, "syn_disabled_fallback_connect"
        raise ReconError("SYN scan is disabled in config")
    if has_net_raw():
        return "syn", True, "syn_privileged"
    if cfg.privilege.allow_sudo and shutil_which("sudo"):
        return "syn", True, "syn_via_sudo"
    if cfg.privilege.fallback_connect:
        return "connect", False, "unprivileged_fallback_connect"
    raise ReconError(
        "SYN scan requested but this process has no CAP_NET_RAW and sudo is unavailable"
    )


def elevate_if_needed(
    argv: list[str], *, need_raw_socket: bool, cfg: ReconConfig | None = None
) -> tuple[list[str], bool, str]:
    """Optionally prefix sudo -n -- for SYN-capable bins. Never sudo unlisted bins."""
    cfg = cfg or get_config()
    privileged = has_net_raw()
    if not need_raw_socket:
        return argv, privileged, "not_required"
    if not cfg.privilege.allow_syn_scan:
        return argv, privileged, "syn_disabled"
    if privileged:
        return argv, True, "already_privileged"
    if not cfg.privilege.allow_sudo:
        return argv, False, "sudo_disabled"
    logical = bins.logical_name_for_path(argv[0]) or Path(argv[0]).name
    if logical not in cfg.privilege.sudo_bins:
        return argv, False, f"bin_not_in_sudo_allowlist:{logical}"
    sudo = shutil_which("sudo")
    if not sudo:
        return argv, False, "sudo_not_found"
    if Path(argv[0]).name == "sudo":
        return argv, False, "already_sudo"
    return [sudo, "-n", "--", *argv], False, "sudo_n"


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def prepare_argv(argv: list[str], *, need_raw_socket: bool = False) -> tuple[list[str], bool, str]:
    if not isinstance(argv, list):
        raise ReconError("argv must be a list of strings, not a shell string")
    if not argv:
        raise ReconError("argv is empty")
    if not all(isinstance(x, str) for x in argv):
        raise ReconError("argv items must be strings")
    if any("\x00" in x for x in argv):
        raise ReconError("argv items must not contain NUL")
    resolved0 = bins.resolve_allowed(argv[0])
    prepared = [resolved0, *argv[1:]]
    return elevate_if_needed(prepared, need_raw_socket=need_raw_socket)


def audit(
    *,
    tool: str,
    argv_redacted: list[str],
    job_id: str | None = None,
    operator: str = "mcp",
) -> None:
    log.info(
        "recon exec ts=%s tool=%s operator=%s job_id=%s argv=%s",
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool,
        operator,
        job_id or "-",
        argv_redacted,
    )


def run_recon(
    argv: list[str],
    *,
    timeout_s: int,
    max_output_bytes: int | None = None,
    input_text: str | None = None,
    cwd: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    need_raw_socket: bool = False,
    tool: str = "run_recon",
    job_id: str | None = None,
    operator: str = "mcp",
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """Run a listed recon binary. argv is a list; shell is never used."""
    cfg = get_config()
    if timeout_s <= 0:
        raise ReconError("timeout_s must be positive")
    limit = cfg.limits.max_output_bytes if max_output_bytes is None else max_output_bytes
    if limit < 0:
        raise ReconError("max_output_bytes must be >= 0")
    if cwd is not None:
        cwd_path = Path(cwd).resolve()
        if not cwd_path.is_dir():
            raise ReconError(f"cwd is not a directory: {cwd}")
        cwd = str(cwd_path)
    argv_exec, privileged, privilege_note = prepare_argv(
        argv, need_raw_socket=need_raw_socket
    )
    redacted = redact_argv(argv_exec)
    audit(tool=tool, argv_redacted=redacted, job_id=job_id, operator=operator)
    env = build_env(extra_env, cfg=cfg)
    stdin_data = None if input_text is None else input_text.encode("utf-8", errors="replace")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False
            argv_exec,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
            shell=False,
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return RunResult(
            ok=False,
            exit_code=None,
            stdout="",
            stderr=f"failed to spawn: {exc}",
            truncated=False,
            timed_out=False,
            duration_ms=duration_ms,
            argv_redacted=redacted,
            privileged=privileged,
            privilege_note=privilege_note,
        )
    stdout_buf = _LimitedBuffer(limit)
    stderr_buf = _LimitedBuffer(limit)
    overflow = threading.Event()

    def _reader(stream: object, buf: _LimitedBuffer) -> None:
        try:
            while True:
                chunk = stream.read(65536)  # type: ignore[attr-defined]
                if not chunk:
                    break
                buf.write(chunk)
                if buf.truncated:
                    overflow.set()
                    break
        except OSError:
            pass
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, stdout_buf), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, stderr_buf), daemon=True),
    ]
    for t in threads:
        t.start()
    if proc.stdin is not None:
        try:
            if stdin_data:
                proc.stdin.write(stdin_data)
        except OSError:
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass

    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_group(proc)
            break
        if overflow.is_set():
            _kill_group(proc)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_group(proc)
            break
        time.sleep(0.02)
    for t in threads:
        t.join(timeout=2)
    duration_ms = int((time.monotonic() - started) * 1000)
    code = proc.returncode
    stdout = bytes(stdout_buf.buf).decode("utf-8", errors="replace")
    stderr = bytes(stderr_buf.buf).decode("utf-8", errors="replace")
    truncated = stdout_buf.truncated or stderr_buf.truncated
    ok = (not timed_out) and (not cancelled) and (code == 0)
    if cancelled and not stderr:
        stderr = "cancelled"
    return RunResult(
        ok=ok,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
        timed_out=timed_out,
        duration_ms=duration_ms,
        argv_redacted=redacted,
        privileged=privileged,
        privilege_note=privilege_note,
    )


async def arun_recon(*args: object, **kwargs: object) -> RunResult:
    return await asyncio.to_thread(run_recon, *args, **kwargs)  # type: ignore[arg-type]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    if proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


class _LimitedBuffer:
    def __init__(self, limit: int) -> None:
        self.buf = bytearray()
        self.limit = limit
        self.truncated = False

    def write(self, data: bytes) -> None:
        if self.truncated:
            return
        room = self.limit - len(self.buf)
        if len(data) <= room:
            self.buf.extend(data)
            return
        self.buf.extend(data[:room])
        self.truncated = True
