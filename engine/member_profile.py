"""Shared helpers for channel member profiles."""

from __future__ import annotations

import unicodedata

MEMBER_KIND_AGENT = "agent"
MEMBER_KIND_USER = "user"
MAX_MEMBER_DESCRIPTION_UNITS = 512


def member_name_key(value: object) -> str:
    """Canonical label key: NFKC, trimmed, case-insensitive Name."""
    return canonical_name(value)


def canonical_name(value: object) -> str:
    """Normalize a display label for case-insensitive label lookup."""
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def normalize_member_kind(value: object) -> str:
    """Return a known member kind. Missing or unknown values mean agent."""
    if str(value or "").strip().casefold() == MEMBER_KIND_USER:
        return MEMBER_KIND_USER
    return MEMBER_KIND_AGENT


def member_description_weight(value: object) -> int:
    """Count ASCII as 1 and every other Unicode code point as 2."""
    text = str(value or "")
    return sum(1 if ord(character) < 128 else 2 for character in text)


def truncate_member_description(
    value: object,
    max_units: int = MAX_MEMBER_DESCRIPTION_UNITS,
) -> str:
    """Truncate at a complete Unicode code point within the weighted budget."""
    if max_units <= 0:
        return ""
    text = str(value or "")
    weight = 0
    end = 0
    for index, character in enumerate(text):
        next_weight = 1 if ord(character) < 128 else 2
        if weight + next_weight > max_units:
            break
        weight += next_weight
        end = index + 1
    return text[:end]
