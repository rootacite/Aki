"""Shared group-chat memory visible to every Aki profile."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


PUBLIC_MEMORY_MAX_RECORDS = 4096


def default_public_memory_path(backend_dir: Path, channel_key: str) -> Path:
    return (
        Path(backend_dir)
        / "memfs"
        / "shared"
        / "channel"
        / channel_key
        / "messages.jsonl"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class PublicMemoryStore:
    """Append-only, de-duplicated public memory for final group messages."""

    def __init__(
        self,
        path: Path,
        *,
        max_records: int = PUBLIC_MEMORY_MAX_RECORDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.index_path = self.path.with_name("MEMORY.md")
        self.max_records = max(1, int(max_records))
        self.logger = logger or logging.getLogger("akid.public-memory")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("id"):
                records.append(value)
        return records

    @staticmethod
    def _record_id(entry: Any) -> str:
        message_id = str(getattr(entry, "message_id", "") or "").strip()
        if message_id:
            return f"message:{message_id}"
        sender_id = str(getattr(entry, "sender_id", "") or "")
        sent_at = int(getattr(entry, "sent_at", 0) or 0)
        timestamp = str(getattr(entry, "timestamp", "") or "")
        text = str(getattr(entry, "text", "") or "")
        clock = str(sent_at) if sent_at > 0 else timestamp[:19]
        digest = hashlib.sha256(
            f"{sender_id}\0{clock}\0{text}".encode("utf-8")
        ).hexdigest()[:32]
        return f"content:{digest}"

    @classmethod
    def _entry_record(cls, entry: Any) -> dict[str, Any] | None:
        if str(getattr(entry, "phase", "final") or "final") != "final":
            return None
        text = str(getattr(entry, "text", "") or "").strip()
        if not text:
            return None
        return {
            "id": cls._record_id(entry),
            "sender": str(getattr(entry, "sender", "") or ""),
            "sender_id": str(getattr(entry, "sender_id", "") or ""),
            "from_agent": bool(getattr(entry, "from_agent", False)),
            "text": text,
            "timestamp": str(getattr(entry, "timestamp", "") or ""),
            "sent_at": int(getattr(entry, "sent_at", 0) or 0),
            "message_id": str(getattr(entry, "message_id", "") or ""),
            "recorded_at": _now_iso(),
        }

    def record_many(self, entries: Iterable[Any]) -> int:
        new_records = [
            record
            for entry in entries
            if (record := self._entry_record(entry)) is not None
        ]
        if not new_records:
            return 0
        with self._locked():
            records = self._read_unlocked()
            known_ids = {str(item.get("id") or "") for item in records}
            added = [
                record
                for record in new_records
                if str(record["id"]) not in known_ids
            ]
            if not added:
                return 0
            records.extend(added)
            if len(records) > self.max_records:
                records = records[-self.max_records :]
            body = "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in records
            )
            _atomic_write(self.path, body)
            _atomic_write(
                self.index_path,
                "# Public group memory\n\n"
                "- `messages.jsonl` — append-only final group-chat messages shared by all agents.\n"
                f"- Records: {len(records)}\n"
                f"- Updated: {added[-1]['recorded_at']}\n",
            )
        return len(added)

    def record(self, entry: Any) -> bool:
        return self.record_many([entry]) > 0

    def snapshot(self) -> list[dict[str, Any]]:
        with self._locked():
            return self._read_unlocked()

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        records = self.snapshot()
        return records[-max(1, int(limit)) :]

    def status(self) -> dict[str, Any]:
        records = self.snapshot()
        return {
            "path": str(self.path),
            "index_path": str(self.index_path),
            "records": len(records),
            "updated_at": (
                str(records[-1].get("recorded_at") or "") if records else None
            ),
        }
