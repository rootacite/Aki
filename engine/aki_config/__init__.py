"""Profile-aware configuration files for akid (``~/.aki/<profile>.conf``)."""

from __future__ import annotations

from .load import (
    _bool,
    _bool_or,
    _int,
    _int_or,
    _json_object,
    _list,
    _parser,
    _read_parser,
    _require,
    atomic_write,
    join_url,
    load_config,
    parse_listen,
    parse_upstream_proxy,
)
from .models import (
    AkiConfig,
    ChannelIdentity,
    ComfyUI,
    ConfigError,
    DEFAULT_AGENT_ID,
    DEFAULT_PROFILE,
    ImageGeneration,
    ModelRoute,
    PROFILE_PATTERN,
    ToolPolicy,
    Upstream,
    _APIS,
    _AUTH_PROVIDERS,
    _EXTRA_BODY_RESERVED,
    _KNOWN_SECTIONS,
    _TOOLSETS,
    default_config_path,
    default_socket_path,
    default_username_path,
    discover_profiles,
    legacy_state_dir,
    profile_config_path,
    profile_from_config_path,
    provider_id_for_profile,
    read_agent_identity,
    resolve_config_path,
    state_dir,
    validate_profile_name,
)
from .store import (
    _format_value,
    _migrate_legacy,
    _move_legacy,
    ensure_config,
    ensure_proxy_token,
    read_mcp_excluded,
    read_mqtt,
    read_writable,
    set_key,
    sync_channel_identity,
    write_mcp_excluded,
    write_mqtt,
    write_writable,
)
from .template import (
    _haru_template_text,
    _template_for,
    template_text,
)
import configparser
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import (
    dataclass,
    field,
)
from pathlib import (
    Path,
)
from urllib.parse import (
    urlsplit,
)
from engine.member_profile import (
    truncate_member_description,
)
