"""Parse channel prompts, speaker sentences, and roster text."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    MEMBER_KIND_USER,
    canonical_name,
)


CHANNEL_PROMPT_HEADER = "[akid MQTT channel message]"

USERNAME_PLACEHOLDER = "${{USERNAME}}"

LEGACY_USERNAME_PLACEHOLDER = "{{USERNAME}}"

SPEAKER_SENTENCE_TEMPLATE = f"当前正在和你说话的是 {USERNAME_PLACEHOLDER}。"

CHANNEL_CONTEXT_RE = re.compile(
    r"(?ms)^[ \t]*<channel[-_]context\b[^>]*>.*?</channel[-_]context>[ \t]*(?:\r?\n)?"
)

WAKE_RE = re.compile(r"(?m)^wake-reminder:[ \t]*(?P<text>.*)$")

MESSAGE_TIME_RE = re.compile(r"(?m)^message-time:[ \t]*.*$")

SENDER_RE = re.compile(r"(?m)^sender:[ \t]*(?P<value>.*)$")

SEQ_RE = re.compile(r"(?m)^seq:[ \t]*(?P<seq>\d+)[ \t]*$")

SPEAKER_PREFIX_RE = re.compile(
    r"^当前正在和你说话的是 .+?(?:（(?:用户|智能体|成员)）)?。"
)

SPEAKER_IDENTITY_RE = re.compile(
    r"^当前正在和你说话的是 (?P<identity>.+?)(?:（(?P<label>用户|智能体|成员)）)?。"
)

MEMBER_TAG_RE = re.compile(r"(?ms)<member\b(?P<attrs>[^>]*)>")

MEMBER_ATTR_RE = re.compile(r"(?P<name>[A-Za-z_][\w:-]*)=([\"'])(?P<value>.*?)\2")

SENDER_ID_SUFFIX_RE = re.compile(
    r"^(?P<name>.*?)\s+\((?P<id>"
    r"(?:mem|legacy)_[0-9a-f]{32}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[^()\s]+"
    r")\)$"
)



@dataclass(frozen=True)
class RosterMember:
    id: str
    name: str
    kind: str
    is_self: bool = False



@dataclass(frozen=True)
class ChannelPromptInfo:
    sender_name: str = ""
    sender_id: str = ""
    seq: int | None = None
    body: str = ""
    has_channel_context: bool = False
    members: tuple[RosterMember, ...] = ()



@dataclass(frozen=True)
class ConversationBoundary:
    size: int
    prefix_sha256: str = ""
    prefix_length: int = 0



@dataclass(frozen=True)
class StripResult:
    status: str
    message_id: str = ""
    entry_id: str = ""
    updated_at: str = ""
    error: str = ""



def speaker_kind_label(kind: object) -> str:
    value = str(kind or "").strip().casefold()
    if value == MEMBER_KIND_USER:
        return "用户"
    if value == MEMBER_KIND_AGENT:
        return "智能体"
    return "成员"



def speaker_sentence(name: object, kind: object) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        return ""
    return render_username_template(SPEAKER_SENTENCE_TEMPLATE, text)



def render_username_template(template: str, username: str) -> str:
    """Replace both the requested and legacy username placeholders."""
    value = " ".join(str(username or "").split()) or "Master"
    return template.replace(USERNAME_PLACEHOLDER, value).replace(
        LEGACY_USERNAME_PLACEHOLDER, value
    )



def existing_speaker(text: str) -> tuple[str, str]:
    """Return the name and optional kind label from an existing reminder."""
    match = WAKE_RE.search(text)
    if not match:
        return "", ""
    identity = SPEAKER_IDENTITY_RE.match(match.group("text"))
    if not identity:
        return "", ""
    return identity.group("identity").strip(), identity.group("label") or ""



def has_channel_context(text: str) -> bool:
    return bool(CHANNEL_CONTEXT_RE.search(text))



def strip_channel_context(text: str) -> tuple[str, bool]:
    """Remove complete channel roster blocks while preserving everything else."""
    updated = CHANNEL_CONTEXT_RE.sub("", text)
    return updated, updated != text



def _parse_attrs(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in MEMBER_ATTR_RE.finditer(raw):
        values[match.group("name")] = html.unescape(match.group("value"))
    return values



def parse_roster_members(text: str) -> tuple[RosterMember, ...]:
    members: list[RosterMember] = []
    for match in MEMBER_TAG_RE.finditer(text):
        attrs = _parse_attrs(match.group("attrs"))
        member_id = str(attrs.get("id") or "").strip()
        name = str(attrs.get("name") or "").strip()
        if not member_id and not name:
            continue
        raw_kind = str(attrs.get("kind") or "").strip().casefold()
        kind = (
            raw_kind
            if raw_kind in (MEMBER_KIND_USER, MEMBER_KIND_AGENT)
            else ""
        )
        members.append(
            RosterMember(
                id=member_id,
                name=name,
                kind=kind,
                is_self=str(attrs.get("self") or "").strip().casefold() == "true",
            )
        )
    return tuple(members)



def parse_sender(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    match = SENDER_ID_SUFFIX_RE.fullmatch(text)
    if match:
        return match.group("name").strip(), match.group("id").strip()
    return text, ""



def parse_channel_prompt(text: str) -> ChannelPromptInfo | None:
    if not text.startswith(CHANNEL_PROMPT_HEADER):
        return None
    sender_match = SENDER_RE.search(text)
    sender_name, sender_id = parse_sender(
        sender_match.group("value") if sender_match else ""
    )
    seq_match = SEQ_RE.search(text)
    body = ""
    marker = re.search(r"(?m)^message:[ \t]*\r?\n", text)
    if marker:
        body = text[marker.end() :]
        body = body.split("\n\nYou are listening", 1)[0]
        body = re.split(
            r"\n(?:attachment-names:|multimodal-note:)", body, maxsplit=1
        )[0]
        body = body.strip()
    return ChannelPromptInfo(
        sender_name=sender_name,
        sender_id=sender_id,
        seq=int(seq_match.group("seq")) if seq_match else None,
        body=body,
        has_channel_context=has_channel_context(text),
        members=parse_roster_members(text),
    )



def roster_kind(text: str, sender_id: str, sender_name: str) -> str:
    members = parse_roster_members(text)
    sender_key = canonical_name(sender_name)
    for member in members:
        if sender_id and member.id == sender_id:
            if member.kind:
                return member.kind
            if member.is_self:
                return MEMBER_KIND_AGENT
        if sender_key and canonical_name(member.name) == sender_key:
            if member.kind:
                return member.kind
            if member.is_self:
                return MEMBER_KIND_AGENT
    return ""



def match_history_kind(
    entries: Sequence[Mapping[str, Any]],
    *,
    seq: int | None,
    body: str,
    row_ms: int = 0,
    sender_id: str = "",
    sender_name: str = "",
) -> str:
    """Return the unambiguous kind for a prompt from channel history."""
    candidates = list(entries)
    if not candidates:
        return ""
    if seq is not None:
        by_seq = [
            entry for entry in candidates if int(entry.get("seq") or 0) == seq
        ]
        if by_seq:
            candidates = by_seq
    if body:
        exact = [
            entry
            for entry in candidates
            if str(entry.get("text") or "").strip() == body
        ]
        if not exact:
            return ""
        candidates = exact
    if sender_id:
        by_id = [
            entry
            for entry in candidates
            if str(entry.get("sender_id") or "") == sender_id
        ]
        if by_id:
            candidates = by_id
    if sender_name:
        name_key = canonical_name(sender_name)
        by_name = [
            entry
            for entry in candidates
            if canonical_name(entry.get("sender")) == name_key
        ]
        if by_name:
            candidates = by_name
    if not candidates:
        return ""
    kinds = {bool(entry.get("from_agent")) for entry in candidates}
    if len(kinds) == 1:
        return MEMBER_KIND_AGENT if kinds.pop() else MEMBER_KIND_USER
    if row_ms > 0:
        ranked = sorted(
            candidates,
            key=lambda entry: abs(int(entry.get("sent_at") or 0) - row_ms),
        )
        if len(ranked) == 1:
            return (
                MEMBER_KIND_AGENT
                if bool(ranked[0].get("from_agent"))
                else MEMBER_KIND_USER
            )
        first = abs(int(ranked[0].get("sent_at") or 0) - row_ms)
        second = abs(int(ranked[1].get("sent_at") or 0) - row_ms)
        if first < second:
            return (
                MEMBER_KIND_AGENT
                if bool(ranked[0].get("from_agent"))
                else MEMBER_KIND_USER
            )
    return ""



def infer_speaker_kind(
    text: str,
    sender_name: str,
    sender_id: str,
    *,
    history_entries: Sequence[Mapping[str, Any]] = (),
    row_ms: int = 0,
    identity_lookup: Callable[[str, str], str] | None = None,
) -> str:
    """Infer a speaker kind in the documented priority order."""
    kind = roster_kind(text, sender_id, sender_name)
    if kind:
        return kind
    info = parse_channel_prompt(text)
    kind = match_history_kind(
        history_entries,
        seq=info.seq if info else None,
        body=info.body if info else "",
        row_ms=row_ms,
        sender_id=sender_id,
        sender_name=sender_name,
    )
    if kind:
        return kind
    if identity_lookup is not None:
        kind = identity_lookup(sender_id, sender_name)
        if kind in (MEMBER_KIND_USER, MEMBER_KIND_AGENT):
            return kind
    return ""



def add_speaker_to_wake_reminder(
    text: str,
    *,
    speaker_name: str,
    speaker_kind: str,
    username: str = "",
) -> tuple[str, bool]:
    """Prepend the current speaker to an existing or newly inserted reminder."""
    sentence = speaker_sentence(speaker_name, speaker_kind)
    if not sentence:
        return text, False
    match = WAKE_RE.search(text)
    if match:
        existing = match.group("text")
        base = SPEAKER_PREFIX_RE.sub("", existing, count=1).lstrip()
        if username:
            replacement_name = render_username_template(
                USERNAME_PLACEHOLDER, username
            )
            base = base.replace("用户", replacement_name)
        replacement = f"wake-reminder: {sentence}{base}"
        if replacement == match.group(0):
            return text, False
        return text[: match.start()] + replacement + text[match.end() :], True

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    insert_at = 0
    message_time_index = None
    for index, line in enumerate(lines):
        if MESSAGE_TIME_RE.match(line):
            message_time_index = index
            break
    if message_time_index is not None:
        insert_at = message_time_index + 1
    elif lines:
        insert_at = 1
    line = f"wake-reminder: {sentence}{newline}"
    if insert_at >= len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + newline
    lines.insert(insert_at, line)
    return "".join(lines), True
