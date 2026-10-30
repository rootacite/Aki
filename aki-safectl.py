#!/usr/bin/env python3
"""Safely start, stop, restart, and inspect akid systemd user services.

The script never kills processes directly. Lifecycle operations are delegated
to systemd, proxies are started before gateways, readiness is verified through
the akid Unix socket, and a failed start only rolls back units started by the
current invocation.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from engine.aki_config import (
        AkiConfig,
        ConfigError,
        default_socket_path,
        discover_profiles,
        load_config,
        profile_config_path,
        state_dir,
        validate_profile_name,
    )
except ImportError as exc:
    print(f"error: cannot import the Aki runtime from {ROOT}: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc


GATEWAY_TEMPLATE = "akid@.service"
PROXY_TEMPLATE = "akid-proxy@.service"
UNIT_PATTERN = re.compile(
    r"^(akid(?:-proxy)?)@([a-z0-9][a-z0-9_-]{0,31})\.service$"
)
DEFAULT_WAIT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.25
SYSTEMCTL_GRACE_SECONDS = 30.0


class ControlError(RuntimeError):
    """A lifecycle operation cannot continue safely."""


@dataclass(frozen=True)
class UnitState:
    active: bool
    active_state: str
    sub_state: str
    main_pid: int
    file_state: str


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    config: AkiConfig
    socket_path: Path
    proxy_host: str
    proxy_port: int
    gateway_active: bool
    proxy_active: bool


def gateway_unit(profile: str) -> str:
    return f"akid@{profile}.service"


def proxy_unit(profile: str) -> str:
    return f"akid-proxy@{profile}.service"


def _info(message: str) -> None:
    print(f"[aki-safectl] {message}", flush=True)


def _warn(message: str) -> None:
    print(f"[aki-safectl] warning: {message}", file=sys.stderr, flush=True)


def _systemctl(
    *args: str, timeout: float = SYSTEMCTL_GRACE_SECONDS
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", "--no-pager", *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ControlError("systemctl is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ControlError(
            f"systemctl {' '.join(args)} timed out; systemd may still be working"
        ) from exc


def _systemctl_checked(
    args: tuple[str, ...], action: str, *, timeout: float = SYSTEMCTL_GRACE_SECONDS
) -> None:
    result = _systemctl(*args, timeout=timeout)
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "").strip()
    raise ControlError(
        f"{action} failed" + (f": {detail[-1000:]}" if detail else "")
    )


def _unit_state(unit: str) -> UnitState:
    result = _systemctl(
        "show",
        unit,
        "-p",
        "ActiveState,SubState,MainPID,UnitFileState",
        timeout=10.0,
    )
    if result.returncode != 0:
        return UnitState(False, "unknown", "", 0, "unknown")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    active_state = values.get("ActiveState", "unknown")
    try:
        main_pid = int(values.get("MainPID", "0") or "0")
    except ValueError:
        main_pid = 0
    return UnitState(
        active=active_state in {"active", "activating", "reloading"},
        active_state=active_state,
        sub_state=values.get("SubState", ""),
        main_pid=main_pid,
        file_state=values.get("UnitFileState", "unknown"),
    )


def _unit_active(unit: str) -> bool:
    return _unit_state(unit).active


def _ensure_user_systemd() -> None:
    if os.geteuid() == 0:
        raise ControlError(
            "refusing to manage per-user services as root; run this script as the Aki owner"
        )
    result = _systemctl("show-environment", timeout=10.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ControlError(
            "systemctl --user is unavailable"
            + (f": {detail[-500:]}" if detail else "")
        )


def _ensure_units_installed() -> None:
    missing = [
        template
        for template in (PROXY_TEMPLATE, GATEWAY_TEMPLATE)
        if _systemctl("cat", template, timeout=10.0).returncode != 0
    ]
    if missing:
        raise ControlError(
            f"missing systemd user units: {', '.join(missing)}; "
            "run python3 install-daemon.py"
        )


@contextlib.contextmanager
def _control_lock():
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root_stat = root.stat()
    except OSError as exc:
        raise ControlError(f"cannot inspect state directory {root}: {exc}") from exc
    if root_stat.st_uid != os.getuid():
        raise ControlError(f"state directory is owned by uid {root_stat.st_uid}: {root}")

    path = root / "safectl.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ControlError(f"cannot open control lock {path}: {exc}") from exc
    try:
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ControlError(
                "another aki-safectl operation is already in progress"
            ) from exc
        except OSError as exc:
            raise ControlError(f"cannot acquire control lock {path}: {exc}") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _instance_profiles() -> list[str]:
    result = _systemctl(
        "list-units",
        "--all",
        "--no-legend",
        "--plain",
        "akid@*.service",
        "akid-proxy@*.service",
        timeout=10.0,
    )
    if result.returncode != 0:
        return []
    names: set[str] = set()
    for line in result.stdout.splitlines():
        unit = line.split(maxsplit=1)[0] if line.strip() else ""
        match = UNIT_PATTERN.fullmatch(unit)
        if match:
            names.add(match.group(2))
    return sorted(names)


def _profile_names(action: str, requested: list[str]) -> list[str]:
    discovered = discover_profiles()
    names: list[str] = []
    if requested:
        for raw in requested:
            for part in raw.split(","):
                part = part.strip()
                if part:
                    try:
                        names.append(validate_profile_name(part))
                    except ConfigError as exc:
                        raise ControlError(
                            f"invalid profile name {part!r}: {exc}"
                        ) from exc
    else:
        names.extend(discovered)
        if action in {"stop", "restart", "status"}:
            names.extend(_instance_profiles())

    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    if unique:
        return unique
    if action == "start":
        raise ControlError(
            "no profiles found under ~/.aki; run python3 install-daemon.py first"
        )
    raise ControlError("no akid profiles or service instances found")


def _backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".letta" / "lc-local-backend"
    )


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _agent_exists(agent_id: str) -> bool:
    folder = _backend_dir() / "agents"
    try:
        paths = folder.glob("*.json")
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
    return (
        _backend_dir()
        / "conversations"
        / _b64url(f"{conversation_id}:{agent_id}")
        / "conversation.json"
    ).is_file()


def _socket_conflict(path: Path) -> str | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot inspect socket {path}: {exc}"
    if not stat.S_ISSOCK(path_stat.st_mode):
        return f"{path} exists and is not a socket"

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


def _profile_socket(config: AkiConfig, profile: str) -> Path:
    configured = config.socket.strip()
    return (
        Path(configured).expanduser()
        if configured
        else default_socket_path(profile)
    )


def _runtime_preflight(profile: str, config: AkiConfig) -> None:
    runtime = Path(
        os.environ.get(
            "AKI_LETTA_RUNTIME",
            Path.home() / ".local" / "lib" / "aki" / "letta-code" / "current",
        )
    ).expanduser()
    letta_js = runtime / "letta.js"
    if not letta_js.is_file():
        raise ControlError(
            f"{profile}: vendored Letta runtime is missing at {letta_js}; "
            "run python3 scripts/build-letta.py --build --install"
        )

    runner = ROOT / "bin" / "letta-aki"
    if not runner.is_file() or not os.access(runner, os.X_OK):
        raise ControlError(f"{profile}: Letta launcher is not executable: {runner}")

    node = Path(os.environ.get("AKI_NODE_BIN", "/usr/bin/node")).expanduser()
    if not node.is_file() or not os.access(node, os.X_OK):
        raise ControlError(f"{profile}: node executable not found: {node}")

    if not config.sandbox:
        return
    candidates = [
        os.environ.get("AKI_BWRAP_BIN", ""),
        "/usr/bin/bwrap",
        shutil.which("bwrap") or "",
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return
    raise ControlError(
        f"{profile}: sandbox is enabled but bwrap is unavailable; "
        "install bwrap or explicitly configure a sandbox-disabled profile"
    )


def _load_spec(
    profile: str, path: Path, *, check_runtime: bool
) -> ProfileSpec:
    try:
        config = load_config(path, profile=profile)
    except (ConfigError, OSError) as exc:
        raise ControlError(f"{profile}: cannot load {path}: {exc}") from exc

    socket_path = _profile_socket(config, profile)
    gateway_is_active = _unit_active(gateway_unit(profile))
    proxy_is_active = _unit_active(proxy_unit(profile))

    if check_runtime:
        if not _agent_exists(config.agent_id):
            raise ControlError(
                f"{profile}: agent {config.agent_id} was not found under "
                f"{_backend_dir() / 'agents'}"
            )
        if not _conversation_exists(config.agent_id, config.conversation):
            raise ControlError(
                f"{profile}: conversation {config.conversation!r} for agent "
                f"{config.agent_id} does not exist"
            )
        _runtime_preflight(profile, config)

    if not gateway_is_active:
        conflict = _socket_conflict(socket_path)
        if conflict:
            raise ControlError(f"{profile}: {conflict}")

    if not proxy_is_active and _port_in_use(config.proxy_host, config.proxy_port):
        raise ControlError(
            f"{profile}: LLM proxy port {config.proxy_host}:{config.proxy_port} "
            "is already in use"
        )

    return ProfileSpec(
        name=profile,
        config=config,
        socket_path=socket_path,
        proxy_host=config.proxy_host,
        proxy_port=config.proxy_port,
        gateway_active=gateway_is_active,
        proxy_active=proxy_is_active,
    )


def _check_conflicts(specs: list[ProfileSpec]) -> None:
    requested = {spec.name for spec in specs}
    universe: dict[str, ProfileSpec] = {spec.name: spec for spec in specs}

    for name, path in discover_profiles().items():
        if name in universe:
            continue
        gateway_is_active = _unit_active(gateway_unit(name))
        proxy_is_active = _unit_active(proxy_unit(name))
        if not gateway_is_active and not proxy_is_active:
            continue
        try:
            config = load_config(path, profile=name)
        except (ConfigError, OSError) as exc:
            _warn(f"cannot inspect active profile {name} for conflicts: {exc}")
            continue
        universe[name] = ProfileSpec(
            name=name,
            config=config,
            socket_path=_profile_socket(config, name),
            proxy_host=config.proxy_host,
            proxy_port=config.proxy_port,
            gateway_active=gateway_is_active,
            proxy_active=proxy_is_active,
        )

    seen: set[tuple[str, str, str]] = set()
    for current in specs:
        gateway_starting = not current.gateway_active
        proxy_starting = not current.proxy_active
        if not gateway_starting and not proxy_starting:
            continue
        for other in universe.values():
            if other.name == current.name:
                continue
            other_gateway_present = other.name in requested or other.gateway_active
            other_proxy_present = other.name in requested or other.proxy_active
            if gateway_starting and other_gateway_present:
                if current.config.agent_id == other.config.agent_id:
                    key = (min(current.name, other.name), max(current.name, other.name), "agent")
                    if key not in seen:
                        seen.add(key)
                        raise ControlError(
                            f"agent {current.config.agent_id} is already used by "
                            f"profile {other.name}"
                        )
                if current.socket_path == other.socket_path:
                    key = (min(current.name, other.name), max(current.name, other.name), "socket")
                    if key not in seen:
                        seen.add(key)
                        raise ControlError(
                            f"socket {current.socket_path} is already used by "
                            f"profile {other.name}"
                        )
            if proxy_starting and other_proxy_present:
                if (
                    current.proxy_host == other.proxy_host
                    and current.proxy_port == other.proxy_port
                ):
                    key = (min(current.name, other.name), max(current.name, other.name), "proxy")
                    if key not in seen:
                        seen.add(key)
                        raise ControlError(
                            f"LLM proxy port {current.proxy_host}:{current.proxy_port} "
                            f"is already used by profile {other.name}"
                        )


def _preflight_start(profiles: list[str]) -> list[ProfileSpec]:
    discovered = discover_profiles()
    specs: list[ProfileSpec] = []
    for name in profiles:
        path = discovered.get(name) or profile_config_path(name)
        if not path.is_file():
            raise ControlError(
                f"{name}: profile config does not exist at {path}; "
                "run python3 install-daemon.py or create the profile first"
            )
        specs.append(_load_spec(name, path, check_runtime=True))
    _check_conflicts(specs)
    return specs


def _wait_port(host: str, port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _port_in_use(host, port):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def _rpc_health(socket_path: Path, timeout: float = 1.0) -> dict | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        request = {
            "jsonrpc": "2.0",
            "id": "aki-safectl",
            "method": "aki.health",
            "params": {},
        }
        sock.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        line = sock.makefile("rb").readline()
        if not line:
            return None
        message = json.loads(line.decode("utf-8"))
        result = message.get("result")
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def _health_detail(health: dict | None) -> str:
    if health is None:
        return "no health response"
    keys = ("backend_ready", "bridge_ready", "channel_ready", "sandbox_ready")
    return ", ".join(f"{key}={health.get(key)}" for key in keys)


def _wait_health(socket_path: Path, deadline: float) -> dict | None:
    last: dict | None = None
    while time.monotonic() < deadline:
        last = _rpc_health(socket_path)
        if last is not None and last.get("ready"):
            return last
        time.sleep(POLL_INTERVAL_SECONDS)
    return last if last is not None and last.get("ready") else None


def _journal_hint(profile: str) -> None:
    print(
        "inspect logs with: "
        f"journalctl --user -u {gateway_unit(profile)} -u {proxy_unit(profile)} "
        "-n 200 --no-pager",
        file=sys.stderr,
    )


def _rollback(started: list[tuple[str, str]]) -> None:
    for profile, unit in reversed(started):
        try:
            _info(f"{profile}: rolling back {unit}")
            _systemctl_checked(("stop", unit), f"rollback stop {unit}")
        except ControlError as exc:
            _warn(str(exc))


def _start_one(
    spec: ProfileSpec,
    wait_seconds: float,
    started: list[tuple[str, str]],
) -> None:
    profile = spec.name
    gw = gateway_unit(profile)
    px = proxy_unit(profile)
    proxy_is_active = _unit_active(px)
    gateway_is_active = _unit_active(gw)

    if proxy_is_active and gateway_is_active:
        _info(f"{profile}: already running; checking readiness")
    else:
        if not proxy_is_active:
            started.append((profile, px))
            _info(f"{profile}: starting {px}")
            _systemctl_checked(("start", px), f"start {px}")
        else:
            _info(f"{profile}: {px} is already running")

        deadline = time.monotonic() + wait_seconds
        if not _wait_port(spec.proxy_host, spec.proxy_port, deadline):
            raise ControlError(
                f"{profile}: LLM proxy did not listen on "
                f"{spec.proxy_host}:{spec.proxy_port} within {wait_seconds:g}s"
            )
        _info(f"{profile}: proxy ready on {spec.proxy_host}:{spec.proxy_port}")

        if not gateway_is_active:
            started.append((profile, gw))
            _info(f"{profile}: starting {gw}")
            _systemctl_checked(("start", gw), f"start {gw}")
        else:
            _info(f"{profile}: {gw} is already running")

    deadline = time.monotonic() + wait_seconds
    health = _wait_health(spec.socket_path, deadline)
    if health is None:
        latest = _rpc_health(spec.socket_path)
        raise ControlError(
            f"{profile}: gateway did not become ready on {spec.socket_path} "
            f"within {wait_seconds:g}s ({_health_detail(latest)})"
        )
    _info(f"{profile}: ready on {spec.socket_path}")


def _start_profiles(
    profiles: list[str], wait_seconds: float, *, dry_run: bool
) -> int:
    specs = _preflight_start(profiles)
    if dry_run:
        for spec in specs:
            state = "running" if spec.gateway_active and spec.proxy_active else "stopped"
            _info(
                f"would ensure {spec.name} is ready "
                f"(proxy {spec.proxy_host}:{spec.proxy_port}, "
                f"socket {spec.socket_path}, currently {state})"
            )
        return 0

    started: list[tuple[str, str]] = []
    try:
        for spec in specs:
            _start_one(spec, wait_seconds, started)
    except BaseException:
        if started:
            _warn("start failed; stopping units started by this invocation")
        _rollback(started)
        raise
    return 0


def _wait_inactive(unit: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _unit_active(unit):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return not _unit_active(unit)


def _stop_one(
    profile: str,
    *,
    wait_seconds: float,
    dry_run: bool,
) -> bool:
    gw = gateway_unit(profile)
    px = proxy_unit(profile)
    gateway_is_active = _unit_active(gw)
    proxy_is_active = _unit_active(px)
    if not gateway_is_active and not proxy_is_active:
        _info(f"{profile}: already stopped")
        return True

    if dry_run:
        targets = []
        if gateway_is_active:
            targets.append(gw)
        if proxy_is_active:
            targets.append(px)
        _info("would stop " + ", then ".join(targets))
        return True

    if gateway_is_active:
        _info(f"{profile}: stopping {gw}")
        _systemctl_checked(("stop", gw), f"stop {gw}")
        deadline = time.monotonic() + wait_seconds + SYSTEMCTL_GRACE_SECONDS
        if not _wait_inactive(gw, deadline):
            raise ControlError(f"{profile}: {gw} is still active after stop")

    if proxy_is_active:
        _info(f"{profile}: stopping {px}")
        _systemctl_checked(("stop", px), f"stop {px}")
        deadline = time.monotonic() + wait_seconds + SYSTEMCTL_GRACE_SECONDS
        if not _wait_inactive(px, deadline):
            raise ControlError(f"{profile}: {px} is still active after stop")

    _info(f"{profile}: stopped; configuration and history were left untouched")
    return True


def _stop_profiles(
    profiles: list[str], wait_seconds: float, *, dry_run: bool
) -> int:
    failures = 0
    for profile in profiles:
        try:
            _stop_one(profile, wait_seconds=wait_seconds, dry_run=dry_run)
        except ControlError as exc:
            failures += 1
            print(f"error: {exc}", file=sys.stderr)
            _journal_hint(profile)
    return 1 if failures else 0


def _restart_profiles(
    profiles: list[str], wait_seconds: float, *, dry_run: bool
) -> int:
    active: list[ProfileSpec] = []
    for profile in profiles:
        gateway_is_active = _unit_active(gateway_unit(profile))
        proxy_is_active = _unit_active(proxy_unit(profile))
        if not gateway_is_active and not proxy_is_active:
            _info(f"{profile}: not running; skipping restart (use start to launch it)")
            continue
        discovered = discover_profiles()
        path = discovered.get(profile) or profile_config_path(profile)
        if not path.is_file():
            raise ControlError(
                f"{profile}: cannot restart because profile config is missing at {path}"
            )
        active.append(_load_spec(profile, path, check_runtime=True))

    if not active:
        _info("nothing is running")
        return 0
    _check_conflicts(active)

    if dry_run:
        for spec in active:
            _info(f"would restart {spec.name}")
        return 0

    for spec in active:
        profile = spec.name
        try:
            _info(f"{profile}: restarting")
            _stop_one(profile, wait_seconds=wait_seconds, dry_run=False)
            started: list[tuple[str, str]] = []
            try:
                _start_one(spec, wait_seconds, started)
            except BaseException:
                _rollback(started)
                raise
            _info(f"{profile}: restarted")
        except ControlError as exc:
            print(f"error: {exc}", file=sys.stderr)
            _journal_hint(profile)
            _warn("aborting remaining restarts to avoid a wider outage")
            return 1
    return 0


def _status_rows(profiles: list[str]) -> tuple[list[dict], bool]:
    discovered = discover_profiles()
    rows: list[dict] = []
    all_ok = True
    for name in profiles:
        gateway = _unit_state(gateway_unit(name))
        proxy = _unit_state(proxy_unit(name))
        row: dict = {
            "profile": name,
            "gateway": {
                "unit": gateway_unit(name),
                "active": gateway.active,
                "state": gateway.active_state,
                "sub_state": gateway.sub_state,
                "main_pid": gateway.main_pid,
                "enabled": gateway.file_state,
            },
            "proxy": {
                "unit": proxy_unit(name),
                "active": proxy.active,
                "state": proxy.active_state,
                "sub_state": proxy.sub_state,
                "main_pid": proxy.main_pid,
                "enabled": proxy.file_state,
            },
            "config": None,
            "socket": None,
            "proxy_address": None,
            "proxy_listening": False,
            "health": None,
            "healthy": False,
            "error": None,
        }
        path = discovered.get(name) or profile_config_path(name)
        if not path.is_file():
            row["error"] = f"profile config does not exist at {path}"
        else:
            try:
                config = load_config(path, profile=name)
            except (ConfigError, OSError) as exc:
                row["error"] = f"cannot load {path}: {exc}"
            else:
                socket_path = _profile_socket(config, name)
                row["config"] = str(path)
                row["socket"] = str(socket_path)
                row["proxy_address"] = f"{config.proxy_host}:{config.proxy_port}"
                row["proxy_listening"] = _port_in_use(
                    config.proxy_host, config.proxy_port
                )
                row["health"] = _rpc_health(socket_path)
                row["healthy"] = bool(
                    row["health"] is not None and row["health"].get("ready")
                )

        profile_ok = bool(
            gateway.active
            and proxy.active
            and row["healthy"]
            and row["proxy_listening"]
            and row["error"] is None
        )
        row["ok"] = profile_ok
        all_ok = all_ok and profile_ok
        rows.append(row)
    return rows, all_ok


def _status_profiles(profiles: list[str], *, json_mode: bool) -> int:
    rows, all_ok = _status_rows(profiles)
    if json_mode:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if all_ok else 1

    for row in rows:
        print(f"* {row['profile']}")
        for key in ("gateway", "proxy"):
            unit = row[key]
            state = unit["state"] + (
                f"/{unit['sub_state']}" if unit["sub_state"] else ""
            )
            pid = (
                f" pid={unit['main_pid']}"
                if unit["main_pid"] and unit["main_pid"] != 0
                else ""
            )
            print(
                f"    {unit['unit']:<28} {state:<18} "
                f"{unit['enabled']:<10}{pid}"
            )
        if row["error"]:
            print(f"    config{'':<24} {row['error']}")
            continue
        health = row["health"]
        if health is None:
            health_text = "unavailable"
        else:
            health_text = f"{health.get('status', 'unknown')} ({_health_detail(health)})"
        print(f"    {'socket':<28} {health_text}")
        print(f"    {'socket path':<28} {row['socket']}")
        listening = "listening" if row["proxy_listening"] else "not listening"
        print(f"    {'proxy address':<28} {row['proxy_address']} ({listening})")
        print(f"    {'result':<28} {'ok' if row['ok'] else 'degraded'}")
    return 0 if all_ok else 1


def _check_profiles(profiles: list[str], *, json_mode: bool) -> int:
    specs = _preflight_start(profiles)
    if json_mode:
        print(
            json.dumps(
                [
                    {
                        "profile": spec.name,
                        "config": str(spec.config.path),
                        "socket": str(spec.socket_path),
                        "proxy": f"{spec.proxy_host}:{spec.proxy_port}",
                        "gateway_active": spec.gateway_active,
                        "proxy_active": spec.proxy_active,
                        "ok": True,
                    }
                    for spec in specs
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for spec in specs:
        state = "running" if spec.gateway_active and spec.proxy_active else "stopped"
        _info(
            f"{spec.name}: preflight ok (config {spec.config.path}, "
            f"socket {spec.socket_path}, proxy {spec.proxy_host}:{spec.proxy_port}, "
            f"{state})"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aki-safectl.py",
        description="Safely manage akid systemd user services.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ./aki-safectl.py check\n"
            "  ./aki-safectl.py start\n"
            "  ./aki-safectl.py stop aki\n"
            "  ./aki-safectl.py restart --profile fuyu\n"
            "  ./aki-safectl.py status --json\n"
            "\n"
            "Default profile selection is every profile under ~/.aki.\n"
            "start/stop/restart do not change systemd enablement state.\n"
        ),
    )
    parser.add_argument(
        "action",
        choices=("start", "stop", "restart", "status", "check"),
        help="lifecycle action",
    )
    parser.add_argument(
        "profiles",
        nargs="*",
        help="profile names (default: all discovered profiles)",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        dest="profile_options",
        help="profile name; may be repeated",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        help=f"readiness wait in seconds (default: {DEFAULT_WAIT_SECONDS:g})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the planned lifecycle operations without changing units",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON output for status and check",
    )
    return parser


def _handle_sigterm(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite number greater than zero")
    wait_seconds = min(args.timeout, 600.0)
    requested = list(args.profiles) + list(args.profile_options)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        _ensure_user_systemd()
        profiles = _profile_names(args.action, requested)

        if args.action in {"start", "check"}:
            _ensure_units_installed()
        if args.action == "start":
            if args.dry_run:
                return _start_profiles(profiles, wait_seconds, dry_run=True)
            with _control_lock():
                return _start_profiles(profiles, wait_seconds, dry_run=False)
        if args.action == "stop":
            if args.dry_run:
                return _stop_profiles(profiles, wait_seconds, dry_run=True)
            with _control_lock():
                return _stop_profiles(profiles, wait_seconds, dry_run=False)
        if args.action == "restart":
            if args.dry_run:
                return _restart_profiles(profiles, wait_seconds, dry_run=True)
            with _control_lock():
                return _restart_profiles(profiles, wait_seconds, dry_run=False)
        if args.action == "status":
            return _status_profiles(profiles, json_mode=args.json)
        return _check_profiles(profiles, json_mode=args.json)
    except (ControlError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
