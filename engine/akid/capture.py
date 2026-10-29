"""Capture a camera photo or a desktop screenshot."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from engine.tokifactor import detect_image_mime
from .constants import (
    MAX_CAMERA_DIMENSION,
    MAX_CAMERA_OUTPUT_BYTES,
    MAX_SCREENSHOT_OUTPUT_BYTES,
    MIN_CAMERA_DIMENSION,
)
from .paths import (
    _is_video_device,
    default_camera_dir,
    default_screenshot_dir,
    prepare_private_output_dir,
    unique_capture_path,
)

from .access import pkg

class CaptureMixin:

    async def _handle_camera_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.config.camera_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={"external": True, "channel": "camera", "privacy": "camera"},
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"camera photo not taken: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_camera_capture(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"camera capture failed: {exc}"
            )
            return
        text = (
            "photo captured "
            f"path={result['path']} bytes={result['bytes']} "
            f"width={result['width']} height={result['height']} "
            f"device={result['device']} mime={result['mime']}"
        )
        await self._external_tool_success(turn, tool_call_id, text)


    async def _handle_screenshot_tool(
        self,
        turn: Turn,
        tool_call_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> None:
        if self.config.screenshot_require_approval:
            allow, reason = await self._request_approval(
                turn,
                approval_id=f"external-{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_input=tool_input,
                extra={
                    "external": True,
                    "channel": "desktop",
                    "privacy": "screenshot",
                },
            )
            if not allow:
                await self._external_tool_failure(
                    turn,
                    tool_call_id,
                    f"screenshot not taken: {reason or 'denied'}",
                )
                return
        try:
            result = await self._execute_screenshot_capture(tool_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._external_tool_failure(
                turn, tool_call_id, f"screenshot capture failed: {exc}"
            )
            return
        text = (
            "screenshot captured "
            f"path={result['path']} bytes={result['bytes']} "
            f"width={result['width']} height={result['height']} "
            f"display={result['display']} backend={result['backend']} "
            f"mime={result['mime']} "
            f"cursor={str(result['include_cursor']).lower()}"
        )
        await self._external_tool_success(turn, tool_call_id, text)


    def _camera_dimension(self, value: Any, default: int) -> int:
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("camera width/height must be integers")
        if value < MIN_CAMERA_DIMENSION or value > MAX_CAMERA_DIMENSION:
            raise ValueError(
                f"camera dimensions must be between {MIN_CAMERA_DIMENSION} and {MAX_CAMERA_DIMENSION}"
            )
        return value


    async def _probe_image_dimensions(
        self, path: Path, fallback_width: int, fallback_height: int
    ) -> tuple[int, int]:
        if not self.config.ffprobe_bin:
            return fallback_width, fallback_height
        ffprobe = shutil.which(self.config.ffprobe_bin)
        if not ffprobe:
            return fallback_width, fallback_height
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.CancelledError:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            raise
        except (OSError, asyncio.TimeoutError):
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            return fallback_width, fallback_height
        if proc.returncode != 0:
            return fallback_width, fallback_height
        parts = stdout.decode("utf-8", "replace").strip().split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
        return fallback_width, fallback_height


    async def _execute_camera_capture(self, args: Mapping[str, Any]) -> dict[str, Any]:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        if not ffmpeg:
            raise ValueError(f"ffmpeg is unavailable: {self.config.ffmpeg_bin}")
        device_value = args.get("device", self.config.camera_device)
        if not isinstance(device_value, str) or not device_value.strip():
            raise ValueError("device must be a non-empty string")
        try:
            device = Path(device_value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("camera device does not exist") from exc
        if not pkg()._is_video_device(device):
            raise ValueError("device must be a V4L2 /dev/videoN character device")
        width = self._camera_dimension(args.get("width"), self.config.camera_width)
        height = self._camera_dimension(args.get("height"), self.config.camera_height)
        output_dir = self.config.camera_output_dir or default_camera_dir()
        prepare_private_output_dir(output_dir, "camera")
        output = unique_capture_path(output_dir, "camera", ".jpg")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-input_format",
            "mjpeg",
            "-video_size",
            f"{width}x{height}",
            "-i",
            str(device),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ValueError(f"cannot start ffmpeg: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except asyncio.TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera capture timed out") from exc
        if proc.returncode != 0:
            with contextlib.suppress(OSError):
                output.unlink()
            detail = stderr.decode("utf-8", "replace").strip()[-500:]
            raise ValueError(f"ffmpeg failed: {detail or proc.returncode}")
        try:
            size = output.stat().st_size
        except OSError as exc:
            raise ValueError("camera did not produce an output file") from exc
        if size <= 0:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera produced an empty image")
        if size > MAX_CAMERA_OUTPUT_BYTES:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError(f"camera image exceeds {MAX_CAMERA_OUTPUT_BYTES} bytes")
        with contextlib.suppress(OSError):
            os.chmod(output, 0o600)
        data = await asyncio.to_thread(output.read_bytes)
        mime = detect_image_mime(data)
        if mime is None:
            with contextlib.suppress(OSError):
                output.unlink()
            raise ValueError("camera output is not a supported image")
        actual_width, actual_height = await self._probe_image_dimensions(
            output, width, height
        )
        return {
            "path": str(output),
            "bytes": size,
            "width": actual_width,
            "height": actual_height,
            "device": str(device),
            "mime": mime,
        }


    def _png_dimensions(self, path: Path) -> tuple[int, int] | None:
        try:
            with path.open("rb") as handle:
                header = handle.read(24)
        except OSError:
            return None
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if header[12:16] != b"IHDR":
            return None
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0:
            return None
        return width, height


    async def _execute_screenshot_capture(
        self,
        args: Mapping[str, Any],
        *,
        output_path: Path | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        output_name = args.get("output")
        if output_name is not None:
            if not isinstance(output_name, str) or not output_name.strip():
                raise ValueError("output must be a non-empty string")
            output_name = output_name.strip()
            if len(output_name) > 128 or not all(
                char.isalnum() or char in "._-" for char in output_name
            ):
                raise ValueError("output contains unsupported characters")
        include_cursor = args.get("include_cursor", False)
        if not isinstance(include_cursor, bool):
            raise ValueError("include_cursor must be a boolean")
        backends = self._screenshot_backends()
        if output_name:
            backends = [backend for backend in backends if backend[0] == "grim"]
        if not backends:
            if output_name:
                raise ValueError("output selection requires the grim backend")
            raise ValueError("no supported screenshot backend found (spectacle/grim)")
        candidates = self._wayland_display_candidates()
        if not candidates:
            raise ValueError("no Wayland display socket found")
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        ).expanduser()
        if output_path is not None:
            output = output_path.expanduser()
            prepare_private_output_dir(output.parent, "screenshot")
        else:
            output_dir = (
                self.config.screenshot_output_dir or default_screenshot_dir()
            )
            prepare_private_output_dir(output_dir, "screenshot")
            output = unique_capture_path(output_dir, "screenshot", ".png")
        errors: list[str] = []
        for backend, executable in backends:
            for display in candidates:
                with contextlib.suppress(OSError):
                    output.unlink()
                if backend == "spectacle":
                    command = [executable, "-b", "-n", "-f"]
                    if include_cursor:
                        command.append("-p")
                    command.extend(["-o", str(output)])
                else:
                    command = [executable, "-t", "png"]
                    if include_cursor:
                        command.append("-c")
                    if output_name:
                        command.extend(["-o", output_name])
                    command.append(str(output))
                env = dict(os.environ)
                env["XDG_RUNTIME_DIR"] = str(runtime_dir)
                env["WAYLAND_DISPLAY"] = display
                env.setdefault("XDG_SESSION_TYPE", "wayland")
                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.CancelledError:
                    if proc is not None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await proc.wait()
                    raise
                except asyncio.TimeoutError:
                    if proc is not None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await proc.wait()
                    errors.append(f"{display}/{backend}: timed out")
                    continue
                except OSError as exc:
                    errors.append(f"{display}/{backend}: cannot start: {exc}")
                    continue
                detail = stderr.decode("utf-8", "replace").strip()[-300:]
                if proc.returncode != 0:
                    errors.append(
                        f"{display}/{backend}: "
                        + (detail or f"exited {proc.returncode}")
                    )
                    continue
                try:
                    size = output.stat().st_size
                except OSError:
                    errors.append(f"{display}/{backend}: produced no output")
                    continue
                if size <= 0:
                    errors.append(f"{display}/{backend}: produced an empty file")
                    continue
                if size > MAX_SCREENSHOT_OUTPUT_BYTES:
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(
                        f"{display}/{backend}: screenshot exceeds "
                        f"{MAX_SCREENSHOT_OUTPUT_BYTES} bytes"
                    )
                    continue
                dimensions = self._png_dimensions(output)
                if dimensions is None:
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(f"{display}/{backend}: output is not a valid PNG")
                    continue
                if backend == "spectacle" and await self._image_is_effectively_black(
                    output
                ):
                    with contextlib.suppress(OSError):
                        output.unlink()
                    errors.append(f"{display}/{backend}: produced a black screenshot")
                    continue
                with contextlib.suppress(OSError):
                    os.chmod(output, 0o600)
                width, height = dimensions
                return {
                    "path": str(output),
                    "bytes": size,
                    "width": width,
                    "height": height,
                    "display": display,
                    "backend": backend,
                    "mime": "image/png",
                    "include_cursor": include_cursor,
                }
        with contextlib.suppress(OSError):
            output.unlink()
        detail = "; ".join(errors[-3:]) or "unknown error"
        raise ValueError(f"screenshot capture failed: {detail}")


    async def _image_is_effectively_black(self, path: Path) -> bool:
        ffmpeg = shutil.which(self.config.ffmpeg_bin)
        if not ffmpeg:
            return False
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                "scale=1:1",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (OSError, asyncio.TimeoutError):
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            return False
        if proc.returncode != 0 or len(stdout) < 3:
            return False
        return all(value <= 2 for value in stdout[:3])
