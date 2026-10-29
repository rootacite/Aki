"""MCP tool calls, image generation, and the send-image tool entry."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from engine.image_generation import (
    ASPECT_RATIOS,
    MAX_IMAGES,
    ImageGenerationError,
    generate_minimax_images,
)
from .constants import LOGGER
from .history import (
    _normalize_mcp_result,
    _truncate_mcp_text,
)

class RemoteToolsMixin:

    async def _handle_mcp_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if tool_name not in self._mcp_tool_names:
            await self._external_tool_failure(
                turn, tool_call_id, f"unknown MCP tool: {tool_name}"
            )
            return
        try:
            text, is_error = await self._call_mcp_tool(tool_name, tool_input)
        except Exception as exc:
            LOGGER.warning("MCP tool call failed tool=%s error=%s", tool_name, exc)
            await self._external_tool_failure(
                turn, tool_call_id, f"MCP tool call failed: {exc}"
            )
            return
        if is_error:
            await self._external_tool_failure(turn, tool_call_id, text)
        else:
            await self._external_tool_success(turn, tool_call_id, text)


    async def _call_mcp_tool(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> tuple[str, bool]:
        if self._aki_bridge_available():
            payload = await self.backend.mcp_call(
                tool_name,
                arguments,
                agent_id=self.config.agent_id,
                timeout=self.config.mcp_tool_timeout,
            )
            result = payload.get("result")
            return _normalize_mcp_result(
                json.dumps(result if result is not None else {}, ensure_ascii=False)
            )
        payload = json.dumps(dict(arguments), ensure_ascii=False)
        command = [
            self.config.letta_bin,
            "mcp",
            "call",
            tool_name,
            "--args-file",
            "-",
            "--agent",
            self.config.agent_id,
        ]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")),
                timeout=self.config.mcp_tool_timeout,
            )
        except asyncio.TimeoutError as exc:
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()
            raise RuntimeError(
                f"MCP tool timed out after {self.config.mcp_tool_timeout:g}s"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"failed to start letta mcp: {exc}") from exc

        stdout_text = stdout.decode("utf-8", "replace")
        stderr_text = stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            detail = (stderr_text or stdout_text.strip() or f"exit code {proc.returncode}")
            return _truncate_mcp_text(f"MCP tool failed: {detail}"), True
        return _normalize_mcp_result(stdout_text)


    async def _handle_tokifactor_image_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.channel is None:
            await self._external_tool_failure(
                turn, tool_call_id, "tokifactor image not sent: channel is disabled"
            )
            return
        if self.config.tokifactor_image_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={"external": True, "channel": "tokifactor"},
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"tokifactor image not sent: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_tokifactor_send_image(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"tokifactor image send failed: {exc}"
            )
            return
        text = (
            "sent tokifactor image "
            f"transfer_id={result['transfer_id']} bytes={result['bytes']} "
            f"chunks={result['chunks']} mime={result['mime']} topic={result['topic']}"
        )
        await self._external_tool_success(turn, tool_call_id, text)


    async def _handle_image_generation_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        image = self.config.image_generation
        if image is None or not image.available:
            await self._external_tool_failure(
                turn, tool_call_id, "image generation is not configured or available"
            )
            return
        if image.require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={
                    "external": True,
                    "channel": "image",
                    "provider": "minimax",
                },
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"image not generated: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_image_generation(tool_input)
        except asyncio.CancelledError:
            raise
        except (ImageGenerationError, ValueError) as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"image generation failed: {exc}"
            )
            return
        except Exception as exc:
            LOGGER.exception("image generation failed tool_call_id=%s", tool_call_id)
            await self._external_tool_failure(
                turn, tool_call_id, f"image generation failed: {exc}"
            )
            return
        images = result.get("images") if isinstance(result, dict) else None
        if not isinstance(images, list) or not images:
            await self._external_tool_failure(
                turn, tool_call_id, "image generation returned no images"
            )
            return
        lines = [
            "image generated "
            f"count={result['count']} model={result['model']} "
            f"aspect_ratio={result['aspect_ratio']}"
        ]
        for index, item in enumerate(images):
            lines.append(
                f"image[{index}] path={item['path']} bytes={item['bytes']} "
                f"mime={item['mime']}"
            )
        lines.append(
            "Use tokifactor_send_image with one path to share it in the channel."
        )
        await self._external_tool_success(turn, tool_call_id, "\n".join(lines))


    async def _execute_image_generation(
        self, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        image = self.config.image_generation
        if image is None or not image.available:
            raise ImageGenerationError("image generation is not configured or available")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        aspect_ratio = args.get("aspect_ratio")
        if aspect_ratio is None:
            aspect_ratio = image.aspect_ratio
        if not isinstance(aspect_ratio, str) or aspect_ratio not in ASPECT_RATIOS:
            raise ValueError("aspect_ratio is not supported")
        n = args.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_IMAGES:
            raise ValueError(f"n must be between 1 and {MAX_IMAGES}")
        optimizer = args.get("prompt_optimizer")
        if optimizer is None:
            optimizer = image.prompt_optimizer
        if not isinstance(optimizer, bool):
            raise ValueError("prompt_optimizer must be a boolean")
        output_dir = Path(image.output_dir).expanduser()
        result = await asyncio.to_thread(
            generate_minimax_images,
            base_url=image.base_url,
            endpoint=image.path,
            api_key=image.api_key,
            model=image.model,
            prompt=prompt,
            output_dir=output_dir,
            aspect_ratio=aspect_ratio,
            n=n,
            timeout=image.timeout,
            max_bytes=image.max_bytes,
            prompt_optimizer=optimizer,
            aigc_watermark=image.aigc_watermark,
            use_proxy=image.use_proxy,
            upstream_proxy=self.config.aki.upstream_proxy if self.config.aki else "",
        )
        return result
