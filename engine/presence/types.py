"""Presence samples, trigger records, and shared clock helpers."""

from __future__ import annotations

import time
from dataclasses import (
    dataclass,
    field,
)
from datetime import (
    datetime,
    time as dt_time,
    timezone,
)
from pathlib import Path


PRESENCE_VERSION = 1

DEFAULT_POLL_SECONDS = 15

DEFAULT_IDLE_RESET_SECONDS = 300

DEFAULT_WORK_WARN_SECONDS = 7200

DEFAULT_WORK_REPEAT_SECONDS = 3600

DEFAULT_NIGHT_START = "00:30"

DEFAULT_NIGHT_END = "05:30"

DEFAULT_NIGHT_HISTORY_DAYS = 30

DEFAULT_RETURN_IDLE_SECONDS = 2700

DEFAULT_RETURN_DEBOUNCE_SECONDS = 10

MAX_SAMPLE_GAP_SECONDS = 60

INPUT_RESCAN_SECONDS = 60

EV_KEY = 0x01

EV_REL = 0x02

KEY_MAX = 0x2FF

IGNORED_KEYS = frozenset({116, 142, 143, 205})  # power/sleep/wakeup/suspend



@dataclass
class ActivitySample:
    active: bool
    valid: bool = True
    session_id: str = ""
    idle_hint: bool | None = None
    locked: bool = False
    session_active: bool = True
    idle_since: float | None = None
    sampled_at: float = field(default_factory=time.time)
    error: str | None = None



@dataclass(frozen=True)
class PresenceTrigger:
    kind: str
    facts: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.kind, "facts": dict(self.facts)}



def parse_clock(value: str, default: str) -> dt_time:
    text = str(value or default).strip() or default
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return dt_time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid clock value: {text}") from exc



def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"



def now_iso(epoch: float | None = None) -> str:
    value = datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")



class ActivityProvider:
    async def sample(self) -> ActivitySample:
        raise NotImplementedError

    def close(self) -> None:
        pass



def normalized_session_id(value: str) -> str:
    text = str(value or "").strip()
    return text



def _date_key(value: str) -> Any:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return datetime.min.date()



def render_trigger_facts(triggers: Iterable[PresenceTrigger]) -> str:
    lines = []
    for trigger in triggers:
        facts = " ".join(
            f'{key}="{value}"' for key, value in sorted(trigger.facts.items())
        )
        lines.append(f'<trigger type="{trigger.kind}" {facts}/>')
    return "\n".join(lines)
