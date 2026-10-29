"""Command line entry for the akid gateway process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path
from engine.aki_config import ConfigError
from engine.live_context import clamp_budget_tokens
from engine.mqtt_settings import load_mqtt_server
from .config_env import build_config_from_env
from .paths import default_memory_dir



async def _serve(config: GatewayConfig) -> None:
    from engine.akid.server import GatewayServer
    server = GatewayServer(config)
    await server.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await server.stop()



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aki local gateway")
    parser.add_argument("--profile", help="profile name from ~/.aki/<profile>.conf")
    parser.add_argument("--socket", help="Unix socket path")
    parser.add_argument("--agent-id", help="Letta agent id")
    parser.add_argument("--conversation-id", help="Letta conversation id")
    parser.add_argument("--letta-bin", help="Path to the letta executable")
    parser.add_argument("--mqtt-host", help="tokifactor MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, help="tokifactor MQTT broker port")
    parser.add_argument("--mqtt-topic", help="tokifactor topic")
    parser.add_argument("--mqtt-magic", help="tokifactor magic string")
    parser.add_argument("--mqtt-state", help="Persistent MQTT settings JSON path")
    parser.add_argument("--no-mqtt", action="store_true", help="disable the MQTT channel")
    parser.add_argument("--bwrap", help="Path to the bwrap executable")
    parser.add_argument("--writable-state", help="Writable directory registry JSON path")
    parser.add_argument("--letta-home", help="Letta internal home directory")
    parser.add_argument("--avatar-path", help="Aki avatar image path")
    parser.add_argument("--no-camera", action="store_true", help="disable the camera tool")
    parser.add_argument("--camera-device", help="V4L2 camera device (default /dev/video0)")
    parser.add_argument("--camera-width", type=int, help="default camera capture width")
    parser.add_argument("--camera-height", type=int, help="default camera capture height")
    parser.add_argument("--camera-output-dir", help="directory for captured photos")
    parser.add_argument("--ffmpeg-bin", help="Path to the ffmpeg executable")
    parser.add_argument("--ffprobe-bin", help="Path to the ffprobe executable")
    parser.add_argument("--no-screenshot", action="store_true", help="disable the screenshot tool")
    parser.add_argument("--grim-bin", help="Path to the grim executable")
    parser.add_argument("--spectacle-bin", help="Path to the spectacle executable")
    parser.add_argument("--screenshot-output-dir", help="directory for desktop screenshots")
    parser.add_argument(
        "--screenshot-wayland-display",
        help="preferred WAYLAND_DISPLAY for grim (default: auto-detect)",
    )
    parser.add_argument(
        "--screenshot-require-approval",
        action="store_true",
        help="require approval for desktop_take_screenshot (default: auto-approve)",
    )
    parser.add_argument(
        "--no-live-context", action="store_true", help="disable desktop live context"
    )
    parser.add_argument(
        "--live-context-budget-tokens",
        type=int,
        help="live context token ceiling (default: 65536)",
    )
    parser.add_argument("--zsh-history", help="path to the zsh history file")
    parser.add_argument("--ps-bin", help="Path to the ps executable")
    parser.add_argument("--qdbus-bin", help="Path to qdbus6/qdbus")
    parser.add_argument("--no-layer", action="store_true", help="disable the aki-layer window")
    parser.add_argument("--layer-bin", help="Path to the aki-layer executable")
    parser.add_argument(
        "--no-layer-notify",
        action="store_true",
        help="disable chat-message delivery to aki-layer bubbles",
    )
    parser.add_argument("--layer-socket", help="Path to the aki-layer control socket")
    parser.add_argument(
        "--layer-preview-chars",
        type=int,
        help="maximum notification bubble preview length (default 80)",
    )
    parser.add_argument(
        "--live-context-timeout",
        type=float,
        help="per-source timeout for live context collection",
    )
    parser.add_argument("--no-sandbox", action="store_true", help="disable the OS sandbox (unsafe)")
    parser.add_argument(
        "--tokifactor-image-require-approval",
        action="store_true",
        help="require approval for tokifactor_send_image (default: auto-approve)",
    )
    parser.add_argument("--log-level", default=os.environ.get("AKI_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = build_config_from_env(args.profile)
    except (ConfigError, OSError) as exc:
        print(f"cannot load profile config: {exc}", file=sys.stderr)
        return 2
    if args.socket:
        config.socket_path = Path(args.socket).expanduser()
    if args.agent_id:
        config.agent_id = args.agent_id
        if config.memory_dir is None:
            config.memory_dir = default_memory_dir(config.agent_id)
    if args.conversation_id:
        config.conversation_id = args.conversation_id
    if args.letta_bin:
        config.letta_bin = args.letta_bin
    if args.mqtt_host:
        config.mqtt_host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt_port = args.mqtt_port
    if args.mqtt_topic:
        config.mqtt_topic = args.mqtt_topic
    if args.mqtt_magic:
        config.mqtt_magic = args.mqtt_magic
    if args.mqtt_state:
        config.mqtt_state = Path(args.mqtt_state).expanduser()
        saved_host, saved_port = load_mqtt_server(config.mqtt_state)
        config.mqtt_host = saved_host or config.mqtt_host
        config.mqtt_port = saved_port or config.mqtt_port
    if args.no_mqtt:
        config.mqtt_enabled = False
    if args.bwrap:
        config.bwrap_bin = args.bwrap
    if args.writable_state:
        config.writable_state = Path(args.writable_state).expanduser()
    if args.letta_home:
        config.letta_home = Path(args.letta_home).expanduser()
    if args.avatar_path:
        config.avatar_path = Path(args.avatar_path).expanduser()
    if args.no_camera:
        config.camera_enabled = False
    if args.camera_device:
        config.camera_device = args.camera_device
    if args.camera_width:
        config.camera_width = args.camera_width
    if args.camera_height:
        config.camera_height = args.camera_height
    if args.camera_output_dir:
        config.camera_output_dir = Path(args.camera_output_dir).expanduser()
    if args.ffmpeg_bin:
        config.ffmpeg_bin = args.ffmpeg_bin
    if args.ffprobe_bin:
        config.ffprobe_bin = args.ffprobe_bin
    if args.no_screenshot:
        config.screenshot_enabled = False
    if args.grim_bin:
        config.grim_bin = args.grim_bin
    if args.spectacle_bin:
        config.spectacle_bin = args.spectacle_bin
    if args.screenshot_output_dir:
        config.screenshot_output_dir = Path(args.screenshot_output_dir).expanduser()
    if args.screenshot_wayland_display:
        config.screenshot_wayland_display = args.screenshot_wayland_display
    if args.screenshot_require_approval:
        config.screenshot_require_approval = True
    if args.no_live_context:
        config.live_context_enabled = False
    if args.live_context_budget_tokens is not None:
        config.live_context_budget_tokens = clamp_budget_tokens(
            args.live_context_budget_tokens
        )
    if args.zsh_history:
        config.zsh_history_path = Path(args.zsh_history).expanduser()
    if args.ps_bin:
        config.ps_bin = args.ps_bin
    if args.qdbus_bin:
        config.qdbus_bin = args.qdbus_bin
    if args.no_layer:
        config.layer_enabled = False
    if args.layer_bin:
        config.layer_bin = Path(args.layer_bin).expanduser()
    if args.no_layer_notify:
        config.layer_notify_enabled = False
    if args.layer_socket:
        config.layer_socket = Path(args.layer_socket).expanduser()
    if args.layer_preview_chars is not None:
        config.layer_preview_chars = max(1, args.layer_preview_chars)
    if args.live_context_timeout is not None:
        config.live_context_timeout = args.live_context_timeout
    if args.no_sandbox:
        config.sandbox_enabled = False
    if args.tokifactor_image_require_approval:
        config.tokifactor_image_require_approval = True
    try:
        asyncio.run(_serve(config))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
