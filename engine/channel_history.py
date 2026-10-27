"""Shared channel-history paths so akid and akictl agree on local files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def channel_history_key(
    *,
    host: str,
    port: int,
    topic: str,
    magic: str,
) -> str:
    material = json.dumps(
        {
            "host": str(host or "").strip().casefold(),
            "port": int(port),
            "topic": str(topic or "").strip(),
            "magic": str(magic or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def channel_history_dir(
    *,
    profile_config_path: Path | None,
    fallback: Path,
) -> Path:
    if profile_config_path is not None:
        return profile_config_path.parent / "channel-history"
    return fallback


def channel_history_path(
    root: Path,
    *,
    key: str,
    profile: str,
    solo: bool = False,
) -> Path:
    filename = f"{profile or 'aki'}.solo.jsonl" if solo else f"{profile or 'aki'}.jsonl"
    return Path(root) / key / filename


def read_channel_history(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not Path(path).is_file():
        return entries
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entries
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries
