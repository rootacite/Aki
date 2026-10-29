"""Match channel mentions and decide who a message wakes."""

from __future__ import annotations

import re
import unicodedata
from engine.member_profile import (
    MEMBER_KIND_AGENT,
    canonical_name,
    normalize_member_kind,
)
from .types import (
    CHANNEL_WAKE_ALL,
    CHANNEL_WAKE_MENTIONS,
    CHANNEL_WAKE_USER,
    ChannelReplyRoute,
    _EMAIL_RE,
)


def select_execution_responder(
    members: list[ChannelMember] | tuple[ChannelMember, ...],
) -> str:
    """Pick one deterministic member to ask the user for an executor."""
    agents = [
        member
        for member in members
        if member.id and normalize_member_kind(member.kind) == MEMBER_KIND_AGENT
    ]
    if not agents:
        return ""
    for member in agents:
        labels = {canonical_name(member.id), canonical_name(member.name)}
        labels.update(canonical_name(alias) for alias in member.aliases)
        if "aki" in labels:
            return member.id
    ids = [member.id for member in agents]
    return min(ids, key=lambda value: (value.casefold(), value))



def strip_user_notice_target(text: str, user_name: str) -> tuple[str, bool]:
    """Remove an ``@<user_name>`` user-only marker from a reply.

    The marker forces the reply to wake only the user.  It is intended for
    replies addressed exclusively to the user, where other agents should not be
    disturbed.  The marker may appear anywhere in the reply.
    """
    name = user_name.strip()
    if not name:
        return text, False
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])@" + re.escape(name) + r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    if pattern.search(text) is None:
        return text, False
    cleaned = pattern.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, True



def _non_wake_alias_map(
    members: list[ChannelMember] | tuple[ChannelMember, ...],
) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for member in members:
        if normalize_member_kind(member.kind) != MEMBER_KIND_AGENT:
            continue
        seen: set[str] = set()
        for alias in (member.name, *member.aliases):
            normalized = alias.strip()
            key = canonical_name(normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            aliases.append((normalized, member.id))
    aliases.sort(key=lambda item: len(item[0]), reverse=True)
    return aliases



def _iter_non_wake_matches(
    text: str, members: list[ChannelMember] | tuple[ChannelMember, ...]
) -> list[tuple[int, int, str]]:
    marker_text = _EMAIL_RE.sub(lambda match: " " * len(match.group(0)), text)
    aliases = _non_wake_alias_map(members)
    if not aliases:
        return []
    lookup = {canonical_name(alias): member_id for alias, member_id in aliases}
    pattern = re.compile(
        r"\^(" + "|".join(re.escape(alias) for alias, _member_id in aliases) + r")"
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    matches: list[tuple[int, int, str]] = []
    matched_ends: set[int] = set()
    last_end = 0
    for match in pattern.finditer(marker_text):
        start, end = match.span()
        if start < last_end:
            continue
        previous = marker_text[start - 1] if start > 0 else ""
        if (
            previous.isascii()
            and (previous.isalnum() or previous == "_")
            and start not in matched_ends
        ):
            continue
        member_id = lookup.get(canonical_name(match.group(1)))
        if not member_id:
            continue
        matches.append((start, end, member_id))
        matched_ends.add(end)
        last_end = end
    return matches



def match_channel_non_wakes(
    text: str, members: list[ChannelMember] | tuple[ChannelMember, ...]
) -> set[str]:
    """Return member ids excluded by ``^alias`` markers."""
    return {member_id for _start, _end, member_id in _iter_non_wake_matches(text, members)}



def strip_channel_non_wakes(
    text: str, members: list[ChannelMember] | tuple[ChannelMember, ...]
) -> tuple[str, set[str]]:
    """Remove ``^alias`` markers and return the excluded member ids."""
    matches = _iter_non_wake_matches(text, members)
    if not matches:
        return text, set()
    excluded = {member_id for _start, _end, member_id in matches}
    cleaned = text
    for start, end, _member_id in sorted(matches, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip(), excluded



def route_channel_reply(
    text: str,
    members: list[ChannelMember] | tuple[ChannelMember, ...],
    *,
    user_name: str,
) -> ChannelReplyRoute:
    """Resolve which agents a final channel reply should wake."""
    cleaned, excluded = strip_channel_non_wakes(text, members)
    cleaned, user_only = strip_user_notice_target(cleaned, user_name)
    if user_only:
        return ChannelReplyRoute(text=cleaned, wake=CHANNEL_WAKE_USER)
    mentions = match_channel_mentions(cleaned, members)
    if mentions:
        return ChannelReplyRoute(
            text=cleaned,
            wake=CHANNEL_WAKE_MENTIONS,
            target_ids=tuple(sorted(mentions - excluded)),
        )
    if excluded:
        eligible = {
            member.id
            for member in members
            if member.id
            and not member.is_self
            and normalize_member_kind(member.kind) == MEMBER_KIND_AGENT
        }
        return ChannelReplyRoute(
            text=cleaned,
            wake=CHANNEL_WAKE_MENTIONS,
            target_ids=tuple(sorted(eligible - excluded)),
        )
    return ChannelReplyRoute(text=cleaned, wake=CHANNEL_WAKE_ALL)



def match_channel_mentions(
    text: str, members: list[ChannelMember] | tuple[ChannelMember, ...]
) -> set[str]:
    """Return member ids mentioned by ``@alias`` followed by a delimiter."""
    mention_text = _EMAIL_RE.sub(lambda match: " " * len(match.group(0)), text)
    aliases: list[tuple[str, str]] = []
    for member in members:
        seen: set[str] = set()
        for alias in (member.name, *member.aliases):
            normalized = alias.strip()
            key = canonical_name(normalized)
            if not normalized or key in seen:
                continue
            seen.add(key)
            aliases.append((normalized, member.id))
    aliases.sort(key=lambda item: len(item[0]), reverse=True)
    occupied = [False] * len(mention_text)
    matched: set[str] = set()
    for alias, member_id in aliases:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])@" + re.escape(alias) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(mention_text):
            start, end = match.span()
            if not _valid_mention_boundaries(mention_text, start, end):
                continue
            if any(occupied[start:end]):
                continue
            for index in range(start, end):
                occupied[index] = True
            matched.add(member_id)
    return matched



def strip_channel_mentions(
    text: str, members: list[ChannelMember] | tuple[ChannelMember, ...]
) -> str:
    """Remove valid ``@alias`` member markers from text."""
    mention_text = _EMAIL_RE.sub(lambda match: " " * len(match.group(0)), text)
    aliases = _non_wake_alias_map(members)
    if not aliases:
        return text
    spans: list[tuple[int, int]] = []
    occupied = [False] * len(mention_text)
    for alias, _member_id in aliases:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])@" + re.escape(alias) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(mention_text):
            start, end = match.span()
            if not _valid_mention_boundaries(mention_text, start, end):
                continue
            if any(occupied[start:end]):
                continue
            for index in range(start, end):
                occupied[index] = True
            spans.append((start, end))
    if not spans:
        return text
    cleaned = text
    for start, end in sorted(spans, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()



def _valid_mention_boundaries(text: str, start: int, end: int) -> bool:
    embedded_prefixes = "_@\\./%+-?&=#:"
    if start > 0:
        previous = text[start - 1]
        if previous.isalnum() or previous in embedded_prefixes:
            return False
    if end >= len(text):
        return False
    following = text[end]
    if following.isspace():
        return True
    category = unicodedata.category(following)
    if not category.startswith(("P", "S")) or following in "_@\\":
        return False
    if following in ".-+/" and end + 1 < len(text):
        after = text[end + 1]
        if after.isalnum() or after in "_-":
            return False
    return True
