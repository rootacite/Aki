"""Point a local Letta backend at akid's loopback model proxy."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from engine.aki_config import AkiConfig, ModelRoute, atomic_write, load_config, set_key

DEEPSEEK_HANDLE = "deepseek/deepseek-flash"
AKI_HANDLE = "aki/deepseek-flash"


def render_provider_mod(config: AkiConfig) -> str:
    models = ",\n".join(_model_literal(route) for route in config.enabled_models)
    body = models + ("," if models else "")
    base = json.dumps(config.proxy_base_url)
    return f"""export default function activate(letta) {{
  if (!letta.capabilities || !letta.capabilities.providers) return;
  return letta.providers.register("aki", {{
    name: "Aki",
    description: "Loopback proxy owned by akid",
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
    path = letta_home / "mods" / "aki-proxy.ts"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, render_provider_mod(config))
    return path


def prepare_letta(config: AkiConfig, *, backend_dir: Path, letta_home: Path) -> AkiConfig:
    """Move a DeepSeek key out of Letta, then publish the loopback provider."""
    auth_path = backend_dir / "providers" / "auth.json"
    file = _read_auth(auth_path)
    deepseek = file["providers"].get("deepseek")
    stored = config.upstreams.get("deepseek")
    if (stored is None or not stored.api_key) and isinstance(deepseek, dict):
        key = _api_key(deepseek)
        if key:
            set_key(config.path, "upstream.deepseek", "api_key", key)
            config = load_config(config.path)
    write_provider_mod(letta_home, config)
    ready = any(route.model_id == "deepseek-flash" and route.enabled for route in config.models)
    file = _read_auth(auth_path)
    if not ready:
        if "aki" not in file["providers"]:
            _write_aki_provider(auth_path, config)
        return config
    if "deepseek" in file["providers"]:
        _backup_auth(auth_path, letta_home)
        file = _read_auth(auth_path)
    desired = _aki_record(config)
    current = file["providers"].get("aki")
    if isinstance(current, dict) and current.get("created_at"):
        desired["created_at"] = current["created_at"]
    if "deepseek" in file["providers"] or not _same_proxy(current, desired):
        file["providers"].pop("deepseek", None)
        file["providers"]["aki"] = desired
        _write_auth(auth_path, file)
    _retarget_agents(backend_dir)
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


def _write_aki_provider(path: Path, config: AkiConfig) -> None:
    file = _read_auth(path)
    file["providers"]["aki"] = _aki_record(config)
    _write_auth(path, file)


def _aki_record(config: AkiConfig) -> dict:
    now = datetime.now().astimezone().isoformat()
    return {
        "id": "local-provider-aki",
        "name": "aki",
        "provider_type": "aki",
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


def _retarget_agents(backend_dir: Path) -> None:
    agents = backend_dir / "agents"
    if not agents.is_dir():
        return
    for path in agents.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("model") != DEEPSEEK_HANDLE:
            continue
        data["model"] = AKI_HANDLE
        settings = data.get("model_settings")
        if not isinstance(settings, dict):
            settings = {}
        settings["provider_type"] = "aki"
        data["model_settings"] = settings
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
