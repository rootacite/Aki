"""Logind-backed user activity, work-duration, late-night, and return triggers."""

from __future__ import annotations

from .evdev import (
    EvdevActivityProbe,
    _capability_bits,
    _eviocgbit,
    _has_bit,
    _is_user_input_device,
)
from .logind import (
    LogindActivityProvider,
    _run,
    _usec_to_epoch,
)
from .monitor import (
    PresenceMonitor,
)
from .types import (
    ActivityProvider,
    ActivitySample,
    DEFAULT_IDLE_RESET_SECONDS,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_HISTORY_DAYS,
    DEFAULT_NIGHT_START,
    DEFAULT_POLL_SECONDS,
    DEFAULT_RETURN_DEBOUNCE_SECONDS,
    DEFAULT_RETURN_IDLE_SECONDS,
    DEFAULT_WORK_REPEAT_SECONDS,
    DEFAULT_WORK_WARN_SECONDS,
    EV_KEY,
    EV_REL,
    IGNORED_KEYS,
    INPUT_RESCAN_SECONDS,
    KEY_MAX,
    MAX_SAMPLE_GAP_SECONDS,
    PRESENCE_VERSION,
    PresenceTrigger,
    _date_key,
    boot_id,
    normalized_session_id,
    now_iso,
    parse_clock,
    render_trigger_facts,
)
import asyncio
import array
import contextlib
import errno
import fcntl
import json
import logging
import os
import select
import time
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    time as dt_time,
    timedelta,
    timezone,
)
from pathlib import (
    Path,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
)
from engine.aki_config import (
    atomic_write,
)
