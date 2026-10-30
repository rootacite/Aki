"""Live-context data types, token estimates, and block patterns."""

from __future__ import annotations

import re
from dataclasses import (
    dataclass,
    field,
)
from xml.sax.saxutils import (
    escape,
    quoteattr,
)
from engine.member_profile import (
    MAX_MEMBER_DESCRIPTION_UNITS,
    MEMBER_KIND_AGENT,
)


DEFAULT_BUDGET_TOKENS = 64 * 1024

MAX_HISTORY_COMMAND_CHARS = 4_000

HISTORY_READ_BYTES = 256 * 1024

LIVE_CONTEXT_TAG = "live-context"

CHANNEL_TRANSCRIPT_TAG = "channel-transcript"

CHANNEL_IDENTITY_TAG = "channel-identity"

CHANNEL_WAKE_ROUTING_TAG = "channel-wake-routing"

CHANNEL_POLICY_TAG = "channel-policy"

TURN_CONTROL_TAG = "turn-control"

CHANNEL_TURN_TAG = "channel-turn"

RUNTIME_CONTEXT_TAG = "runtime-context"

FORMAT_GUARD_TAG = "format-guard"

SOLO_MODE_TAG = "solo-mode"

SOLO_TRANSCRIPT_TAG = "solo-transcript"

SOLO_MEMORIES_TAG = "solo-memories"

DEFAULT_CHANNEL_TRANSCRIPT_BUDGET_TOKENS = 32 * 1024

DEFAULT_SOLO_TRANSCRIPT_BUDGET_TOKENS = 64 * 1024

CORE_MEMORY_PLACEHOLDER = "{CORE_MEMORY}"

MAX_CHANNEL_MEMBER_DESCRIPTION = MAX_MEMBER_DESCRIPTION_UNITS


_MARKDOWN_HEADING_BOUNDARY = r"^# [^\n]+[ \t]*$"


def _markdown_block_re(*tags: str) -> re.Pattern[str]:
    """Match one generated H1 block up to the next H1 boundary."""
    return re.compile(_markdown_block_pattern(*tags), re.S | re.M)


def _markdown_block_pattern(*tags: str) -> str:
    joined = "|".join(re.escape(tag) for tag in tags)
    return (
        rf"^# (?:{joined})\b[^\n]*\n.*?"
        rf"(?={_MARKDOWN_HEADING_BOUNDARY}|\Z)"
    )


def _framework_block_re(
    tag: str,
    legacy: str | None = None,
    *,
    markdown_tags: tuple[str, ...] = (),
) -> re.Pattern[str]:
    names = [re.escape(tag)]
    if legacy:
        names.append(re.escape(legacy))
    joined = "|".join(names)
    xml = re.compile(rf"\n*<(?:{joined})\b.*?</(?:{joined})>\n*", re.S)
    if not markdown_tags:
        return xml
    markdown = _markdown_block_re(*markdown_tags)
    return re.compile(
        rf"(?:{xml.pattern})|(?:{markdown.pattern})",
        re.S | re.M,
    )


_LIVE_CONTEXT_RE = _framework_block_re(
    LIVE_CONTEXT_TAG,
    "aki-live-context",
    markdown_tags=(LIVE_CONTEXT_TAG, RUNTIME_CONTEXT_TAG),
)

_CHANNEL_TRANSCRIPT_RE = _framework_block_re(
    CHANNEL_TRANSCRIPT_TAG,
    "aki-channel-transcript",
    markdown_tags=(CHANNEL_TRANSCRIPT_TAG,),
)

_CHANNEL_IDENTITY_RE = _framework_block_re(
    CHANNEL_IDENTITY_TAG, "aki-channel-identity"
)

_CHANNEL_WAKE_ROUTING_RE = _framework_block_re(
    CHANNEL_WAKE_ROUTING_TAG, "aki-channel-wake-routing"
)

_CHANNEL_POLICY_RE = _framework_block_re(
    CHANNEL_POLICY_TAG, "aki-channel-policy"
)

_CHANNEL_TURN_RE = _framework_block_re(CHANNEL_TURN_TAG, "aki-channel-turn")

_TURN_CONTROL_RE = re.compile(
    r"(?:\n*<(?:turn-control|channel-turn)\b.*?</(?:turn-control|channel-turn)>\n*)"
    r"|(?:" + _markdown_block_pattern(TURN_CONTROL_TAG) + r")",
    re.S | re.M,
)

_SOLO_MODE_RE = re.compile(
    r"(?:\n*<solo-mode\b.*?</solo-mode>\n*)"
    r"|(?:" + _markdown_block_pattern(SOLO_MODE_TAG) + r")",
    re.S | re.M,
)

_SOLO_TRANSCRIPT_RE = re.compile(
    r"(?:\n*<solo-transcript\b.*?</solo-transcript>\n*)"
    r"|(?:" + _markdown_block_pattern(SOLO_TRANSCRIPT_TAG) + r")",
    re.S | re.M,
)

_SOLO_MEMORIES_RE = re.compile(
    r"(?:\n*<solo-memories\b.*?</solo-memories>\n*)"
    r"|(?:" + _markdown_block_pattern(SOLO_MEMORIES_TAG) + r")",
    re.S | re.M,
)

_FORMAT_GUARD_RE = _markdown_block_re(FORMAT_GUARD_TAG)

_ZSH_EXTENDED_RE = re.compile(
    r"^: (?P<timestamp>\d+):(?P<duration>\d+);(?P<command>.*)$"
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)



@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float | None
    mem_percent: float | None



@dataclass(frozen=True)
class WindowInfo:
    title: str
    app: str | None = None
    pid: int | None = None



@dataclass(frozen=True)
class HistoryEntry:
    command: str
    timestamp: str | None = None



@dataclass(frozen=True)
class ScreenshotInfo:
    path: str
    bytes: int
    width: int | None
    height: int | None
    backend: str | None
    display: str | None
    captured_at: str



@dataclass
class LiveContextSnapshot:
    captured_at: datetime
    processes: list[ProcessInfo] = field(default_factory=list)
    window: WindowInfo | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    screenshot: ScreenshotInfo | None = None
    errors: list[str] = field(default_factory=list)

    def sections(self) -> dict[str, bool]:
        return {
            "time": True,
            "focused_window": self.window is not None,
            "processes": bool(self.processes),
            "zsh_history": bool(self.history),
            "screenshot": self.screenshot is not None,
        }



@dataclass(frozen=True)
class LiveContextOptions:
    ps_bin: str = "ps"
    qdbus_bin: str | None = None
    journalctl_bin: str = "journalctl"
    kwin_script_dir: Path | None = None
    history_path: Path | None = None
    history_limit: int = 20
    process_limit: int = 5
    timeout: float = 3.0
    screenshot_timeout: float = 2.5
    include_screenshot: bool = True



def estimate_tokens(text: str) -> int:
    """Cheap model-agnostic token estimate, conservative for CJK text."""
    ascii_chars = 0
    non_ascii_chars = 0
    for char in text:
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return (ascii_chars + 3) // 4 + non_ascii_chars



def clamp_budget_tokens(value: int | None) -> int:
    if value is None:
        return DEFAULT_BUDGET_TOKENS
    return max(1024, min(int(value), DEFAULT_BUDGET_TOKENS))



def _sanitize(value: str, *, single_line: bool = False) -> str:
    text = _CONTROL_RE.sub("?", str(value))
    if single_line:
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.replace(CORE_MEMORY_PLACEHOLDER, "{ CORE_MEMORY }")
    return text



def _attr(value: str, *, single_line: bool = True) -> str:
    return quoteattr(_sanitize(value, single_line=single_line))



def _text(value: str, *, single_line: bool = False) -> str:
    return escape(_sanitize(value, single_line=single_line))



@dataclass(frozen=True)
class ChannelMember:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    is_self: bool = False
    kind: str = MEMBER_KIND_AGENT



@dataclass(frozen=True)
class ChannelTranscriptEntry:
    seq: int
    sender: str
    sender_id: str
    text: str
    from_agent: bool = False
    timestamp: str = ""
    sent_at: int = 0
    message_id: str = ""
    phase: str = "final"
    arrived_while_busy: bool = False
    attachments: tuple["ChannelImageAttachment", ...] = ()



@dataclass(frozen=True)
class ChannelImageAttachment:
    name: str
    path: str
    mime: str = ""
    size: int = 0
    index: int = 0
    missing: bool = False



CHANNEL_WAKE_USER = "user"

CHANNEL_WAKE_MENTIONS = "mentions"

CHANNEL_WAKE_ALL = "all"



@dataclass(frozen=True)
class ChannelReplyRoute:
    text: str
    wake: str
    target_ids: tuple[str, ...] = ()



def _apply_dynamic_block(
    system: str,
    block: str,
    block_re: re.Pattern[str],
    later_res: tuple[re.Pattern[str], ...],
) -> str:
    cleaned = block_re.sub("\n", system).rstrip()
    insert_at = min(
        (match.start() for pattern in later_res if (match := pattern.search(cleaned))),
        default=None,
    )
    rendered = block.strip()
    if insert_at is None:
        return f"{cleaned}\n\n{rendered}\n"
    prefix = cleaned[:insert_at].rstrip()
    suffix = cleaned[insert_at:].lstrip("\n")
    return f"{prefix}\n\n{rendered}\n\n{suffix}\n"
