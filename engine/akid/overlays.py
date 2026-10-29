"""Persist mid-turn user insertions and restore conversation snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from engine.channel_prompt import capture_conversation_boundary
from engine.live_context import (
    render_new_message,
    render_user_insertion,
)
from .constants import LOGGER
from .paths import now_iso

class OverlaysMixin:

    @staticmethod
    def _jsonl_last_entry_id(path: Path, size: int) -> str:
        if size <= 0:
            return ""
        try:
            with path.open("rb") as handle:
                start = max(0, size - 256 * 1024)
                handle.seek(start)
                data = handle.read(size - start)
        except OSError:
            return ""
        for raw in reversed(data.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("type") == "message":
                return str(row.get("id") or "")
        return ""


    def _snapshot_conversation(
        self, expected_prompt: str | None = None
    ) -> dict[str, Any] | None:
        directory = self._conversation_dir()
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        try:
            if not messages.is_file() or not conversation.is_file():
                return None
            size = messages.stat().st_size
            boundary = capture_conversation_boundary(messages)
            return {
                "directory": directory,
                "messages_size": size,
                "conversation_bytes": conversation.read_bytes(),
                "conversation_mode": conversation.stat().st_mode & 0o777,
                "last_entry_id": self._jsonl_last_entry_id(messages, size),
                "expected_prompt": expected_prompt,
                "prefix_sha256": boundary.prefix_sha256,
                "prefix_length": boundary.prefix_length,
            }
        except OSError as exc:
            LOGGER.warning("cannot snapshot channel conversation: %s", exc)
            return None


    def _aki_bridge_available(self) -> bool:
        return bool(getattr(self.backend, "supports_aki_bridge", False))


    async def _snapshot_turn_conversation(self, turn: Turn) -> None:
        """Keep a file snapshot as fallback and capture an in-process snapshot."""
        turn.conversation_snapshot = self._snapshot_conversation(turn.message)
        if not self._aki_bridge_available():
            return
        try:
            payload = await self.backend.snapshot_conversation(
                conversation_id=self.config.conversation_id,
                agent_id=self.config.agent_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "Aki bridge snapshot failed turn=%s; using file snapshot: %s",
                turn.turn_id,
                exc,
            )
            return
        snapshot_id = str(payload.get("snapshot_id") or "")
        if snapshot_id:
            turn.bridge_snapshot_id = snapshot_id


    @staticmethod
    def _render_during_work_message(
        text: str,
        *,
        sender: str,
        sender_id: str,
        seq: int | None,
        from_agent: bool,
    ) -> str:
        return render_new_message(
            text,
            sender=sender,
            sender_id=sender_id,
            source="agent" if from_agent else "user",
            phase="final",
            seq=seq,
            untrusted=True,
            during_work=True,
        )


    @staticmethod
    def _overlay_attachments_payload(
        attachments: Sequence[ChannelImageAttachment],
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": attachment.name,
                "path": attachment.path,
                **({"missing": True} if attachment.missing else {}),
            }
            for attachment in attachments
        ]


    def _ensure_turn_overlay_dir(self, turn_id: str) -> Path:
        root = self._turn_overlay_dir()
        directory = root / turn_id
        if not os.environ.get("AKI_TURN_OVERLAY_DIR", "").strip():
            try:
                root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(root.parent, 0o700)
            except OSError:
                pass
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (root, directory):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        return directory


    def _build_insertion_record(
        self, turn: Turn, item: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        text = str(item.get("text") or "")
        sender = str(item.get("sender") or turn.channel_sender or "user")
        sender_id = str(item.get("sender_id") or turn.origin_sender_id or "")
        attachments = tuple(item.get("attachments") or ())
        insertion_id = str(item.get("insertion_id") or "").strip()
        if not insertion_id:
            insertion_id = f"ins-{uuid.uuid4().hex}"
        try:
            inserted_at_ms = int(
                item.get("inserted_at_ms")
                or item.get("sent_at")
                or int(time.time() * 1000)
            )
        except (TypeError, ValueError):
            inserted_at_ms = int(time.time() * 1000)
        inserted_at = str(item.get("inserted_at") or "").strip()
        if not inserted_at:
            inserted_at = (
                datetime.fromtimestamp(inserted_at_ms / 1000, tz=timezone.utc)
                .astimezone()
                .isoformat(timespec="milliseconds")
            )
        attachment_payload = self._overlay_attachments_payload(attachments)
        content = render_user_insertion(
            text,
            insertion_id=insertion_id,
            seq=seq,
            inserted_at=inserted_at,
            inserted_at_ms=inserted_at_ms,
            attachments=attachment_payload,
        )
        item["insertion_id"] = insertion_id
        item["inserted_at"] = inserted_at
        item["inserted_at_ms"] = inserted_at_ms
        item["insertion_content"] = content
        return {
            "schema": "aki.turn-overlay.v1",
            "turn_id": turn.turn_id,
            "insertion_id": insertion_id,
            "seq": seq,
            "sender": sender,
            "sender_id": sender_id,
            "origin_is_agent": False,
            "inserted_at": inserted_at,
            "inserted_at_ms": inserted_at_ms,
            "text": text,
            "content": content,
            "sent_at": int(item.get("sent_at") or 0),
            "attachments": attachment_payload,
        }


    async def _write_turn_overlay_file(
        self, turn: Turn, item: dict[str, Any]
    ) -> Path | None:
        try:
            seq = int(item.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= 0:
            return None
        payload = self._build_insertion_record(turn, item)
        try:
            directory = await asyncio.to_thread(
                self._ensure_turn_overlay_dir, turn.turn_id
            )
            path = directory / f"{seq:08d}-{uuid.uuid4().hex}.json"
            data = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=str(directory)
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
                raise
            return path
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "failed to write turn overlay file turn=%s seq=%s: %s",
                turn.turn_id,
                seq,
                exc,
            )
            return None


    @staticmethod
    def _remove_turn_overlay_file(turn: Turn, seq: int) -> None:
        path = turn.overlay_files.pop(seq, None)
        if path is None:
            return
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


    async def _push_aki_interjection(
        self,
        turn: Turn,
        text: str,
        sender: str,
        sender_id: str,
        seq: int | None,
        *,
        from_agent: bool,
    ) -> None:
        if not turn.bridge_turn_started or not self._aki_bridge_available():
            return
        content = self._render_during_work_message(
            text,
            sender=sender,
            sender_id=sender_id,
            seq=seq,
            from_agent=from_agent,
        )
        try:
            await self.backend.update_aki_turn(
                turn.turn_id,
                {"interjection": {"user_messages": [{"content": content}]}},
            )
        except Exception as exc:
            LOGGER.warning(
                "failed to deliver Aki interjection turn=%s: %s",
                turn.turn_id,
                exc,
            )


    def _restore_boundary_error(
        self, snapshot: Mapping[str, Any], data: bytes
    ) -> str:
        expected_size = int(snapshot.get("messages_size") or 0)
        if len(data) < expected_size:
            return (
                f"conversation transcript shrank from {expected_size} "
                f"to {len(data)}"
            )
        if expected_size > 0 and data[expected_size - 1 : expected_size] != b"\n":
            return "conversation transcript boundary is not a line ending"
        prefix_length = int(snapshot.get("prefix_length") or 0)
        expected_prefix = str(snapshot.get("prefix_sha256") or "")
        if prefix_length and expected_prefix:
            start = max(0, expected_size - prefix_length)
            actual = hashlib.sha256(data[start:expected_size]).hexdigest()
            if actual != expected_prefix:
                return "conversation transcript prefix changed after snapshot"
        last_entry_id = str(snapshot.get("last_entry_id") or "")
        if last_entry_id:
            actual_last = ""
            for raw in reversed(data[:expected_size].splitlines()):
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("type") == "message":
                    actual_last = str(row.get("id") or "")
                    break
            if actual_last != last_entry_id:
                return (
                    "conversation snapshot boundary entry changed "
                    f"(expected {last_entry_id!r}, got {actual_last!r})"
                )
        expected_prompt = snapshot.get("expected_prompt")
        if expected_prompt is None:
            return ""
        suffix = data[expected_size:]
        first_row: dict[str, Any] | None = None
        for raw in suffix.splitlines():
            if not raw.strip():
                continue
            try:
                candidate = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return "first line after the snapshot is not valid JSON"
            if isinstance(candidate, dict) and candidate.get("type") == "message":
                first_row = candidate
                break
            return "first line after the snapshot is not a message"
        if first_row is None:
            return "no message followed the snapshot boundary"
        message = first_row.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            return "first message after the snapshot is not the temporary user turn"
        content = message.get("content")
        texts = (
            [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            if isinstance(content, list)
            else [content] if isinstance(content, str) else []
        )
        if str(expected_prompt) not in texts:
            return "first message after the snapshot does not match the temporary turn prompt"
        return ""


    async def _restore_conversation_snapshot(
        self, snapshot: Mapping[str, Any]
    ) -> None:
        directory = Path(snapshot["directory"])
        messages = directory / "messages.jsonl"
        conversation = directory / "conversation.json"
        try:
            expected_size = int(snapshot["messages_size"])
            data = messages.read_bytes()
        except (OSError, TypeError, ValueError) as exc:
            self._channel_transcript_status["last_restore_error"] = str(exc)
            LOGGER.warning("cannot read conversation before rollback: %s", exc)
            return
        boundary_error = self._restore_boundary_error(snapshot, data)
        if boundary_error:
            LOGGER.error(
                "refusing to roll back conversation snapshot: %s",
                boundary_error,
            )
            self._channel_transcript_status["last_restore_error"] = (
                boundary_error
            )
            return
        stopped = False
        try:
            await self.backend.stop()
            stopped = True
            temp = conversation.with_name(f".{conversation.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp.write_bytes(snapshot["conversation_bytes"])
                os.chmod(temp, int(snapshot.get("conversation_mode", 0o600)))
                os.replace(temp, conversation)
            finally:
                with contextlib.suppress(OSError):
                    temp.unlink()
            with messages.open("r+b") as handle:
                if expected_size > 0:
                    handle.seek(expected_size - 1)
                    if handle.read(1) != b"\n":
                        raise ValueError("conversation transcript boundary is invalid")
                handle.truncate(expected_size)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(messages, 0o600)
            self._channel_transcript_status["last_ephemeral_restore_at"] = now_iso()
            self._channel_transcript_status["ephemeral_turns"] = (
                int(self._channel_transcript_status.get("ephemeral_turns", 0)) + 1
            )
            self._channel_transcript_status.pop("last_restore_error", None)
        except (OSError, ValueError, TypeError) as exc:
            self._channel_transcript_status["last_restore_error"] = str(exc)
            LOGGER.warning("failed to roll back channel conversation: %s", exc)
        finally:
            if stopped and not self._shutting_down:
                self.backend.start()
                ready = await self._wait_backend_ready(20.0)
                if not ready:
                    LOGGER.warning(
                        "backend did not become ready after channel rollback"
                    )
