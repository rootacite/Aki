#!/usr/bin/env python3
"""Install and manage the Aki gateway as a systemd user service."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
SERVICE_NAME = "akid.service"


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


def default_socket_path() -> Path:
    configured = os.environ.get("AKI_SOCKET")
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "aki" / "akid.sock"


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
    agent_id: str,
    conversation_id: str,
    letta_bin: str,
    socket_path: Path,
    mqtt_enabled: bool = True,
    mqtt_host: str = "192.168.3.1",
    mqtt_port: int = 1883,
    mqtt_topic: str = "tokifactor/text",
    mqtt_magic: str = "akid-default-channel",
    sandbox_enabled: bool = True,
    bwrap_bin: str = "bwrap",
    writable_state: str | None = None,
    mqtt_state: str | None = None,
    avatar_path: str | None = None,
) -> str:
    return f"""[Unit]
Description=Aki local gateway (akid)
After=network.target

[Service]
Type=simple
ExecStart={python_bin} {repo / 'engine' / 'akid.py'}
WorkingDirectory={repo}
Environment="PYTHONUNBUFFERED=1"
Environment="AKI_AGENT_ID={agent_id}"
Environment="AKI_CONVERSATION_ID={conversation_id}"
Environment="AKI_LETTA_BIN={letta_bin}"
Environment="AKI_SOCKET={socket_path}"
Environment="XDG_RUNTIME_DIR=%t"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"
Environment="AKI_MQTT_ENABLED={1 if mqtt_enabled else 0}"
Environment="AKI_MQTT_HOST={mqtt_host}"
Environment="AKI_MQTT_PORT={mqtt_port}"
Environment="AKI_MQTT_TOPIC={mqtt_topic}"
Environment="AKI_MQTT_MAGIC={mqtt_magic}"
Environment="AKI_MQTT_STATE={mqtt_state or str(Path.home() / '.local' / 'share' / 'aki' / 'mqtt.json')}"
Environment="AKI_SANDBOX_ENABLED={1 if sandbox_enabled else 0}"
Environment="AKI_BWRAP_BIN={bwrap_bin}"
Environment="AKI_WRITABLE_STATE={writable_state or str(Path.home() / '.local' / 'share' / 'aki' / 'writable-dirs.json')}"
Environment="AKI_TOKIFACTOR_IMAGE_REQUIRE_APPROVAL=0"
Environment="AKI_AVATAR_PATH={avatar_path or repo / 'aki_avi.jpg'}"
Environment="AKI_CAMERA_ENABLED=1"
Environment="AKI_CAMERA_REQUIRE_APPROVAL=1"
Environment="AKI_FFMPEG_BIN={shutil.which('ffmpeg') or 'ffmpeg'}"
Environment="AKI_FFPROBE_BIN={shutil.which('ffprobe') or 'ffprobe'}"
Environment="AKI_CAMERA_DEVICE=/dev/video0"
Environment="AKI_CAMERA_WIDTH=1280"
Environment="AKI_CAMERA_HEIGHT=720"
Environment="AKI_CAMERA_OUTPUT_DIR=/tmp/aki/camera"
Environment="AKI_SCREENSHOT_ENABLED=1"
Environment="AKI_SCREENSHOT_REQUIRE_APPROVAL=0"
Environment="AKI_GRIM_BIN={shutil.which('grim') or 'grim'}"
Environment="AKI_SPECTACLE_BIN={shutil.which('spectacle') or 'spectacle'}"
Environment="AKI_SCREENSHOT_OUTPUT_DIR=/tmp/aki/screenshots"
Environment="AKI_LIVE_CONTEXT_ENABLED=1"
Environment="AKI_LIVE_CONTEXT_BUDGET_TOKENS=65536"
Environment="AKI_ZSH_HISTORY={Path.home() / '.zsh_history'}"
Environment="AKI_PS_BIN={shutil.which('ps') or 'ps'}"
Environment="AKI_QDBUS_BIN={shutil.which('qdbus') or shutil.which('qdbus6') or ''}"
Environment="AKI_LIVE_CONTEXT_TIMEOUT=3.0"
Restart=on-failure
RestartSec=2
TimeoutStopSec=15
KillMode=control-group
RuntimeDirectory=aki
RuntimeDirectoryMode=0700
UMask=0077

[Install]
WantedBy=default.target
"""


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


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


def journal_hint() -> None:
    print("akid did not become ready; inspect logs with:", file=sys.stderr)
    print("  journalctl --user -u akid.service -n 200 --no-pager", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Aki gateway user service")
    parser.add_argument("--dry-run", action="store_true", help="print the unit without writing")
    parser.add_argument("--uninstall", action="store_true", help="stop and remove the unit")
    parser.add_argument("--restart", action="store_true", help="restart an installed unit")
    parser.add_argument("--status", action="store_true", help="show unit and gateway status")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--letta-bin", default=shutil.which("letta") or "letta")
    parser.add_argument("--socket", help="gateway socket path")
    parser.add_argument("--mqtt-host", default="192.168.3.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-topic", default="tokifactor/text")
    parser.add_argument("--mqtt-magic", default="akid-default-channel")
    parser.add_argument("--no-mqtt", action="store_true", help="disable the tokifactor channel")
    parser.add_argument("--bwrap-bin", default=shutil.which("bwrap") or "/usr/bin/bwrap")
    parser.add_argument("--writable-state", help="writable directory registry JSON path")
    parser.add_argument("--mqtt-state", help="persistent MQTT settings JSON path")
    parser.add_argument("--avatar-path", help="Aki avatar image path")
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox (unsafe)")
    args = parser.parse_args(argv)

    if os.geteuid() == 0:
        print("refusing to install a per-user service as root", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parent
    python_bin = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    socket_path = Path(args.socket).expanduser() if args.socket else default_socket_path()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / SERVICE_NAME
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

    if args.dry_run:
        print(f"# would link {default_user_bin_dir() / 'akictl'} -> {repo / 'bin' / 'akictl'}")
        print(f"# would write {unit_path}")
        print(unit, end="")
        return 0

    if args.status:
        try:
            result = run(["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"], check=False)
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
            run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], check=False)
        except FileNotFoundError:
            with_error = True
        if unit_path.exists():
            unit_path.unlink()
        try:
            run(["systemctl", "--user", "daemon-reload"], check=False)
        except FileNotFoundError:
            with_error = True
        try:
            removed_link = uninstall_akictl(repo)
        except OSError as exc:
            print(f"warning: could not remove akictl link: {exc}", file=sys.stderr)
            removed_link = False
        if removed_link:
            print(f"akictl link removed: {default_user_bin_dir() / 'akictl'}")
        print("akid.service removed; MemFS and conversation history were left untouched")
        return 1 if with_error else 0

    if args.restart:
        if not unit_path.exists():
            print(f"{SERVICE_NAME} is not installed at {unit_path}", file=sys.stderr)
            return 1
        try:
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "restart", SERVICE_NAME])
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(f"failed to restart akid: {exc}", file=sys.stderr)
            return 1
        if not wait_ready(socket_path):
            journal_hint()
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
    os.chmod(unit_path, 0o644)
    try:
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
    except FileNotFoundError:
        print("systemctl is not available; unit written but not started", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"systemctl failed: {exc.stderr or exc}", file=sys.stderr)
        journal_hint()
        return 1

    if not wait_ready(socket_path):
        journal_hint()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
