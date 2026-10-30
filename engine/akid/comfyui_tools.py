"""External-tool handlers for Haru's managed ComfyUI lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from engine.comfyui import ComfyUIError

from .constants import (
    COMFYUI_END_TOOL_NAME,
    COMFYUI_GENERATE_TOOL_NAME,
    COMFYUI_STARTUP_TOOL_NAME,
    LOGGER,
)


class ComfyUIToolsMixin:
    async def _handle_comfyui_tool(
        self,
        turn: Any,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        manager = self.comfyui
        if tool_name == COMFYUI_STARTUP_TOOL_NAME:
            try:
                result = await manager.start()
            except asyncio.CancelledError:
                raise
            except ComfyUIError as exc:
                await self._external_tool_failure(turn, tool_call_id, str(exc))
                return
            except Exception as exc:
                LOGGER.exception("ComfyUI startup failed")
                await self._external_tool_failure(
                    turn, tool_call_id, f"ComfyUI startup failed: {exc}"
                )
                return
            await self._external_tool_success(
                turn,
                tool_call_id,
                "comfyui started "
                f"status={result['status']} pid={result.get('pid')} "
                f"pgid={result.get('pgid')} endpoint={result['endpoint']} "
                f"log={result.get('log_path', '')}",
            )
            return

        if tool_name == COMFYUI_END_TOOL_NAME:
            try:
                result = await manager.stop()
            except asyncio.CancelledError:
                raise
            except ComfyUIError as exc:
                await self._external_tool_failure(turn, tool_call_id, str(exc))
                return
            except Exception as exc:
                LOGGER.exception("ComfyUI shutdown failed")
                await self._external_tool_failure(
                    turn, tool_call_id, f"ComfyUI shutdown failed: {exc}"
                )
                return
            await self._external_tool_success(
                turn,
                tool_call_id,
                "comfyui stopped "
                f"pid={result.get('pid')} pgid={result.get('pgid')} "
                f"signal={result['signal']}",
            )
            return

        if tool_name != COMFYUI_GENERATE_TOOL_NAME:
            await self._external_tool_failure(
                turn, tool_call_id, f"unknown ComfyUI tool: {tool_name}"
            )
            return

        if manager.require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={"external": True, "channel": "comfyui"},
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"ComfyUI image not generated: {reason or 'denied'}",
                )
                return
        try:
            result = await manager.generate(tool_input)
        except asyncio.CancelledError:
            raise
        except (ComfyUIError, ValueError) as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"ComfyUI image generation failed: {exc}"
            )
            return
        except Exception as exc:
            LOGGER.exception("ComfyUI image generation failed")
            await self._external_tool_failure(
                turn, tool_call_id, f"ComfyUI image generation failed: {exc}"
            )
            return
        await self._external_tool_success(
            turn,
            tool_call_id,
            "comfyui image generated "
            f"path={result['path']} bytes={result['bytes']} mime={result['mime']} "
            f"seed={result['seed']} prompt_id={result['prompt_id']} "
            f"images={result['image_count']}. Use tokifactor_send_image or "
            f"#[/absolute/path] to share it.",
        )
