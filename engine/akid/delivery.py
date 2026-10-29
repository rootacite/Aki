"""Send images to the channel and answer approval RPCs."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from engine.letta_process import (
    BackendProtocolError,
    BackendUnavailable,
)
from engine.tokifactor import (
    MAX_ATTACHMENT_NAME_CHARS,
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    ImageEnvelope,
    detect_image_mime,
)
from engine.protocol import (
    ErrorCode,
    RpcError,
    optional_string,
    require_string,
)
from .constants import (
    CHANNEL_IMAGE_DIRECTIVE_RE,
    LOGGER,
    MAX_AGENT_IMAGE_DIRECTIVES,
    MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES,
    MAX_IMAGE_CAPTION_CHARS,
)

class DeliveryMixin:

    async def _execute_tokifactor_send_image(self, args: Mapping[str, Any]) -> dict[str, Any]:
        channel = self.channel
        if channel is None:
            raise ValueError("tokifactor channel is disabled")
        path_value = args.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("path must be a non-empty string")
        caption_value = args.get("caption")
        if caption_value is not None and not isinstance(caption_value, str):
            raise ValueError("caption must be a string")
        caption = (caption_value or "").strip()
        if len(caption) > MAX_IMAGE_CAPTION_CHARS:
            raise ValueError(f"caption exceeds {MAX_IMAGE_CAPTION_CHARS} characters")

        raw = Path(path_value).expanduser()
        if not raw.is_absolute():
            raise ValueError("image path must be absolute")
        try:
            path = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError("image file does not exist") from exc
        if not path.is_file():
            raise ValueError("image path must be a file")
        roots = self.writable.external_source_roots()
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError(
                "image path must be under /tmp, ~/.local/share/tmp, or a registered writable directory"
            )
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("image file is empty")
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        data = await asyncio.to_thread(path.read_bytes)
        mime = detect_image_mime(data)
        if mime is None:
            raise ValueError("unsupported image format (png/jpeg/gif/webp only)")
        name = (caption or path.name)[:MAX_ATTACHMENT_NAME_CHARS]
        published = await channel.publish_message(
            "",
            [{"data": data, "mime": mime, "name": name}],
        )
        first = (published.get("images") or [{}])[0]
        message_id = str(published.get("message_id") or "")
        stored = self._store_channel_image(
            ImageEnvelope(
                sender=self.config.channel_name,
                data=data,
                sender_id=self._channel_self_key(),
                message_id=message_id,
                name=name,
                attachment_index=0,
                attachment_count=1,
                sent_at=int(time.time() * 1000),
                mime=mime,
                transfer_id=str(first.get("transfer_id") or ""),
            ),
            name=name,
            index=1,
        )
        if stored is not None:
            self._record_channel_message(
                sender=self.config.channel_name,
                sender_id=self._channel_self_key(),
                text="",
                from_agent=True,
                sent_at=int(time.time() * 1000),
                phase="final",
                attachments=(stored,),
            )
        result = dict(first)
        result["path"] = str(path)
        result["name"] = name
        return result


    async def _execute_tokifactor_send_file(self, args: Mapping[str, Any]) -> dict[str, Any]:
        channel = self.channel
        if channel is None:
            raise ValueError("tokifactor channel is disabled")
        path_value = args.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("path must be a non-empty string")
        raw = Path(path_value).expanduser()
        if not raw.is_absolute():
            raise ValueError("file path must be absolute")
        try:
            path = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError("file does not exist") from exc
        if not path.is_file():
            raise ValueError("path must be a file")

        def check_readable() -> None:
            with path.open("rb") as handle:
                handle.read(1)

        try:
            await asyncio.to_thread(check_readable)
        except OSError as exc:
            raise ValueError("file is not readable") from exc
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
        result = await channel.publish_file(path, source_path=str(path))
        sent_at = int(time.time() * 1000)
        self._record_channel_message(
            sender=self.config.channel_name,
            sender_id=self._channel_self_key(),
            text=str(path),
            from_agent=True,
            message_id=str(result.get("message_id") or ""),
            sent_at=sent_at,
            phase="final",
        )
        return result


    async def _extract_agent_channel_images(
        self, text: str, *, load_data: bool = True
    ) -> tuple[str, list[dict[str, Any]]]:
        """Replace valid #[/path] instructions with outbound image attachments."""
        images: list[dict[str, Any]] = []
        spans: list[tuple[int, int]] = []
        roots = self.writable.external_source_roots()
        used_names: set[str] = set()
        total_bytes = 0
        for match in CHANNEL_IMAGE_DIRECTIVE_RE.finditer(text):
            if len(images) >= MAX_AGENT_IMAGE_DIRECTIVES:
                break
            raw_path = match.group(1).strip()
            try:
                path = Path(raw_path).expanduser().resolve(strict=True)
            except OSError:
                continue
            if not path.is_absolute() or not path.is_file():
                continue
            if not any(path == root or root in path.parents for root in roots):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > MAX_IMAGE_BYTES:
                continue
            if total_bytes + size > MAX_CHANNEL_ATTACHMENT_TOTAL_BYTES:
                continue
            if load_data:
                try:
                    data = await asyncio.to_thread(path.read_bytes)
                except OSError:
                    continue
                mime = detect_image_mime(data)
            else:
                try:
                    with path.open("rb") as handle:
                        data = handle.read(64)
                except OSError:
                    continue
                mime = detect_image_mime(data)
            if mime is None:
                continue
            name = path.name.strip()[:MAX_ATTACHMENT_NAME_CHARS] or f"图片{len(images) + 1}"
            base = name
            suffix = 2
            while name.casefold() in used_names:
                name = f"{base} ({suffix})"
                suffix += 1
            used_names.add(name.casefold())
            total_bytes += size
            spans.append(match.span())
            if load_data:
                images.append({"data": data, "mime": mime, "name": name, "path": str(path)})
            else:
                images.append({"name": name, "path": str(path)})
        cleaned = text
        for start, end in reversed(spans):
            cleaned = cleaned[:start] + cleaned[end:]
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, images


    async def _send_external_result(
        self, tool_call_id: str, text: str, is_error: bool
    ) -> None:
        self.operation_journal.finish(
            tool_call_id,
            status="error" if is_error else "completed",
            error=text if is_error else "",
        )
        try:
            await self.backend.send_external_tool_result(
                tool_call_id,
                [{"type": "text", "text": text}],
                is_error=is_error,
            )
        except (BackendUnavailable, BackendProtocolError):
            LOGGER.warning("failed to deliver external tool result tool_call_id=%s", tool_call_id)


    def _approval_respond(
        self, conn: Connection, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        approval_id = require_string(params, "approval_id")
        decision = require_string(params, "decision")
        if decision not in ("allow", "deny"):
            raise RpcError(ErrorCode.INVALID_PARAMS, "decision must be 'allow' or 'deny'")
        message = optional_string(params, "message")
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise RpcError(
                ErrorCode.TURN_NOT_FOUND,
                "approval request not found",
                {"approval_id": approval_id},
            )
        if approval.turn.origin != "mqtt" and approval.turn.conn is not conn:
            raise RpcError(ErrorCode.NOT_INITIATOR, "only the initiating client may approve")
        if approval.future.done():
            raise RpcError(ErrorCode.TURN_NOT_FOUND, "approval already resolved")
        approval.future.set_result((decision == "allow", message))
        LOGGER.info(
            "local approval decision id=%s decision=%s turn=%s origin=%s",
            approval_id,
            decision,
            approval.turn.turn_id,
            approval.turn.origin,
        )
        return {"approval_id": approval_id, "decision": decision, "ok": True}
