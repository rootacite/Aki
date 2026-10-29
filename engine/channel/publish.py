"""Publish text, pictures, notices, and approval prompts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from engine.tokifactor import (
    CONTROL_TOPIC,
    MAX_IMAGE_BYTES,
    MAX_MESSAGE_ATTACHMENTS,
    MAX_MESSAGE_IMAGE_BYTES,
    PICTURE_TOPIC,
    detect_image_mime,
    encode_picture_abort,
    encode_picture_payloads,
    encode_text_payloads,
)
from .constants import ChannelUnavailable

class PublishMixin:

    async def _worker_loop(self) -> None:
        while True:
            (
                sender,
                sender_id,
                text,
                metadata,
                message_id,
                sent_at,
                replayed,
            ) = await self._inbox.get()
            try:
                await self.on_text(
                    sender,
                    sender_id,
                    text,
                    metadata,
                    message_id,
                    sent_at,
                    replayed,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("channel message handler failed")


    async def publish_text(
        self,
        text: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        payloads = encode_text_payloads(
            text,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            message_id=message_id,
            metadata=metadata,
        )
        async with self._publish_lock:
            for payload in payloads:
                await client.publish(self.topic, payload.encode("utf-8"), qos=1)
        self._replies_sent += 1


    async def publish_structured(
        self, text: str, metadata: Mapping[str, Any]
    ) -> None:
        await self.publish_text(text, metadata)


    async def publish_control(
        self,
        text: str,
        metadata: Mapping[str, Any],
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        payloads = encode_text_payloads(
            text,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            metadata=metadata,
        )
        async with self._publish_lock:
            for payload in payloads:
                await client.publish(
                    CONTROL_TOPIC,
                    payload.encode("utf-8"),
                    qos=qos,
                    retain=retain,
                )
        self._control_sent += 1


    async def publish_notice(self, text: str) -> None:
        with contextlib.suppress(ChannelUnavailable):
            await self.publish_text(text)


    async def publish_approval(self, data: dict) -> None:
        approval_id = str(data.get("approval_id") or "")
        tool_name = str(data.get("tool_name") or "unknown")
        tool_input = data.get("input")
        try:
            rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            rendered = str(tool_input)
        if len(rendered) > 1500:
            rendered = rendered[:1500] + "\n... (truncated)"
        timeout_s = data.get("timeout_s")
        lines = [
            "akid approval required",
            f"tool: {tool_name}",
            f"approval_id: {approval_id}",
        ]
        if rendered and rendered != "null":
            lines.extend(["input:", rendered])
        if timeout_s is not None:
            lines.append(f"timeout: {timeout_s}s")
        lines.extend(
            [
                "",
                f"reply: akid-approve {approval_id}",
                f"       akid-deny {approval_id}",
                "or simply: akid-approve / akid-deny (oldest pending)",
            ]
        )
        await self.publish_structured(
            "\n".join(lines),
            {
                "schema": "akid.v1",
                "kind": "approval",
                "approvalId": approval_id,
                "requesterId": str(data.get("requester_id") or ""),
                "toolName": tool_name,
                "toolCallId": str(data.get("tool_call_id") or ""),
                "timeoutS": str(timeout_s if timeout_s is not None else ""),
                "details": rendered if rendered and rendered != "null" else "",
            },
        )


    async def publish_image(
        self,
        data: bytes,
        *,
        caption: str | None = None,
        mime: str | None = None,
        message_id: str = "",
        attachment_index: int = 0,
        attachment_count: int = 1,
        expect_text: bool = False,
    ) -> dict:
        client = self._client
        if client is None or not self._connected:
            raise ChannelUnavailable("channel is not connected")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes")
        detected = mime or detect_image_mime(data)
        if detected is None:
            raise ValueError("unsupported image format (png/jpeg/gif/webp only)")
        transfer_id, payloads = encode_picture_payloads(
            data,
            self.magic,
            sender=self.sender_name,
            sender_id=self.sender_id,
            avatar_hash=self._avatar_hash,
            caption=caption or "",
            message_id=message_id,
            attachment_index=attachment_index,
            attachment_count=attachment_count,
            text_commit=expect_text,
        )
        semaphore = asyncio.Semaphore(8)

        async def publish_one(payload: str) -> None:
            async with semaphore:
                await client.publish(PICTURE_TOPIC, payload.encode("utf-8"), qos=1)

        async with self._image_lock:
            try:
                await asyncio.gather(*(publish_one(payload) for payload in payloads))
            except Exception:
                with contextlib.suppress(Exception):
                    abort = encode_picture_abort(
                        transfer_id,
                        sender=self.sender_name,
                        sender_id=self.sender_id,
                        reason="fail",
                    )
                    await client.publish(PICTURE_TOPIC, abort.encode("utf-8"), qos=1)
                raise
        self._images_sent += 1
        self._last_image_at = time.time()
        return {
            "transfer_id": transfer_id,
            "bytes": len(data),
            "chunks": len(payloads),
            "mime": detected,
            "topic": PICTURE_TOPIC,
            "message_id": message_id or transfer_id,
            "name": caption or "",
            "attachment_index": int(attachment_index),
            "attachment_count": int(attachment_count),
        }


    async def publish_message(
        self,
        text: str,
        images: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish one logical message, uploading images before the text commit."""
        if len(images) > MAX_MESSAGE_ATTACHMENTS:
            raise ValueError("a channel message may contain at most 4 images")
        if sum(len(bytes(image.get("data") or b"")) for image in images) > MAX_MESSAGE_IMAGE_BYTES:
            raise ValueError("channel message images exceed the size limit")
        message_id = str(uuid.uuid4())
        results: list[dict[str, Any]] = []
        total = len(images)
        for index, image in enumerate(images):
            result = await self.publish_image(
                bytes(image["data"]),
                caption=str(image.get("name") or ""),
                mime=str(image.get("mime") or "") or None,
                message_id=message_id,
                attachment_index=index,
                attachment_count=total,
                expect_text=bool(text.strip()),
            )
            results.append(result)
        if text.strip() or not results:
            commit_metadata = dict(metadata or {})
            if results:
                commit_metadata.update(
                    {
                        "attachmentCount": str(len(results)),
                        "attachmentIds": ",".join(str(item["transfer_id"]) for item in results),
                        "attachmentNames": "\n".join(str(item["name"]) for item in results),
                        "attachments": json.dumps(
                            [
                                {
                                    "id": str(item["transfer_id"]),
                                    "name": str(item["name"]),
                                    "mime": str(item["mime"]),
                                    "bytes": int(item["bytes"]),
                                }
                                for item in results
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            await self.publish_text(
                text,
                commit_metadata,
                message_id=message_id,
            )
        return {"message_id": message_id, "images": results}
