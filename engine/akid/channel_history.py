"""Load, store, and clean channel history and image attachments."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from engine.aki_config import atomic_write
from engine.emoji_catalog import load_emoji_entries
from engine.channel_history import (
    channel_history_key,
    channel_history_path,
)
from engine.live_context import (
    ChannelImageAttachment,
    ChannelTranscriptEntry,
)
from engine.member_profile import (
    MEMBER_KIND_USER,
    normalize_member_kind,
)
from engine.tokifactor import (
    FileEnvelope,
    MAX_ATTACHMENT_NAME_CHARS,
    detect_image_mime,
    sanitize_file_name,
    sha256_file,
)
from .constants import (
    CHANNEL_TRANSCRIPT_MAX_RECORDS,
    LOGGER,
    MAX_CHANNEL_ATTACHMENTS,
    MAX_CHANNEL_TEXT_CHARS,
)
from .paths import now_iso

class ChannelHistoryMixin:

    def _record_channel_message(
        self,
        *,
        sender: str,
        sender_id: str,
        text: str,
        from_agent: bool,
        message_id: str = "",
        sent_at: int = 0,
        phase: str = "final",
        arrived_while_busy: bool = False,
        attachments: tuple[ChannelImageAttachment, ...] = (),
    ) -> ChannelTranscriptEntry:
        self._channel_history_seq += 1
        resolved_id = self._history_member_id(sender, sender_id)
        entry = ChannelTranscriptEntry(
            seq=self._channel_history_seq,
            sender=sender or sender_id or "unknown",
            sender_id=resolved_id or sender_id.strip(),
            text=text,
            from_agent=from_agent,
            timestamp=now_iso(),
            sent_at=sent_at if sent_at > 0 else int(time.time() * 1000),
            message_id=message_id,
            phase=phase,
            arrived_while_busy=arrived_while_busy,
            attachments=tuple(attachments),
        )
        self._channel_history.append(entry)
        if len(self._channel_history) > CHANNEL_TRANSCRIPT_MAX_RECORDS:
            del self._channel_history[:-CHANNEL_TRANSCRIPT_MAX_RECORDS]
        self._persist_channel_history()
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
            }
        )
        return entry


    def _channel_history_key(self) -> str:
        return channel_history_key(
            host=self.config.mqtt_host,
            port=self.config.mqtt_port,
            topic=self.config.mqtt_topic,
            magic=self.config.mqtt_magic,
        )


    def _channel_history_path(self, *, solo: bool | None = None) -> Path:
        use_solo = (
            self._solo_session is not None if solo is None else bool(solo)
        )
        return channel_history_path(
            self.channel_history_dir,
            key=self._channel_history_key(),
            profile=self.config.profile,
            solo=use_solo,
        )


    def _channel_media_root(self) -> Path:
        return (
            self.channel_media_dir
            / self._channel_history_key()
            / (self.config.profile or "aki")
        )


    @staticmethod
    def _safe_media_component(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._")
        return cleaned[:96] or "message"


    def _store_channel_image(
        self,
        image: ImageEnvelope,
        *,
        name: str,
        index: int,
    ) -> ChannelImageAttachment | None:
        mime = image.mime or detect_image_mime(image.data)
        if mime is None:
            return None
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(mime)
        if extension is None:
            return None
        root = self._channel_media_root()
        directory = root / self._safe_media_component(image.message_id)
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("channel media path is not a directory")
            resolved_root = root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
            if resolved_root not in resolved_directory.parents:
                raise ValueError("channel media path escaped its root")
            path = resolved_directory / f"{max(1, index)}{extension}"
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(image.data)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
            return ChannelImageAttachment(
                name=name[:MAX_ATTACHMENT_NAME_CHARS] or f"图片{max(1, index)}",
                path=str(path),
                mime=mime,
                size=len(image.data),
                index=max(1, index),
            )
        except OSError as exc:
            LOGGER.warning("failed to store channel image: %s", exc)
            return None


    def _channel_file_root(self) -> Path:
        return self._channel_media_root() / "files"


    def _channel_file_staging_root(self) -> Path:
        return self._channel_file_root() / ".incoming"


    def _store_channel_file(self, file: FileEnvelope) -> str | None:
        root = self._channel_file_root()
        name = sanitize_file_name(file.name)
        directory = root / self._safe_media_component(file.transfer_id or uuid.uuid4().hex)
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("channel file path is not a directory")
            resolved_root = root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
            if resolved_root not in resolved_directory.parents:
                raise ValueError("channel file path escaped its root")
            destination = resolved_directory / name
            source = Path(file.staged_path)
            try:
                os.replace(source, destination)
            except OSError:
                shutil.copyfile(source, destination)
                source.unlink(missing_ok=True)
            os.chmod(destination, 0o600)
            with contextlib.suppress(OSError):
                source.parent.rmdir()
            return str(destination)
        except OSError as exc:
            LOGGER.warning("failed to store channel file: %s", exc)
            return None


    @staticmethod
    def _channel_file_record_path(file: FileEnvelope, stored_path: str) -> str:
        raw = file.source_path.strip()
        if raw:
            candidate = Path(raw)
            try:
                if candidate.is_absolute() and candidate.is_file():
                    if (
                        candidate.stat().st_size == file.size
                        and file.sha256
                        and sha256_file(candidate).lower() == file.sha256.lower()
                    ):
                        return str(candidate)
            except OSError:
                pass
        return stored_path


    def _cleanup_channel_files(self) -> None:
        root = self._channel_file_root()
        keep = {
            entry.text
            for entry in self._channel_history
            if entry.text and entry.text.startswith("/")
        }
        if not root.is_dir() or root.is_symlink():
            return
        for directory in root.iterdir():
            if directory.name == ".incoming":
                continue
            if not directory.is_dir() or directory.is_symlink():
                continue
            if str(directory) in keep:
                continue
            remaining = False
            for path in directory.iterdir():
                if str(path) in keep:
                    remaining = True
                elif path.is_file():
                    with contextlib.suppress(OSError):
                        path.unlink()
            if not remaining:
                with contextlib.suppress(OSError):
                    directory.rmdir()


    def _cleanup_channel_media(self) -> None:
        root = self._channel_media_root()
        keep = {
            attachment.path
            for entry in self._channel_history
            for attachment in entry.attachments
            if attachment.path and not attachment.missing
        }
        if not root.is_dir() or root.is_symlink():
            return
        for directory in root.iterdir():
            if directory.name == "files":
                continue
            if not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if path.is_file() and str(path) not in keep:
                    with contextlib.suppress(OSError):
                        path.unlink()
            with contextlib.suppress(OSError):
                directory.rmdir()


    @staticmethod
    def _channel_history_payload(entry: ChannelTranscriptEntry) -> dict[str, Any]:
        return {
            "seq": entry.seq,
            "sender": entry.sender,
            "sender_id": entry.sender_id,
            "text": entry.text,
            "from_agent": entry.from_agent,
            "timestamp": entry.timestamp,
            "sent_at": entry.sent_at,
            "message_id": entry.message_id,
            "phase": entry.phase,
            "arrived_while_busy": entry.arrived_while_busy,
            "attachments": [
                {
                    "name": attachment.name,
                    "path": attachment.path,
                    "mime": attachment.mime,
                    "size": attachment.size,
                    "index": attachment.index,
                    "missing": attachment.missing,
                }
                for attachment in entry.attachments
            ],
        }


    @staticmethod
    def _load_channel_attachments(value: Any) -> tuple[ChannelImageAttachment, ...]:
        if not isinstance(value, list):
            return ()
        attachments: list[ChannelImageAttachment] = []
        for item in value[:MAX_CHANNEL_ATTACHMENTS]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()[:MAX_ATTACHMENT_NAME_CHARS]
            path = str(item.get("path") or "").strip()
            if not name:
                continue
            attachments.append(
                ChannelImageAttachment(
                    name=name,
                    path=path,
                    mime=str(item.get("mime") or ""),
                    size=max(0, int(item.get("size") or 0)),
                    index=max(1, int(item.get("index") or len(attachments) + 1)),
                    missing=bool(item.get("missing")) or not path,
                )
            )
        return tuple(attachments)


    def _load_channel_history(self, path: Path | None = None) -> None:
        path = path or self._channel_history_path()
        self.channel_history_path = path
        by_seq: dict[int, ChannelTranscriptEntry] = {}
        load_error: str | None = None
        history_changed = False
        if path.is_file():
            try:
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    try:
                        seq = int(payload.get("seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    if seq <= 0:
                        continue
                    raw_sender = str(payload.get("sender") or "")
                    raw_sender_id = str(payload.get("sender_id") or "")
                    normalized_id = self._history_member_id(raw_sender, raw_sender_id)
                    if normalized_id and normalized_id != raw_sender_id:
                        history_changed = True
                    by_seq[seq] = ChannelTranscriptEntry(
                        seq=seq,
                        sender=raw_sender,
                        sender_id=normalized_id or raw_sender_id,
                        text=str(payload.get("text") or "")[:MAX_CHANNEL_TEXT_CHARS],
                        from_agent=bool(payload.get("from_agent")),
                        timestamp=str(payload.get("timestamp") or ""),
                        sent_at=int(payload.get("sent_at") or 0),
                        message_id=str(payload.get("message_id") or ""),
                        phase=str(payload.get("phase") or "final"),
                        arrived_while_busy=bool(payload.get("arrived_while_busy")),
                        attachments=self._load_channel_attachments(payload.get("attachments")),
                    )
            except OSError as exc:
                load_error = str(exc)
                LOGGER.warning("failed to load channel history: %s", exc)
        ordered = [by_seq[seq] for seq in sorted(by_seq)]
        self._channel_history = ordered[-CHANNEL_TRANSCRIPT_MAX_RECORDS:]
        self._channel_history_seq = max(by_seq, default=0)
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "channel_key": self._channel_history_key(),
                "history_path": str(path),
                "persisted": path.is_file(),
            }
        )
        if load_error is None:
            self._channel_transcript_status.pop("last_history_error", None)
        else:
            self._channel_transcript_status["last_history_error"] = load_error
        if len(ordered) > CHANNEL_TRANSCRIPT_MAX_RECORDS:
            self._persist_channel_history()
        elif history_changed:
            self._persist_channel_history()
        for member_id, member in self._channel_members.items():
            if normalize_member_kind(member.get("kind")) == MEMBER_KIND_USER:
                self._normalize_channel_history_kind(member_id, MEMBER_KIND_USER)


    def _persist_channel_history(self) -> None:
        if self._solo_session is None and not self._channel_history:
            try:
                if (
                    self.channel_history_path.is_file()
                    and self.channel_history_path.stat().st_size > 1
                ):
                    LOGGER.error(
                        "refusing to replace non-empty group history with an empty snapshot: %s",
                        self.channel_history_path,
                    )
                    return
            except OSError:
                pass
        body = "".join(
            json.dumps(
                self._channel_history_payload(entry),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for entry in self._channel_history
        )
        try:
            atomic_write(self.channel_history_path, body)
        except (OSError, TypeError, ValueError) as exc:
            self._channel_transcript_status["last_history_error"] = str(exc)
            LOGGER.warning("failed to persist channel history: %s", exc)
            return
        self._channel_transcript_status.update(
            {
                "entries": len(self._channel_history),
                "last_seq": self._channel_history_seq,
                "channel_key": self._channel_history_key(),
                "history_path": str(self.channel_history_path),
                "persisted": True,
            }
        )
        self._channel_transcript_status.pop("last_history_error", None)
        self._cleanup_channel_media()
        self._cleanup_channel_files()


    def _emoji_entries(self) -> list[EmojiEntry]:
        root = self.config.emoji_dir or (self.config.cwd / "assets" / "emoji")
        return load_emoji_entries(root, self.config.profile)
