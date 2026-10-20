#!/usr/bin/env python3
"""Render userdata persona templates into the Letta MemFS."""

from __future__ import annotations

import argparse
import hashlib
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

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
USERNAME_PLACEHOLDER = "{{USERNAME}}"
DEFAULT_USERNAME = "先生"
USERNAME_FILE = "username.txt"
ALLOWLIST = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)
FRONTMATTER_FILES = {
    "persona.md",
    "memory-policy.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
}
PRIVATE_MARKERS = (
    "sk-",
    "ghp_",
    "AKIA",
    "BEGIN PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def resolve_agent_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("AKI_AGENT_ID")
    if configured:
        return configured
    memfs = default_backend_dir() / "memfs"
    candidates = sorted(path.name for path in memfs.iterdir() if path.is_dir()) if memfs.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    if DEFAULT_AGENT_ID in candidates:
        return DEFAULT_AGENT_ID
    raise SystemExit("cannot determine agent id; pass --agent-id")


def resolve_memory_dir(agent_id: str, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
    else:
        path = default_backend_dir() / "memfs" / agent_id / "memory"
    if not path.is_dir():
        raise SystemExit(f"MemFS memory directory not found: {path}")
    return path


def normalize_username(value: str) -> str:
    value = value.strip()
    if not value:
        raise SystemExit("username must not be empty")
    if len(value) > 64:
        raise SystemExit("username must be at most 64 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SystemExit("username contains control characters")
    return value


def read_username(source: Path, override: str | None) -> str:
    if override is not None:
        return normalize_username(override)
    path = source / USERNAME_FILE
    if not path.is_file():
        return DEFAULT_USERNAME
    try:
        return normalize_username(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc


def render_template(data: bytes, username: str) -> bytes:
    return data.decode("utf-8").replace(USERNAME_PLACEHOLDER, username).encode("utf-8")


def validate_content(rel: str, data: bytes, *, allow_placeholder: bool) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{rel}: not valid UTF-8") from exc
    if not text.strip():
        raise SystemExit(f"{rel}: file is empty")
    if "\x00" in text:
        raise SystemExit(f"{rel}: contains a NUL byte")
    if not allow_placeholder and USERNAME_PLACEHOLDER in text:
        raise SystemExit(f"{rel}: unresolved {USERNAME_PLACEHOLDER}")
    if rel in FRONTMATTER_FILES:
        if not text.startswith("---\n"):
            raise SystemExit(f"{rel}: missing frontmatter")
        front, _, _ = text[3:].partition("\n---")
        if not re.search(r"(?m)^name:\s*\S+", front):
            raise SystemExit(f"{rel}: frontmatter is missing name")
        if not re.search(r"(?m)^description:\s*\S+", front):
            raise SystemExit(f"{rel}: frontmatter is missing description")
    elif rel == "character/MEMORY.md" and text.startswith("---"):
        raise SystemExit(f"{rel}: MEMORY.md must not have frontmatter")
    for marker in PRIVATE_MARKERS:
        if marker in text:
            raise SystemExit(f"{rel}: looks like it contains a secret ({marker})")
    return data


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(memory_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(memory_dir), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def backup_targets(
    memory_dir: Path, backup_root: Path, changed: list[str]
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = backup_root / f"userdata-sync-{stamp}"
    suffix = 1
    while backup.exists():
        backup = backup_root / f"userdata-sync-{stamp}-{suffix}"
        suffix += 1
    backup.mkdir(parents=True, mode=0o700)
    manifest: dict[str, object] = {
        "created_at": now_iso(),
        "memory_dir": str(memory_dir),
        "files": {},
    }
    files: dict[str, object] = {}
    for rel in changed:
        src = memory_dir / rel
        if not src.exists():
            continue
        dst = backup / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        data = src.read_bytes()
        files[rel] = {"sha256": file_digest(data), "bytes": len(data)}
    manifest["files"] = files
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return backup


def write_username_file(source: Path, username: str) -> bool:
    path = source / USERNAME_FILE
    text = username + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, 0o644)
    os.replace(temp, path)
    return True


def rpc_health(socket_path: Path, timeout: float = 2.0) -> dict | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        request = {"jsonrpc": "2.0", "id": "sync", "method": "aki.health", "params": {}}
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
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


def wait_ready(socket_path: Path, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = rpc_health(socket_path)
        if health is not None and health.get("ready"):
            return True
        time.sleep(0.5)
    return False


def emit(result: dict, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result.get("dry_run"):
        print(f"username: {result['username']}")
        print("would update:")
        for rel in result["files"]:
            print(f"  {rel}")
        return
    if not result["changed"]:
        print("userdata is already in sync with MemFS")
        if result.get("username_file_updated"):
            print(f"username file updated: {result['username']}")
        return
    print(f"username: {result['username']}")
    print(f"synced {len(result['files'])} file(s) into {result['memory_dir']}")
    for rel in result["files"]:
        print(f"  {rel}")
    print(f"backup: {result['backup']}")
    print(f"commit: {result['commit']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render userdata persona templates into the Letta MemFS"
    )
    parser.add_argument("--source", default=str(ROOT / "userdata"), help="userdata directory")
    parser.add_argument("--agent-id", help="Letta agent id")
    parser.add_argument("--memory-dir", help="override the MemFS memory directory")
    parser.add_argument("--backup-dir", help="backup root (default: ~/.letta/backups)")
    parser.add_argument("--username", help="override the username for this render")
    parser.add_argument(
        "--write-username",
        action="store_true",
        help="write the username to userdata/username.txt",
    )
    parser.add_argument("--message", default="sync persona from userdata", help="git commit message")
    parser.add_argument("--no-commit", action="store_true", help="write files without committing")
    parser.add_argument("--restart", action="store_true", help="restart akid.service after syncing")
    parser.add_argument("--json", action="store_true", help="print a JSON result")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        print(f"source directory not found: {source}", file=sys.stderr)
        return 1
    username = read_username(source, args.username)
    if args.memory_dir:
        memory_dir = Path(args.memory_dir).expanduser()
        if not memory_dir.is_dir():
            print(f"MemFS memory directory not found: {memory_dir}", file=sys.stderr)
            return 1
        agent_id = args.agent_id or "unknown"
    else:
        agent_id = resolve_agent_id(args.agent_id)
        memory_dir = resolve_memory_dir(agent_id, None)

    extras = [
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and path.relative_to(source).as_posix() not in ALLOWLIST
    ]
    metadata = {"README.md", "MANIFEST.json", USERNAME_FILE}
    unexpected = sorted(rel for rel in extras if rel not in metadata)
    private = [
        rel for rel in unexpected if rel == "human.md" or rel.startswith("profile/")
    ]
    if private:
        print(
            "warning: private-looking files are ignored: " + ", ".join(private),
            file=sys.stderr,
        )
    other = [rel for rel in unexpected if rel not in private]
    if other:
        print(
            "ignoring non-allowlisted files: " + ", ".join(other),
            file=sys.stderr,
        )

    payload: dict[str, bytes] = {}
    for rel in ALLOWLIST:
        template_path = source / rel
        if not template_path.is_file():
            print(f"missing userdata file: {template_path}", file=sys.stderr)
            return 1
        template = validate_content(
            rel, template_path.read_bytes(), allow_placeholder=True
        )
        rendered = render_template(template, username)
        payload[rel] = validate_content(rel, rendered, allow_placeholder=False)

    changed: list[str] = []
    for rel, data in payload.items():
        target = memory_dir / rel
        if target.exists() and target.read_bytes() == data:
            continue
        changed.append(rel)

    username_file_updated = False
    if not changed:
        if args.write_username and not args.dry_run:
            username_file_updated = write_username_file(source, username)
        emit(
            {
                "username": username,
                "changed": False,
                "files": [],
                "backup": None,
                "commit": None,
                "memory_dir": str(memory_dir),
                "username_file_updated": username_file_updated,
                "applies": "already_current",
            },
            json_mode=args.json,
        )
        return 0

    if args.dry_run:
        emit(
            {
                "username": username,
                "changed": True,
                "files": changed,
                "dry_run": True,
                "memory_dir": str(memory_dir),
                "applies": "next_turn",
            },
            json_mode=args.json,
        )
        return 0

    if not args.no_commit:
        try:
            staged = git(memory_dir, "diff", "--cached", "--name-only")
        except subprocess.CalledProcessError as exc:
            print(f"git failed: {exc.stderr or exc}", file=sys.stderr)
            return 1
        if staged.stdout.strip():
            print(
                "MemFS has pre-existing staged changes; commit or unstage them first",
                file=sys.stderr,
            )
            return 1

    backup_root = (
        Path(args.backup_dir).expanduser()
        if args.backup_dir
        else Path.home() / ".letta" / "backups"
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_targets(memory_dir, backup_root, changed)

    for rel in changed:
        target = memory_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(payload[rel])
        os.chmod(temp, mode)
        os.replace(temp, target)

    if args.write_username:
        username_file_updated = write_username_file(source, username)

    commit = "not committed"
    if args.no_commit:
        print(
            "warning: --no-commit leaves the MemFS revision unchanged; "
            "the system prompt may not recompile",
            file=sys.stderr,
        )
    else:
        try:
            git(memory_dir, "add", "--", *changed)
            staged = git(memory_dir, "diff", "--cached", "--quiet", check=False)
            if staged.returncode == 0:
                commit = "no changes to commit"
            else:
                git(memory_dir, "commit", "-m", args.message)
                commit = git(memory_dir, "rev-parse", "--short", "HEAD").stdout.strip()
        except subprocess.CalledProcessError as exc:
            print(f"git failed: {exc.stderr or exc}", file=sys.stderr)
            return 1

    result = {
        "username": username,
        "changed": True,
        "files": changed,
        "backup": str(backup),
        "commit": commit,
        "memory_dir": str(memory_dir),
        "username_file_updated": username_file_updated,
        "applies": "next_turn",
    }
    emit(result, json_mode=args.json)

    if args.restart:
        socket_path = Path(
            os.environ.get("AKI_SOCKET")
            or (Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "aki" / "akid.sock")
        )
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "akid.service"],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"warning: akid restart failed: {exc}", file=sys.stderr)
            return 1
        if not wait_ready(socket_path):
            print(f"akid did not become ready on {socket_path}", file=sys.stderr)
            return 1
        print(f"akid restarted and ready on {socket_path}")
    elif not args.json:
        print("changes apply on the next turn; use --restart to restart akid now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
