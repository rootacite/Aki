"""akictl: chat and local-socket management for Aki."""

from __future__ import annotations

import builtins

print = builtins.print

from .chat import (
    _channel_history_file,
    _chat,
    _connect,
    _mqtt_chat,
    _mqtt_chat_once,
    _print_channel_history,
    _print_history_file,
    _publish_mqtt_cancel,
    _unique_user_identity,
    repl,
)
from .cli import (
    ALL_COMMANDS,
    COMMANDS,
    HELP_TEXT,
    SERVICE_COMMANDS,
    _add_common,
    _chat_parser,
    _compact,
    _extract_profile,
    _history,
    _identity,
    _management,
    _mcp,
    _memory,
    _model,
    _mqtt,
    _print_help,
    _reasoning,
    build_parsers,
    main,
)
from .service import (
    _agent_exists,
    _conversation_exists,
    _gateway_unit,
    _health_ready,
    _port_in_use,
    _preflight_profile,
    _profile_config,
    _profile_down,
    _profile_logs,
    _profile_restart,
    _profile_up,
    _profiles,
    _proxy_unit,
    _service_status,
    _socket_conflict,
    _systemctl,
    _unit_active,
    _wait_port,
)
from .solo import (
    _clear_solo_control_state,
    _default_solo_config,
    _latest_solo_event,
    _read_solo_control_state,
    _solo,
    _solo_broker_call,
    _solo_control_state_path,
    _solo_target,
    _write_solo_control_state,
)
from .support import (
    _b64url,
    _mcp_call,
    _model_call,
    _mqtt_call,
    _parse_mqtt_server,
    _print_error,
    _reasoning_call,
    _rpc_call,
    _socket_from,
)
import argparse
import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import (
    Path,
)
from typing import (
    Any,
)
from engine.aki_config import (
    ConfigError,
    discover_profiles,
    load_config,
    profile_config_path,
    resolve_config_path,
    validate_profile_name,
)
from engine.client import (
    GatewayClient,
    _memory_command,
    _print_json,
    default_socket_path,
)
from engine.channel_history import (
    channel_history_key,
    channel_history_path,
    read_channel_history,
)
from engine.letta_compact import (
    COMPACT_MODES,
)
from engine.member_identity import (
    IdentityRegistry,
    legacy_member_id,
)
from engine.protocol import (
    RpcError,
)
from engine.solo_mode import (
    SOLO_ACTIVE,
    SOLO_END,
    SOLO_START,
)
