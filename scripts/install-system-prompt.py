#!/usr/bin/env python3
"""Install Aki's static base system prompt into the local Letta agent."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = ROOT / "prompts" / "aki-system.md"
DEFAULT_AGENT_ID = "agent-local-d9c5a47d-1c4e-4a21-a9ae-a93beb462efc"
CORE_PLACEHOLDER = "{CORE_MEMORY}"


def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def default_settings_path() -> Path:
    configured = os.environ.get("LETTA_SETTINGS_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "settings.json"


def default_agent_id() -> str:
    configured = os.environ.get("AKI_AGENT_ID")
    if configured:
        return configured
    path = Path(os.environ.get("AKI_CONFIG", Path.home() / ".aki" / "aki.conf")).expanduser()
    if path.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
            configured = parser.get("gateway", "agent_id", fallback="").strip()
        except (OSError, configparser.Error):
            configured = ""
        if configured:
            return configured
    return DEFAULT_AGENT_ID


def find_agent_file(backend_dir: Path, agent_id: str) -> Path:
    for path in sorted((backend_dir / "agents").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == agent_id:
            return path
    raise RuntimeError(f"agent not found: {agent_id}")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return data


def atomic_json(path: Path, payload: dict[str, Any], *, bump_mtime: bool = False) -> None:
    previous_mtime_ns = 0
    if bump_mtime:
        try:
            previous_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            previous_mtime_ns = 0
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        if bump_mtime:
            now_ns = max(
                datetime.now(timezone.utc).timestamp() * 1_000_000_000,
                previous_mtime_ns + 1_000_000,
            )
            os.utime(path, ns=(int(now_ns), int(now_ns)))
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def load_prompt(path: Path) -> str:
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read prompt: {exc}") from exc
    if not prompt:
        raise RuntimeError("prompt is empty")
    if prompt.count(CORE_PLACEHOLDER) != 1:
        raise RuntimeError(f"prompt must contain {CORE_PLACEHOLDER} exactly once")
    return prompt


def backup_agent(agent_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"system-prompt-install-{stamp}"
    suffix = 1
    while target.exists():
        target = backup_dir / f"system-prompt-install-{stamp}-{suffix}"
        suffix += 1
    target.mkdir(mode=0o700)
    shutil.copy2(agent_path, target / "agent.json")
    return target


def update_settings(path: Path, agent_id: str) -> bool:
    if not path.is_file():
        return False
    settings = read_json(path)
    agents = settings.get("agents")
    if not isinstance(agents, list):
        return False
    changed = False
    for item in agents:
        if not isinstance(item, dict) or item.get("agentId") != agent_id:
            continue
        if item.get("systemPromptPreset") != "custom":
            item["systemPromptPreset"] = "custom"
            changed = True
        for key in ("systemPromptHash", "systemPromptVersion"):
            if key in item:
                del item[key]
                changed = True
    if changed:
        atomic_json(path, settings)
    return changed


def install(
    *,
    prompt_path: Path,
    backend_dir: Path,
    agent_id: str,
    conversation_id: str,
    settings_path: Path,
    backup_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    prompt = load_prompt(prompt_path)
    agent_path = find_agent_file(backend_dir, agent_id)
    agent = read_json(agent_path)
    old_system = agent.get("system")
    if not isinstance(old_system, str):
        raise RuntimeError("agent has no system prompt")
    new_system = prompt.rstrip() + "\n"
    changed = new_system != old_system
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "agent_id": agent_id,
        "conversation_id": conversation_id,
        "agent_path": str(agent_path),
        "prompt_path": str(prompt_path),
        "old_base_chars": len(old_system),
        "new_base_chars": len(prompt),
        "agent_changed": changed,
        "changed": changed,
    }
    if not changed:
        return result
    if dry_run:
        return result

    backup_path = backup_agent(agent_path, backup_dir)
    agent["system"] = new_system
    atomic_json(agent_path, agent, bump_mtime=True)
    result["backup_path"] = str(backup_path)
    result["settings_updated"] = update_settings(settings_path, agent_id)
    return result


def status(prompt_path: Path, backend_dir: Path, agent_id: str) -> dict[str, Any]:
    prompt = load_prompt(prompt_path)
    agent_path = find_agent_file(backend_dir, agent_id)
    agent = read_json(agent_path)
    system = str(agent.get("system") or "")
    return {
        "agent_id": agent_id,
        "agent_path": str(agent_path),
        "in_sync": system.strip() == prompt,
        "base_chars": len(system),
        "prompt_chars": len(prompt),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--backend-dir", type=Path, default=default_backend_dir())
    parser.add_argument("--agent-id", default=default_agent_id())
    parser.add_argument("--conversation-id", default="default")
    parser.add_argument("--settings", type=Path, default=default_settings_path())
    parser.add_argument("--backup-dir", type=Path, default=Path.home() / ".letta" / "backups")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.status:
            result = status(args.prompt.expanduser(), args.backend_dir.expanduser(), args.agent_id)
        else:
            result = install(
                prompt_path=args.prompt.expanduser(),
                backend_dir=args.backend_dir.expanduser(),
                agent_id=args.agent_id,
                conversation_id=args.conversation_id,
                settings_path=args.settings.expanduser(),
                backup_dir=args.backup_dir.expanduser(),
                dry_run=args.dry_run,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
