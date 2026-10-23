"""Point a local Letta backend at akid's loopback model proxy."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from engine.aki_config import AkiConfig, ModelRoute, atomic_write, load_config, set_key
from engine.execution_policy import (
    LOCK_ACQUIRE_TOOL,
    LOCK_RELEASE_TOOL,
    READ_ONLY_MCP_TOOLS,
    READ_ONLY_TOOLS,
)
from engine.tool_ownership import EXCLUSIVE_TOOLS

DEEPSEEK_HANDLE = "deepseek/deepseek-flash"
EXECUTION_LOCK_MOD_NAME = "aki-execution-lock.ts"
TOOL_OWNERSHIP_MOD_NAME = "aki-tool-ownership.ts"


def render_provider_mod(config: AkiConfig) -> str:
    models = ",\n".join(_model_literal(route) for route in config.enabled_models)
    body = models + ("," if models else "")
    base = json.dumps(config.proxy_base_url)
    provider_id = json.dumps(config.provider_id)
    display_name = json.dumps(
        "Aki" if config.profile == "aki" else f"Aki ({config.profile})"
    )
    return f"""export default function activate(letta) {{
  if (!letta.capabilities || !letta.capabilities.providers) return;
  return letta.providers.register({provider_id}, {{
    name: {display_name},
    description: "Loopback proxy for Aki profile {config.profile}",
    baseUrl: {base},
    authHeader: true,
    connect: false,
    models: [
{body}
    ],
  }});
}}
"""


def _model_literal(route: ModelRoute) -> str:
    payload: dict[str, object] = {
        "id": route.model_id,
        "name": route.model_id,
        "api": route.api,
        "reasoning": True,
        "input": list(route.inputs),
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": route.context_window,
        "maxTokens": route.max_tokens,
    }
    compat = _compat(route)
    if compat is not None:
        payload["compat"] = compat
    thinking = _thinking_level_map(route)
    if thinking is not None:
        payload["thinkingLevelMap"] = thinking
    return json.dumps(payload, indent=2).replace("\n", "\n    ")


def _compat(route: ModelRoute) -> dict[str, object] | None:
    if route.upstream_name == "deepseek" and route.api == "openai-completions":
        return {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "requiresReasoningContentOnAssistantMessages": True,
            "thinkingFormat": "deepseek",
            "supportsStrictMode": True,
        }
    if _is_xai(route) and route.api == "openai-responses":
        return {
            "supportsLongCacheRetention": False,
            "supportsStrictMode": False,
        }
    if route.upstream_name == "openrouter" and route.api == "openai-completions":
        return {
            "supportsDeveloperRole": False,
            "thinkingFormat": "openrouter",
            "supportsStrictMode": True,
        }
    if route.upstream_name == "local-llama" and route.api == "openai-completions":
        return {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "supportsStrictMode": False,
        }
    if route.upstream_name == "cli-proxy-api" and route.api == "openai-completions":
        return {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "supportsStrictMode": False,
        }
    return None


REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "low"


def supported_reasoning_efforts(route: ModelRoute | None) -> tuple[str, ...]:
    """Levels Letta will offer for this model. ``null`` map entries are excluded."""
    mapping = _thinking_level_map(route) if route is not None else None
    supported: list[str] = []
    for level in REASONING_LEVELS:
        present = mapping is not None and level in mapping
        mapped = mapping[level] if present and mapping is not None else None
        if present and mapped is None:
            continue
        if level in ("xhigh", "max") and not present:
            continue
        supported.append(level)
    return tuple(supported)


def _thinking_level_map(route: ModelRoute) -> dict[str, str | None] | None:
    if _is_xai(route) and route.api == "openai-responses":
        return {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": None,
        }
    if route.upstream_name == "openrouter" and route.api == "openai-completions":
        return {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": None,
            "max": None,
        }
    return None


def _is_xai(route: ModelRoute) -> bool:
    return route.upstream_name == "xai" or route.auth == "grok-oauth"


def write_provider_mod(letta_home: Path, config: AkiConfig) -> Path:
    path = letta_home / "mods" / config.provider_mod_name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, render_provider_mod(config))
    if config.profile == "aki":
        legacy = letta_home / "mods" / "aki-proxy.ts"
        if legacy != path and legacy.is_file():
            legacy.unlink()
    return path


def render_execution_lock_mod() -> str:
    readonly = json.dumps(sorted(READ_ONLY_TOOLS))
    readonly_mcp = json.dumps(sorted(READ_ONLY_MCP_TOOLS))
    lock_tools = json.dumps(sorted((LOCK_ACQUIRE_TOOL, LOCK_RELEASE_TOOL)))
    external_tools = json.dumps(
        sorted(
            {
                "tokifactor_send_image",
                "camera_take_photo",
                "desktop_take_screenshot",
                "wait_other",
            }
        )
    )
    exclusive_tools = json.dumps(
        sorted(tool.name for tool in EXCLUSIVE_TOOLS)
    )
    return f"""import {{ readFileSync }} from "node:fs";

const READ_ONLY = new Set({readonly});
const READ_ONLY_MCP = new Set({readonly_mcp});
const LOCK_TOOLS = new Set({lock_tools});
const EXTERNAL_TOOLS = new Set({external_tools});
const EXCLUSIVE_TOOLS = new Set({exclusive_tools});

function pidAlive(pid) {{
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {{
    process.kill(pid, 0);
    return true;
  }} catch (error) {{
    return error && error.code === "EPERM";
  }}
}}

function readHolder() {{
  const path = process.env.AKI_EXECUTION_LOCK_STATUS;
  if (!path) return null;
  try {{
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" ? value : null;
  }} catch (error) {{
    return null;
  }}
}}

function isExternalTool(toolName) {{
  return EXTERNAL_TOOLS.has(toolName) || toolName.startsWith("mcp__");
}}

function denies(toolName, phase) {{
  if (LOCK_TOOLS.has(toolName)) return;
  if (EXCLUSIVE_TOOLS.has(toolName)) return;
  if (READ_ONLY.has(toolName) || READ_ONLY_MCP.has(toolName)) return;
  if (phase === "approval" && isExternalTool(toolName)) return;
  const profile = process.env.AKI_PROFILE || "";
  const holder = readHolder();
  if (holder && holder.profile === profile && pidAlive(holder.pid)) return;
  const reason = holder
    ? `EXECUTION_LOCK_REQUIRED: the device-wide execution lock is held by ${{holder.profile || "another profile"}}. Do not retry; end this turn.`
    : "EXECUTION_LOCK_REQUIRED: call acquire_execution_lock before any non-read-only tool. Do not retry; end this turn.";
  return {{ decision: "deny", reason }};
}}

export default function activate(letta) {{
  if (!letta.capabilities || !letta.capabilities.permissions) return;
  return letta.permissions.register({{
    id: "aki-execution-lock",
    description: "Require the device-wide execution lock for non-read-only tools",
    isEnabled: () => Boolean(process.env.AKI_PROFILE),
    check: (event) => denies(event.toolName, event.phase),
  }});
}}
"""


def write_execution_lock_mod(letta_home: Path) -> Path:
    path = letta_home / "mods" / EXECUTION_LOCK_MOD_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, render_execution_lock_mod())
    return path


def render_tool_ownership_mod() -> str:
    tools = {
        tool.name: {
            "owner": tool.owner_profile,
            "label": tool.owner_label,
        }
        for tool in EXCLUSIVE_TOOLS
    }
    payload = json.dumps(tools, ensure_ascii=False, sort_keys=True, indent=2)
    return f"""const EXCLUSIVE_TOOLS = {payload};

export default function activate(letta) {{
  if (!letta.capabilities || !letta.capabilities.permissions) return;
  return letta.permissions.register({{
    id: "aki-tool-ownership",
    description: "Deny exclusive tools to agents that do not own them",
    isEnabled: () => Boolean(process.env.AKI_PROFILE),
    check: (event) => {{
      if (!Object.prototype.hasOwnProperty.call(EXCLUSIVE_TOOLS, event.toolName)) return;
      const entry = EXCLUSIVE_TOOLS[event.toolName];
      if (process.env.AKI_PROFILE === entry.owner) return;
      return {{
        decision: "deny",
        reason:
          `TOOL_OWNERSHIP_DENIED: '${{event.toolName}}' is exclusive to ` +
          `${{entry.label}}. Do not retry; hand off with '@${{entry.label}}' ` +
          `or tell the user to ask ${{entry.label}}.`,
      }};
    }},
  }});
}}
"""


def write_tool_ownership_mod(letta_home: Path) -> Path:
    path = letta_home / "mods" / TOOL_OWNERSHIP_MOD_NAME
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, render_tool_ownership_mod())
    return path


def prepare_letta(config: AkiConfig, *, backend_dir: Path, letta_home: Path) -> AkiConfig:
    """Publish this profile's loopback provider and retarget only its agent.

    The proxy itself is a separate service.  This function only prepares
    Letta configuration; it never binds a port.
    """
    auth_path = backend_dir / "providers" / "auth.json"
    file = _read_auth(auth_path)
    if config.profile == "aki":
        deepseek = file["providers"].get("deepseek")
        stored = config.upstreams.get("deepseek")
        if (stored is None or not stored.api_key) and isinstance(deepseek, dict):
            key = _api_key(deepseek)
            if key:
                set_key(config.path, "upstream.deepseek", "api_key", key)
                config = load_config(config.path, profile=config.profile)
    write_provider_mod(letta_home, config)
    write_execution_lock_mod(letta_home)
    write_tool_ownership_mod(letta_home)
    provider_id = config.provider_id
    ready = bool(config.enabled_models)
    file = _read_auth(auth_path)
    desired = _aki_record(config)
    current = file["providers"].get(provider_id)
    if isinstance(current, dict) and current.get("created_at"):
        desired["created_at"] = current["created_at"]
    remove_deepseek = config.profile == "aki" and "deepseek" in file["providers"]
    if remove_deepseek:
        _backup_auth(auth_path, letta_home)
        file = _read_auth(auth_path)
    if (
        remove_deepseek
        or not _same_proxy(current, desired)
        or (ready and provider_id not in file["providers"])
    ):
        if remove_deepseek:
            file["providers"].pop("deepseek", None)
        file["providers"][provider_id] = desired
        _write_auth(auth_path, file)
    _retarget_agent(config, backend_dir)
    return config


def _same_proxy(current: object, desired: dict) -> bool:
    if not isinstance(current, dict):
        return False
    auth = current.get("auth")
    key = auth.get("key") if isinstance(auth, dict) else None
    return key == desired["auth"]["key"] and current.get("base_url") == desired["base_url"]


def _api_key(record: dict) -> str:
    auth = record.get("auth")
    if not isinstance(auth, dict):
        return ""
    key = auth.get("key")
    return key.strip() if isinstance(key, str) else ""


def _read_auth(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "providers": {}}
    if not isinstance(data, dict):
        return {"version": 1, "providers": {}}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        data["providers"] = {}
    data["version"] = 1
    return data


def _write_auth(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, indent=2) + "\n"
    atomic_write(path, text)


def _aki_record(config: AkiConfig) -> dict:
    now = datetime.now().astimezone().isoformat()
    provider_id = config.provider_id
    return {
        "id": f"local-provider-{provider_id}",
        "name": provider_id,
        "provider_type": provider_id,
        "provider_category": "byok",
        "auth": {"type": "api", "key": config.proxy_token},
        "base_url": config.proxy_base_url,
        "created_at": now,
        "updated_at": now,
    }


def _backup_auth(auth_path: Path, letta_home: Path) -> None:
    if not auth_path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = letta_home / "backups" / f"llm-proxy-{stamp}"
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = dest / "auth.json"
    target.write_bytes(auth_path.read_bytes())
    os.chmod(target, 0o600)


def _retarget_agent(config: AkiConfig, backend_dir: Path) -> None:
    agents = backend_dir / "agents"
    if not agents.is_dir():
        return
    provider_id = config.provider_id
    for path in agents.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("id") != config.agent_id:
            continue
        model = data.get("model")
        if not isinstance(model, str) or not model:
            continue
        if model == DEEPSEEK_HANDLE:
            model_id = "deepseek-flash"
        elif "/" in model:
            current = model
            matched = False
            while "/" in current:
                prefix, remainder = current.split("/", 1)
                if not (prefix == provider_id or prefix == "aki" or prefix.startswith("aki-")):
                    break
                matched = True
                current = remainder
            if not matched or not current or "/" in current:
                continue
            model_id = current
        else:
            continue
        data["model"] = f"{provider_id}/{model_id}"
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
        settings["provider_type"] = provider_id
        data["model_settings"] = settings
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
