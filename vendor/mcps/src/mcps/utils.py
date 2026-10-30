"""Small helpers shared by the MCP tools."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def read_file_text(path: str) -> str:
    """Read a local text file and return its contents.

    Raises ValueError with a clear message when the file cannot be read.
    """
    expanded = os.path.expanduser(path)
    file = Path(expanded)
    if not file.exists():
        raise ValueError(f"file not found: {path}")
    if not file.is_file():
        raise ValueError(f"not a regular file: {path}")
    try:
        return file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read file {path}: {exc}") from exc


def read_items(path: str) -> list[str]:
    """Read items from a file: a JSON array of strings, or one item per non-empty line."""
    text = read_file_text(path)
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"file {path} is empty")
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"file {path} starts with '[' but is not valid JSON: {exc}") from exc
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError(f"file {path} JSON must be an array of strings")
        return data
    return [line for line in stripped.splitlines() if line.strip()]


def parse_json(text: str) -> tuple[Any, bool]:
    """Parse a JSON object/array out of a model response.

    Tolerates markdown fences and stray prose around the JSON payload.
    Returns ``(parsed_value, True)`` on success, else ``(original_text, False)``.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned), True
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1]), True
            except json.JSONDecodeError:
                pass
    return text, False


def chunk_text(text: str, max_chars: int, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks of at most ``max_chars`` characters.

    Chunks break at newlines when possible; overlap keeps boundary context.
    """
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + max_chars // 2, end)
            if newline != -1:
                end = newline + 1
        chunks.append(text[start:end])
        if end >= len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end  # overlap bigger than remaining content: no more overlap
        start = next_start
    return chunks


def split_items(
    items: list[str], max_items: int, max_chars: int
) -> list[list[tuple[int, str]]]:
    """Group items into request-sized chunks of ``(index, content)`` pairs."""
    chunks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_chars = 0
    for index, item in enumerate(items):
        if current and (
            len(current) >= max_items or current_chars + len(item) > max_chars
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append((index, item))
        current_chars += len(item)
    if current:
        chunks.append(current)
    return chunks
