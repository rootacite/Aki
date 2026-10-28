#!/usr/bin/env python3
"""Install and manage the Aki gateway as a systemd user service."""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
SERVICE_NAME = "akid.service"
SERVICE_TEMPLATE = "akid@.service"
PROXY_TEMPLATE = "akid-proxy@.service"
LEGACY_SERVICE_NAME = "akid.service"


def gateway_unit(profile: str) -> str:
    return f"akid@{profile}.service"


def proxy_unit_name(profile: str) -> str:
    return f"akid-proxy@{profile}.service"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_user_bin_dir() -> Path:
    return Path.home() / ".local" / "bin"


def install_akictl(repo: Path, *, install_dir: Path | None = None) -> Path:
    target = (repo / "bin" / "akictl").resolve()
    if not target.is_file():
        raise RuntimeError(f"akictl entry point not found at {target}")
    chmod_failed = False
    try:
        os.chmod(target, 0o755)
    except OSError:
        chmod_failed = True
    directory = (
        Path(install_dir).expanduser()
        if install_dir is not None
        else default_user_bin_dir()
    )
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / "akictl"
    if link.is_symlink():
        if link.resolve() == target:
            return link
        raise RuntimeError(f"{link} already exists and points to {link.resolve()}")
    if link.exists():
        raise RuntimeError(f"{link} already exists and is not a symlink")
    link.symlink_to(target)
    if chmod_failed:
        print(f"warning: could not mark {target} executable", file=sys.stderr)
    return link


def uninstall_akictl(repo: Path, *, install_dir: Path | None = None) -> bool:
    directory = (
        Path(install_dir).expanduser()
        if install_dir is not None
        else default_user_bin_dir()
    )
    link = directory / "akictl"
    target = (repo / "bin" / "akictl").resolve()
    if not link.is_symlink():
        return False
    try:
        resolved = link.resolve()
    except OSError:
        return False
    if resolved != target:
        return False
    link.unlink()
    return True


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def default_socket_path(profile: str = "aki") -> Path:
    configured = os.environ.get("AKI_SOCKET")
    if configured:
        return Path(configured).expanduser()
    try:
        from engine.aki_config import load_config, profile_config_path

        config_path = profile_config_path(profile)
        if config_path.is_file():
            socket_path = load_config(config_path, profile=profile).socket
            if socket_path:
                return Path(socket_path).expanduser()
    except (ImportError, OSError, ValueError):
        pass
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    filename = "akid.sock" if profile == "aki" else f"{profile}.sock"
    return Path(runtime) / "aki" / filename


def backend_dir() -> Path:
    return Path(os.environ.get("AKI_LETTA_BACKEND_DIR", Path.home() / ".letta" / "lc-local-backend")).expanduser()


def find_agent_file(agent_id: str) -> Path | None:
    folder = backend_dir() / "agents"
    if not folder.is_dir():
        return None
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("id") == agent_id:
            return path
    return None


def ensure_conversation(agent_id: str, conversation_id: str, cwd: Path) -> Path:
    backend = backend_dir()
    if find_agent_file(agent_id) is None:
        raise RuntimeError(f"agent {agent_id} was not found under {backend / 'agents'}")
    target = backend / "conversations" / b64url(f"{conversation_id}:{agent_id}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    timestamp = now_iso()
    manifest = target / "manifest.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "message_format": "pi-session-entry-jsonl",
                    "provider_stack": "pi-ai",
                    "created_at": timestamp,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    conversation = target / "conversation.json"
    if not conversation.exists():
        conversation.write_text(
            json.dumps(
                {
                    "id": conversation_id,
                    "agent_id": agent_id,
                    "archived": False,
                    "archived_at": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "last_message_at": None,
                    "summary": None,
                    "in_context_message_ids": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    messages = target / "messages.jsonl"
    if not messages.exists():
        session = {
            "type": "session",
            "version": 3,
            "id": conversation_id,
            "timestamp": timestamp,
            "cwd": str(cwd),
        }
        messages.write_text(json.dumps(session, separators=(",", ":")) + "\n", encoding="utf-8")
    for path in (manifest, conversation, messages):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return target


def service_unit(
    *,
    python_bin: str,
    repo: Path,
    agent_id: str = "",
    conversation_id: str = "",
    letta_bin: str = "",
    socket_path: Path | None = None,
    mqtt_enabled: bool = True,
    mqtt_host: str = "127.0.0.1",
    mqtt_port: int = 1883,
    mqtt_topic: str = "tokifactor/text",
    mqtt_magic: str = "akid-default-channel",
    sandbox_enabled: bool = True,
    bwrap_bin: str = "bwrap",
    writable_state: str | None = None,
    mqtt_state: str | None = None,
    avatar_path: str | None = None,
) -> str:
    del (
        agent_id,
        conversation_id,
        socket_path,
        mqtt_enabled,
        mqtt_host,
        mqtt_port,
        mqtt_topic,
        mqtt_magic,
        sandbox_enabled,
        bwrap_bin,
        writable_state,
        mqtt_state,
        avatar_path,
    )
    return f"""[Unit]
Description=Aki local gateway (%i)
After=network.target akid-proxy@%i.service
Wants=akid-proxy@%i.service

[Service]
Type=simple
ExecStart={python_bin} {repo / 'engine' / 'akid.py'} --profile %i
WorkingDirectory={repo}
Environment="PYTHONUNBUFFERED=1"
Environment="AKI_LETTA_BIN={letta_bin or repo / 'bin' / 'letta-aki'}"
Environment="AKI_LETTA_RUNTIME={Path.home() / '.local' / 'lib' / 'aki' / 'letta-code' / 'current'}"
Environment="XDG_RUNTIME_DIR=%t"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"
Restart=on-failure
RestartSec=2
TimeoutStopSec=15
KillMode=control-group
RuntimeDirectory=aki
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
UMask=0077

[Install]
WantedBy=default.target
"""


def proxy_service_unit(*, python_bin: str, repo: Path) -> str:
    return f"""[Unit]
Description=Aki loopback LLM proxy (%i)
After=network.target

[Service]
Type=simple
ExecStart={python_bin} {repo / 'engine' / 'akid_proxy.py'} --profile %i
WorkingDirectory={repo}
Environment="PYTHONUNBUFFERED=1"
Restart=on-failure
RestartSec=2
TimeoutStopSec=15
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
"""


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def ensure_letta_runtime(repo: Path) -> None:
    builder = repo / "scripts" / "build-letta.py"
    if not builder.is_file():
        raise RuntimeError(f"Letta build script not found: {builder}")
    check = subprocess.run(
        [sys.executable, str(builder), "--check"],
        text=True,
        capture_output=True,
        check=False,
    )
    if check.returncode == 0:
        return
    print("building vendored Letta runtime...", file=sys.stderr)
    result = subprocess.run(
        [sys.executable, str(builder), "--build", "--install"],
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (check.stderr or check.stdout or "").strip()
        raise RuntimeError(
            "vendored Letta runtime is missing and could not be built"
            + (f": {detail[-1000:]}" if detail else "")
        )


def rpc_health(socket_path: Path, timeout: float = 2.0) -> dict | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        request = {"jsonrpc": "2.0", "id": "installer", "method": "aki.health", "params": {}}
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        file = sock.makefile("rb")
        line = file.readline()
        if not line:
            return None
        message = json.loads(line.decode("utf-8"))
        result = message.get("result")
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def wait_ready(socket_path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            health = rpc_health(socket_path)
            if health is not None and health.get("ready"):
                print(f"akid is ready on {socket_path}")
                return True
        time.sleep(0.5)
    return False


def journal_hint(profile: str = "aki") -> None:
    print("akid did not become ready; inspect logs with:", file=sys.stderr)
    print(
        f"  journalctl --user -u {gateway_unit(profile)} -u {proxy_unit_name(profile)} "
        "-n 200 --no-pager",
        file=sys.stderr,
    )


def _systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", "--user", *args], check=check)


def _enable_profile(profile: str) -> None:
    _systemctl("enable", "--now", proxy_unit_name(profile), check=True)
    _systemctl("enable", "--now", gateway_unit(profile), check=True)


def _disable_profile(profile: str) -> None:
    _systemctl("disable", "--now", gateway_unit(profile), check=False)
    _systemctl("disable", "--now", proxy_unit_name(profile), check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Aki gateway user service")
    parser.add_argument("--dry-run", action="store_true", help="print the units without writing")
    parser.add_argument("--uninstall", action="store_true", help="stop and remove the units")
    parser.add_argument("--restart", action="store_true", help="restart installed units")
    parser.add_argument("--status", action="store_true", help="show unit and gateway status")
    parser.add_argument("--profile", default="aki", help="default profile installed and started")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument(
        "--letta-bin",
        default=str(Path(__file__).resolve().parent / "bin" / "letta-aki"),
    )
    parser.add_argument("--socket", help="gateway socket path")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-topic", default="tokifactor/text")
    parser.add_argument("--mqtt-magic", default="akid-default-channel")
    parser.add_argument("--no-mqtt", action="store_true", help="disable the tokifactor channel")
    parser.add_argument("--bwrap-bin", default=shutil.which("bwrap") or "/usr/bin/bwrap")
    parser.add_argument("--writable-state", help="writable directory registry JSON path")
    parser.add_argument("--mqtt-state", help="persistent MQTT settings JSON path")
    parser.add_argument("--avatar-path", help="Aki avatar image path")
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox (unsafe)")
    parser.add_argument(
        "--skip-letta-build",
        action="store_true",
        help="do not verify/build the vendored Letta runtime",
    )
    args = parser.parse_args(argv)

    if os.geteuid() == 0:
        print("refusing to install a per-user service as root", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parent
    if not (args.dry_run or args.uninstall or args.status or args.skip_letta_build):
        try:
            ensure_letta_runtime(repo)
        except (OSError, RuntimeError) as exc:
            print(f"cannot prepare vendored Letta runtime: {exc}", file=sys.stderr)
            return 1
    python_bin = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    profile = args.profile.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", profile):
        print("profile must match [a-z0-9][a-z0-9_-]{0,31}", file=sys.stderr)
        return 2
    socket_path = Path(args.socket).expanduser() if args.socket else default_socket_path(profile)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / SERVICE_TEMPLATE
    proxy_path = unit_dir / PROXY_TEMPLATE
    legacy_path = unit_dir / LEGACY_SERVICE_NAME
    unit = service_unit(
        python_bin=python_bin,
        repo=repo,
        agent_id=args.agent_id,
        conversation_id="default",
        letta_bin=args.letta_bin,
        socket_path=socket_path,
        mqtt_enabled=not args.no_mqtt,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_topic=args.mqtt_topic,
        mqtt_magic=args.mqtt_magic,
        sandbox_enabled=not args.no_sandbox,
        bwrap_bin=args.bwrap_bin,
        writable_state=args.writable_state,
        mqtt_state=args.mqtt_state,
        avatar_path=args.avatar_path,
    )
    proxy_unit = proxy_service_unit(python_bin=python_bin, repo=repo)

    if args.dry_run:
        print(f"# would link {default_user_bin_dir() / 'akictl'} -> {repo / 'bin' / 'akictl'}")
        print(f"# would write {unit_path}")
        print(unit, end="")
        print(f"# would write {proxy_path}")
        print(proxy_unit, end="")
        return 0

    if args.status:
        try:
            for name in (proxy_unit_name(profile), gateway_unit(profile)):
                result = _systemctl("status", name, "--no-pager", check=False)
                print(result.stdout, end="")
                if result.stderr:
                    print(result.stderr, end="", file=sys.stderr)
        except FileNotFoundError:
            print("systemctl is not available", file=sys.stderr)
            return 1
        link = default_user_bin_dir() / "akictl"
        target = (repo / "bin" / "akictl").resolve()
        if link.is_symlink() and link.resolve() == target:
            print(f"akictl: {link} -> {target}")
        else:
            print(f"akictl: not installed at {link}")
        health = rpc_health(socket_path)
        print(json.dumps(health, ensure_ascii=False, indent=2) if health else "gateway health unavailable")
        return 0 if health and health.get("ready") else 1

    if args.uninstall:
        with_error = False
        try:
            _disable_profile(profile)
            _systemctl("disable", "--now", LEGACY_SERVICE_NAME, check=False)
        except FileNotFoundError:
            with_error = True
        for path in (unit_path, proxy_path, legacy_path):
            if path.exists():
                path.unlink()
        try:
            _systemctl("daemon-reload", check=False)
        except FileNotFoundError:
            with_error = True
        try:
            removed_link = uninstall_akictl(repo)
        except OSError as exc:
            print(f"warning: could not remove akictl link: {exc}", file=sys.stderr)
            removed_link = False
        if removed_link:
            print(f"akictl link removed: {default_user_bin_dir() / 'akictl'}")
        print("akid units removed; profiles, MemFS and conversation history were left untouched")
        return 1 if with_error else 0

    if args.restart:
        if not unit_path.exists() or not proxy_path.exists():
            print(f"Aki units are not installed under {unit_dir}", file=sys.stderr)
            return 1
        try:
            _systemctl("daemon-reload")
            _systemctl("restart", proxy_unit_name(profile))
            _systemctl("restart", gateway_unit(profile))
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(f"failed to restart akid: {exc}", file=sys.stderr)
            return 1
        if not wait_ready(socket_path):
            journal_hint(profile)
            return 1
        return 0

    if not args.no_mqtt and importlib.util.find_spec("cryptography") is None:
        print(
            "warning: Python package 'cryptography' is missing; the tokifactor MQTT channel "
            "will stay unavailable until it is installed",
            file=sys.stderr,
        )
    if not args.no_sandbox:
        try:
            probe = subprocess.run(
                [args.bwrap_bin, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"cannot run bwrap: {exc}", file=sys.stderr)
            return 1
        if probe.returncode != 0:
            print(f"bwrap probe failed: {(probe.stderr or '').strip()}", file=sys.stderr)
            return 1

    try:
        target = ensure_conversation(args.agent_id, "default", repo)
        print(f"conversation ready: {target}")
    except (OSError, RuntimeError) as exc:
        print(f"cannot prepare conversation: {exc}", file=sys.stderr)
        return 1

    try:
        akictl_link = install_akictl(repo)
        print(f"akictl installed: {akictl_link} -> {(repo / 'bin' / 'akictl').resolve()}")
    except (OSError, RuntimeError) as exc:
        print(f"cannot install akictl: {exc}", file=sys.stderr)
        return 1

    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit, encoding="utf-8")
    proxy_path.write_text(proxy_unit, encoding="utf-8")
    os.chmod(unit_path, 0o644)
    os.chmod(proxy_path, 0o644)
    legacy = legacy_path.exists()
    if legacy:
        # The legacy akid process owns the profile socket and proxy port. Stop it
        # before starting the split proxy/gateway services; rollback restarts it.
        with contextlib.suppress(FileNotFoundError):
            _systemctl("disable", "--now", LEGACY_SERVICE_NAME, check=False)
    try:
        _systemctl("daemon-reload")
        _enable_profile(profile)
    except FileNotFoundError:
        print("systemctl is not available; units written but not started", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"systemctl failed: {exc.stderr or exc}", file=sys.stderr)
        if legacy:
            with contextlib.suppress(FileNotFoundError):
                _disable_profile(profile)
                _systemctl("enable", "--now", LEGACY_SERVICE_NAME, check=False)
        journal_hint(profile)
        return 1

    if not wait_ready(socket_path):
        if legacy:
            print("new profile failed health check; rolling back to akid.service", file=sys.stderr)
            with contextlib.suppress(FileNotFoundError):
                _disable_profile(profile)
                _systemctl("enable", "--now", LEGACY_SERVICE_NAME, check=False)
                unit_path.unlink(missing_ok=True)
                proxy_path.unlink(missing_ok=True)
                _systemctl("daemon-reload", check=False)
        journal_hint(profile)
        return 1
    if legacy:
        legacy_path.unlink(missing_ok=True)
        with contextlib.suppress(FileNotFoundError):
            _systemctl("daemon-reload", check=False)
        print(f"legacy {LEGACY_SERVICE_NAME} migrated to {gateway_unit(profile)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
