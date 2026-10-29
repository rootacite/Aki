#!/usr/bin/env python3
"""One-time migration from persisted Aki runtime prompt blocks to overlays."""

from __future__ import annotations

import argparse
import configparser
import importlib.util
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PROFILES = {
    "aki": ("aki.conf", "aki-system.md"),
    "fuyu": ("fuyu.conf", "fuyu-system.md"),
    "haru": ("haru.conf", "haru-system.md"),
}
RUNTIME_TAGS = (
    "tool-ownership",
    "channel-policy",
    "channel-wake-routing",
    "channel-identity",
    "channel-turn",
    "channel-transcript",
    "solo-mode",
    "solo-transcript",
    "solo-memories",
    "shared-memory",
    "live-context",
)


def load_installer() -> Any:
    path = ROOT / "scripts" / "install-system-prompt.py"
    spec = importlib.util.spec_from_file_location("aki_install_system_prompt", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_profile(config_path: Path) -> tuple[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    if not agent_id:
        raise RuntimeError(f"missing [gateway] agent_id in {config_path}")
    return agent_id, parser.get("gateway", "conversation_id", fallback="default").strip() or "default"


def runtime_tags_in(system: str) -> list[str]:
    return [
        tag
        for tag in RUNTIME_TAGS
        if re.search(rf"(?m)^[ \t]*<(?:aki-)?{re.escape(tag)}(?:\s|>)", system)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".aki")
    parser.add_argument(
        "--backend-dir",
        type=Path,
        default=Path.home() / ".letta" / "lc-local-backend",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".letta" / "settings.json",
    )
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument(
        "--profile",
        action="append",
        choices=sorted(PROFILES),
        help="profile to migrate; repeatable (default: all)",
    )
    args = parser.parse_args(argv)

    profiles = args.profile or sorted(PROFILES)
    installer = load_installer()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = args.backup_root or (
        Path.home() / f"aki-overlay-migration-backup-{stamp}"
    )
    if args.apply:
        backup_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        for source, name in (
            (args.config_dir.expanduser(), "aki"),
            (args.backend_dir.expanduser(), "letta-backend"),
        ):
            if source.is_dir():
                shutil.copytree(source, backup_root / name, symlinks=True)

    results: list[dict[str, Any]] = []
    for profile in profiles:
        config_name, prompt_name = PROFILES[profile]
        config_path = args.config_dir.expanduser() / config_name
        if not config_path.is_file():
            results.append({"profile": profile, "skipped": "missing config"})
            continue
        agent_id, conversation_id = read_profile(config_path)
        prompt_path = ROOT / "prompts" / prompt_name
        try:
            result = installer.install(
                prompt_path=prompt_path,
                backend_dir=args.backend_dir.expanduser(),
                agent_id=agent_id,
                conversation_id=conversation_id,
                settings_path=args.settings.expanduser(),
                backup_dir=backup_root / "system-prompt-installs"
                if args.apply
                else Path.home() / ".letta" / "backups",
                dry_run=not args.apply,
            )
        except Exception as exc:
            result = {"profile": profile, "error": str(exc)}
        result["profile"] = profile
        results.append(result)

    if args.apply:
        for item in results:
            if item.get("error"):
                continue
            agent_path = Path(str(item.get("agent_path") or ""))
            if not agent_path.is_file():
                continue
            agent = json.loads(agent_path.read_text(encoding="utf-8"))
            tags = runtime_tags_in(str(agent.get("system") or ""))
            item["remaining_runtime_tags"] = tags

    print(json.dumps({"apply": args.apply, "backup_root": str(backup_root), "results": results}, ensure_ascii=False, indent=2))
    if args.apply:
        print(
            "Restart each akid or run its `akictl <profile> recompile` so Letta "
            "recompiles the conversation without the removed blocks.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
