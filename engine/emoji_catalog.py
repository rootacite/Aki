"""Per-profile emoji catalog loading for channel system prompts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("akid.emoji")

MAX_EMOJI_NAME_CHARS = 64
MAX_EMOJI_DESC_CHARS = 240
EMOJI_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class EmojiEntry:
    name: str
    description: str


def _safe_file_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        return ""
    return name


def _clean_description(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text[:MAX_EMOJI_DESC_CHARS].strip()


def load_emoji_entries(emoji_root: Path, profile: str) -> list[EmojiEntry]:
    """Load one profile's emoji index, skipping malformed or missing entries."""
    index_path = emoji_root / profile / "index.json"
    if not index_path.is_file():
        return []
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("cannot read emoji index %s: %s", index_path, exc)
        return []
    if not isinstance(payload, list):
        LOGGER.warning("emoji index %s is not a JSON array", index_path)
        return []

    seen: set[str] = set()
    entries: list[EmojiEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        description = _clean_description(item.get("desc"))
        filename = _safe_file_name(item.get("file"))
        if (
            not EMOJI_NAME_RE.fullmatch(name)
            or name in seen
            or not description
            or not filename
        ):
            LOGGER.warning("skipping invalid emoji entry in %s: %r", index_path, item)
            continue
        if not (index_path.parent / filename).is_file():
            LOGGER.warning(
                "skipping emoji %s: missing file %s", name, index_path.parent / filename
            )
            continue
        seen.add(name)
        entries.append(EmojiEntry(name=name, description=description))
    return entries
