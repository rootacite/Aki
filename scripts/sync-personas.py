#!/usr/bin/env python3
"""Sync every profile's userdata templates into MemFS and refresh live prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
    profile_config_path,
    validate_profile_name,
)


SYNC_PERSONA = ROOT / "scripts" / "sync-persona.py"
SAFECTL = ROOT / "aki-safectl.py"
AKICTL = ROOT / "bin" / "akictl"
MANIFEST_NAME = "MANIFEST.json"
ALLOWLIST = (
    "persona.md",
    "memory-policy.md",
    "character/MEMORY.md",
    "character/voice.md",
    "character/relationship.md",
    "character/boundaries.md",
    "character/examples.md",
)


class SyncError(RuntimeError):
    """The automated sync cannot continue safely."""


def _info(message: str) -> None:
    print(f"[sync-personas] {message}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run(command: list[object], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _selected_profiles(requested: list[str]) -> list[str]:
    discovered = discover_profiles()
    if not requested:
        if not discovered:
            raise SyncError("no profiles found under ~/.aki")
        return list(discovered)

    names: list[str] = []
    for raw in requested:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                name = validate_profile_name(part)
            except ConfigError as exc:
                raise SyncError(f"invalid profile {part!r}: {exc}") from exc
            if name not in discovered and not profile_config_path(name).is_file():
                raise SyncError(f"profile {name} not found under ~/.aki")
            names.append(name)
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _source_dir(config) -> Path:
    path = Path(config.userdata or "userdata").expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _sync_one(profile: str, source: Path, config, *, dry_run: bool) -> dict:
    command: list[object] = [
        sys.executable,
        SYNC_PERSONA,
        "--source",
        source,
        "--agent-id",
        config.agent_id,
        "--username",
        config.username,
        "--json",
    ]
    if dry_run:
        command.append("--dry-run")
    result = _run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(f"sync-persona failed: {detail[-2000:]}" if detail else "sync-persona failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"sync-persona returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SyncError("sync-persona returned a non-object JSON payload")
    return payload


def _full_commit(memory_dir: str) -> str:
    result = _run(["git", "-C", memory_dir, "rev-parse", "HEAD"], timeout=30.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(f"cannot resolve MemFS commit: {detail[-1000:]}")
    return result.stdout.strip()


def _manifest_payload(source: Path, username: str, commit: str) -> dict:
    templates: dict[str, dict] = {}
    rendered: dict[str, dict] = {}
    for rel in ALLOWLIST:
        raw = (source / rel).read_bytes()
        rendered_bytes = raw.decode("utf-8").replace("{{USERNAME}}", username).encode("utf-8")
        templates[rel] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        rendered[rel] = {
            "sha256": hashlib.sha256(rendered_bytes).hexdigest(),
            "bytes": len(rendered_bytes),
        }
    return {
        "memfs_commit": commit,
        "username": username,
        "synced_at": _now_iso(),
        "templates": templates,
        "rendered": rendered,
    }


def _update_manifest(source: Path, username: str, commit: str) -> bool:
    path = source / MANIFEST_NAME
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SyncError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncError(f"{path} is not a JSON object")
    data.update(_manifest_payload(source, username, commit))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _gateway_active(profile: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", f"akid@{profile}.service"],
        text=True,
        capture_output=True,
        check=False,
        timeout=15.0,
    )
    return result.returncode == 0


def _start_profile(profile: str) -> None:
    if not SAFECTL.is_file():
        raise SyncError(f"safe start script not found: {SAFECTL}")
    result = _run([sys.executable, SAFECTL, "start", profile], timeout=120.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(f"start {profile} failed: {detail[-2000:]}")


def _recompile(profile: str) -> dict:
    if not AKICTL.is_file():
        raise SyncError(f"akictl not found: {AKICTL}")
    result = _run([AKICTL, profile, "recompile"], timeout=60.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SyncError(f"recompile {profile} failed: {detail[-2000:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _print_row(row: dict) -> None:
    profile = row["profile"]
    if row["error"]:
        _info(f"{profile}: error: {row['error']}")
        return
    if row["changed"]:
        files = ", ".join(row["files"]) or "(none)"
        verb = "would sync" if row["runtime"] == "dry-run" else "synced"
        _info(f"{profile}: {verb} {len(row['files'])} file(s): {files}")
        if row["commit"]:
            _info(f"{profile}: MemFS commit {row['commit']}")
        if row["manifest_updated"]:
            _info(f"{profile}: manifest updated")
    else:
        _info(f"{profile}: already in sync")
    runtime = row["runtime"]
    if runtime == "recompiled":
        _info(f"{profile}: running gateway recompiled")
    elif runtime == "started+recompiled":
        _info(f"{profile}: gateway started and recompiled")
    elif runtime == "stopped":
        _info(f"{profile}: gateway stopped; changes apply on next start")
    elif runtime == "dry-run":
        _info(f"{profile}: dry run; no writes or runtime changes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profiles", nargs="*", help="profile names (default: all discovered)")
    parser.add_argument("--profile", action="append", default=[], help="profile name; may be repeated")
    parser.add_argument("--dry-run", action="store_true", help="show pending syncs without writing")
    parser.add_argument(
        "--start",
        action="store_true",
        help="start stopped profiles that have changes, then recompile them",
    )
    parser.add_argument("--json", action="store_true", help="emit a machine-readable summary")
    args = parser.parse_args(argv)

    requested = [*args.profile, *args.profiles]
    try:
        profiles = _selected_profiles(requested)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    discovered = discover_profiles()
    rows: list[dict] = []
    failures = 0
    changed_count = 0
    runtime_refreshed = 0

    for profile in profiles:
        row = {
            "profile": profile,
            "source": None,
            "changed": False,
            "files": [],
            "commit": None,
            "manifest_updated": False,
            "runtime": "skipped",
            "error": None,
        }
        try:
            path = discovered.get(profile) or profile_config_path(profile)
            config = load_config(path, profile=profile)
            source = _source_dir(config)
            row["source"] = str(source)
            if not source.is_dir():
                raise SyncError(f"userdata directory not found: {source}")

            payload = _sync_one(profile, source, config, dry_run=args.dry_run)
            row["changed"] = bool(payload.get("changed"))
            row["files"] = list(payload.get("files") or [])
            row["commit"] = payload.get("commit")

            if args.dry_run:
                row["runtime"] = "dry-run"
            elif row["changed"]:
                changed_count += 1
                commit = payload.get("commit")
                memory_dir = payload.get("memory_dir")
                if not isinstance(commit, str) or not commit or commit in {"not committed", "no changes to commit"}:
                    raise SyncError("sync-persona reported changes without a commit")
                if not isinstance(memory_dir, str) or not memory_dir:
                    raise SyncError("sync-persona did not report its MemFS directory")
                full_commit = _full_commit(memory_dir)
                row["commit"] = full_commit
                row["manifest_updated"] = _update_manifest(source, config.username, full_commit)

                if _gateway_active(profile):
                    _recompile(profile)
                    row["runtime"] = "recompiled"
                    runtime_refreshed += 1
                elif args.start:
                    _start_profile(profile)
                    _recompile(profile)
                    row["runtime"] = "started+recompiled"
                    runtime_refreshed += 1
                else:
                    row["runtime"] = "stopped"
            else:
                row["runtime"] = "unchanged"
        except (SyncError, ConfigError, OSError, subprocess.TimeoutExpired) as exc:
            row["error"] = str(exc)
            failures += 1

        rows.append(row)
        if not args.json:
            _print_row(row)

    summary = {
        "profiles": rows,
        "changed": changed_count,
        "failures": failures,
        "runtime_refreshed": runtime_refreshed,
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.dry_run:
        pending = sum(1 for row in rows if row["changed"])
        _info(f"summary: {pending} pending, {failures} failed (dry run)")
    else:
        _info(
            f"summary: {changed_count} changed, {failures} failed, "
            f"{runtime_refreshed} runtime refreshed"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
