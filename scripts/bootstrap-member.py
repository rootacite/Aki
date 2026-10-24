#!/usr/bin/env python3
"""Idempotently scaffold a new member profile, agent, MemFS, and prompt.

The script is intentionally conservative: when the target agent or conversation
already exists it makes no changes. That prevents an accidental bootstrap from
rewriting an existing member's session, memory, or prompt.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.aki_config import (  # noqa: E402
    ConfigError,
    atomic_write,
    ensure_proxy_token,
    load_config,
    profile_config_path,
    sync_channel_identity,
    template_text,
)
from engine.member_identity import ensure_manifest  # noqa: E402

DEFAULT_PROFILE = "haru"
DEFAULT_SOURCE = ROOT / "userdata3"
DEFAULT_PROMPT = ROOT / "prompts" / "haru-system.md"
DEFAULT_MODEL_ID = "gemini-3.8-flash"
USERNAME_PLACEHOLDER = "{{USERNAME}}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def default_backend_dir() -> Path:
    configured = os.environ.get("AKI_LETTA_BACKEND_DIR") or os.environ.get(
        "LETTA_LOCAL_BACKEND_DIR"
    )
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "lc-local-backend"


def default_settings_path() -> Path:
    configured = os.environ.get("LETTA_SETTINGS_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta" / "settings.json"


def default_letta_home() -> Path:
    configured = os.environ.get("LETTA_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".letta"


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return data


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def find_agent_file(backend_dir: Path, agent_id: str) -> Path | None:
    folder = backend_dir / "agents"
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json")):
        try:
            data = read_json(path)
        except (OSError, ValueError, RuntimeError):
            continue
        if data.get("id") == agent_id:
            return path
    return None


def conversation_dir(backend_dir: Path, conversation_id: str, agent_id: str) -> Path:
    return backend_dir / "conversations" / b64url(f"{conversation_id}:{agent_id}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_template(path: Path, username: str) -> str:
    return path.read_text(encoding="utf-8").replace(USERNAME_PLACEHOLDER, username)


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    match = re.match(r"^---\n.*?\n---\n?", text, re.S)
    return text[match.end() :] if match else text


def initial_core_memory(source: Path, username: str) -> str:
    parts: list[str] = []
    for rel, tag in (("persona.md", "persona"), ("memory-policy.md", "memory-policy")):
        path = source / rel
        if not path.is_file():
            continue
        text = strip_frontmatter(render_template(path, username)).strip()
        parts.append(f"<{tag}>\n{text}\n</{tag}>")
    index = source / "MEMORY.md"
    if index.is_file():
        parts.append(f"<memory>\n{render_template(index, username).strip()}\n</memory>")
    return "\n\n".join(parts) + "\n"


def initial_memory_index() -> str:
    return """# Memory\n\n## Core\n\n- [persona.md](persona.md) — 稳定的 Haru 人格：创意、活泼、爱恶作剧的妹妹。\n- [memory-policy.md](memory-policy.md) — 压缩上下文时如何提炼长期记忆。\n\n## On demand\n\n- [character/MEMORY.md](character/MEMORY.md) — 语气、关系、示例与边界。\n"""


def run_git(memory_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(memory_dir), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def initialize_memory(memory_dir: Path, agent_id: str) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (memory_dir / ".git").is_dir():
        run_git(memory_dir, "init", "-b", "main")
        run_git(memory_dir, "config", "user.name", "Haru")
        run_git(memory_dir, "config", "user.email", f"{agent_id}@letta.com")
        run_git(memory_dir, "config", "commit.gpgsign", "false")
        run_git(memory_dir, "config", "letta.agentId", agent_id)
        policy = memory_dir / ".git" / "letta-memory-layout-policy"
        policy.write_text("root-marker\n", encoding="utf-8")
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        atomic_write(index, initial_memory_index())
    staged = run_git(memory_dir, "diff", "--cached", "--name-only", check=False)
    if not staged.stdout.strip():
        run_git(memory_dir, "add", "--", "MEMORY.md")
        if run_git(memory_dir, "diff", "--cached", "--quiet", check=False).returncode != 0:
            run_git(memory_dir, "commit", "-m", "bootstrap Haru memory index")


def create_conversation(
    backend_dir: Path,
    conversation_id: str,
    agent_id: str,
    prompt: str,
    core_memory: str,
) -> Path:
    target = conversation_dir(backend_dir, conversation_id, agent_id)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = now_iso()
    manifest = target / "manifest.json"
    if not manifest.exists():
        atomic_json(
            manifest,
            {
                "schema_version": 2,
                "message_format": "pi-session-entry-jsonl",
                "provider_stack": "pi-ai",
                "created_at": timestamp,
            },
        )
    conversation = target / "conversation.json"
    if not conversation.exists():
        atomic_json(
            conversation,
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
        )
    messages = target / "messages.jsonl"
    if not messages.exists():
        session = {
            "type": "session",
            "version": 3,
            "id": conversation_id,
            "timestamp": timestamp,
            "cwd": str(ROOT),
        }
        atomic_write(messages, json.dumps(session, separators=(",", ":")) + "\n")
    system_prompt = target / "system-prompt.json"
    if not system_prompt.exists():
        content = prompt.replace("{CORE_MEMORY}", core_memory.rstrip()).rstrip() + "\n"
        atomic_json(
            system_prompt,
            {
                "content": content,
                "coreMemory": core_memory,
                "compiledAt": timestamp,
                "rawSystemHash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            },
        )
    return target


def create_agent(
    backend_dir: Path,
    agent_id: str,
    *,
    name: str,
    description: str,
    prompt: str,
    model: str,
    context_window: int,
    max_tokens: int,
) -> Path:
    path = backend_dir / "agents" / f"{b64url(agent_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(
        path,
        {
            "id": agent_id,
            "name": name,
            "description": description,
            "system": prompt.strip(),
            "tags": ["origin:letta-code", "git-memory-enabled", "profile:haru"],
            "model": model,
            "model_settings": {
                "parallel_tool_calls": True,
                "provider_type": model.split("/", 1)[0],
                "context_window_limit": context_window,
                "max_tokens": max_tokens,
            },
        },
    )
    os.chmod(path, 0o600)
    return path


def update_settings(path: Path, agent_id: str, backend_dir: Path) -> bool:
    settings: dict[str, Any]
    try:
        settings = read_json(path)
    except (OSError, ValueError, RuntimeError):
        settings = {}
    agents = settings.get("agents")
    if not isinstance(agents, list):
        agents = []
        settings["agents"] = agents
    for item in agents:
        if isinstance(item, dict) and item.get("agentId") == agent_id:
            return False
    agents.append(
        {
            "agentId": agent_id,
            "baseUrl": f"local:{backend_dir}",
            "systemPromptPreset": "custom",
            "memfs": True,
        }
    )
    atomic_json(path, settings)
    return True


def bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    profile = args.profile
    config_path = Path(args.config).expanduser() if args.config else profile_config_path(profile)
    backend_dir = Path(args.backend_dir).expanduser() if args.backend_dir else default_backend_dir()
    settings_path = Path(args.settings).expanduser() if args.settings else default_settings_path()
    source = Path(args.source).expanduser() if args.source else DEFAULT_SOURCE
    prompt_path = Path(args.prompt).expanduser() if args.prompt else DEFAULT_PROMPT
    result: dict[str, Any] = {
        "profile": profile,
        "config": str(config_path),
        "backend_dir": str(backend_dir),
        "settings": str(settings_path),
        "source": str(source),
        "dry_run": bool(args.dry_run),
    }

    if config_path.is_file():
        try:
            existing = load_config(config_path, profile=profile)
        except (OSError, ValueError, ConfigError) as exc:
            raise SystemExit(f"cannot read existing profile {config_path}: {exc}") from exc
        agent_id = existing.agent_id
        conversation_id = existing.conversation
        username = existing.username or args.username
        result["config_created"] = False
    else:
        agent_id = args.agent_id or f"agent-local-{uuid.uuid4()}"
        conversation_id = args.conversation or "default"
        username = args.username
        result["config_created"] = True

    result.update(
        {
            "agent_id": agent_id,
            "conversation": conversation_id,
            "username": username,
        }
    )
    existing_agent = find_agent_file(backend_dir, agent_id)
    existing_conversation = conversation_dir(backend_dir, conversation_id, agent_id)
    if existing_agent is not None or existing_conversation.exists():
        result.update(
            {
                "status": "exists",
                "changed": False,
                "agent_file": str(existing_agent) if existing_agent else None,
                "conversation_dir": str(existing_conversation) if existing_conversation.exists() else None,
            }
        )
        return result

    if args.dry_run:
        result.update({"status": "would_create", "changed": False})
        return result

    if not config_path.exists():
        atomic_write(config_path, template_text(agent_id, profile=profile))
    ensure_proxy_token(config_path)
    config = load_config(config_path, profile=profile)
    username = config.username or username

    if not source.is_dir():
        raise SystemExit(f"userdata source not found: {source}")
    if not prompt_path.is_file():
        raise SystemExit(f"system prompt not found: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if prompt.count("{CORE_MEMORY}") != 1:
        raise SystemExit(f"{prompt_path} must contain {{CORE_MEMORY}} exactly once")

    identity, manifest_path = ensure_manifest(
        source,
        profile=profile,
        agent_id=agent_id,
        fallback_name=config.channel.name or profile.title(),
        fallback_aliases=config.channel.aliases,
        userdata_label=config.userdata,
    )
    sync_channel_identity(
        config_path,
        member_id=identity.id,
        name=identity.name,
        aliases=identity.aliases,
    )
    config = load_config(config_path, profile=profile)

    memory_dir = backend_dir / "memfs" / agent_id / "memory"
    memory_worktrees = backend_dir / "memfs" / agent_id / "memory-worktrees"
    memory_worktrees.mkdir(parents=True, exist_ok=True, mode=0o700)
    initialize_memory(memory_dir, agent_id)

    sync_persona = load_module("aki_sync_persona", ROOT / "scripts" / "sync-persona.py")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        sync_status = sync_persona.main(
            [
                "--source",
                str(source),
                "--agent-id",
                agent_id,
                "--memory-dir",
                str(memory_dir),
                "--username",
                username,
                "--backup-dir",
                str(Path(args.letta_home).expanduser() / "backups")
                if args.letta_home
                else str(default_letta_home() / "backups"),
                "--message",
                "bootstrap Haru persona",
                "--json",
            ]
        )
    if sync_status != 0:
        raise SystemExit("persona sync failed")

    core_memory = initial_core_memory(source, username)
    if not core_memory.strip():
        raise SystemExit("initial core memory is empty")
    model_route = next(
        (route for route in config.models if route.model_id == DEFAULT_MODEL_ID),
        None,
    )
    context_window = model_route.context_window if model_route else 1000000
    max_tokens = model_route.max_tokens if model_route else 65536
    create_agent(
        backend_dir,
        agent_id,
        name=config.channel.name,
        description=config.channel.description,
        prompt=prompt,
        model=f"{config.provider_id}/{DEFAULT_MODEL_ID}",
        context_window=context_window,
        max_tokens=max_tokens,
    )
    target = create_conversation(
        backend_dir, conversation_id, agent_id, prompt, core_memory
    )
    settings_changed = update_settings(settings_path, agent_id, backend_dir)

    install_prompt = load_module(
        "aki_install_system_prompt", ROOT / "scripts" / "install-system-prompt.py"
    )
    install_result = install_prompt.install(
        prompt_path=prompt_path,
        backend_dir=backend_dir,
        agent_id=agent_id,
        conversation_id=conversation_id,
        settings_path=settings_path,
        backup_dir=(Path(args.letta_home).expanduser() / "backups")
        if args.letta_home
        else default_letta_home() / "backups",
    )
    result.update(
        {
            "status": "created",
            "changed": True,
            "identity": identity.to_dict(),
            "identity_manifest": str(manifest_path),
            "memory_dir": str(memory_dir),
            "conversation_dir": str(target),
            "agent_file": str(backend_dir / "agents" / f"{b64url(agent_id)}.json"),
            "settings_changed": settings_changed,
            "system_prompt_changed": install_result.get("changed", False),
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--config", help="profile config path (default: ~/.aki/<profile>.conf)")
    parser.add_argument("--agent-id", help="agent id for a new profile")
    parser.add_argument("--conversation", default="default")
    parser.add_argument("--username", default="哥哥")
    parser.add_argument("--source", help="userdata template directory (default: userdata3)")
    parser.add_argument("--prompt", help="system prompt path (default: prompts/haru-system.md)")
    parser.add_argument("--backend-dir", help="Letta local backend directory")
    parser.add_argument("--letta-home", help="Letta home (mods, backups)")
    parser.add_argument("--settings", help="Letta settings.json path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = bootstrap(args)
    except (OSError, ValueError, RuntimeError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("status") == "exists":
        print(f"agent/conversation already exists; no changes: {result['agent_id']}")
    elif result.get("status") == "would_create":
        print(f"would create profile/agent/conversation: {result['agent_id']}")
    else:
        print(f"created Haru profile and agent: {result['agent_id']}")
        print(f"identity: {result['identity']['id']}")
        print(f"memory: {result['memory_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
