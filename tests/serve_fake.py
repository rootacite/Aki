#!/usr/bin/env python3
"""Run a fake-backed akid server for CLI integration tests."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.akid import GatewayConfig, GatewayServer
from engine.letta_process import LettaProcess


async def run(socket_path: Path, memory_dir: Path, writable_state: Path | None) -> None:
    repo_cwd = Path(os.environ.get("AKI_REPO_CWD", ROOT)).expanduser()
    username_path = (
        Path(os.environ["AKI_USERNAME_PATH"]).expanduser()
        if os.environ.get("AKI_USERNAME_PATH")
        else None
    )
    username = os.environ.get("AKI_USERNAME", "")
    username_source = "default"
    if not username and username_path is not None and username_path.is_file():
        username = username_path.read_text(encoding="utf-8").strip() or "Master"
        username_source = "legacy-profile-file"
    if not username:
        username = "Master"
    config = GatewayConfig(
        socket_path=socket_path,
        agent_id="test-agent",
        conversation_id="default",
        letta_bin="letta",
        cwd=repo_cwd,
        memory_dir=memory_dir,
        backup_dir=socket_path.parent / "backups",
        backend_dir=socket_path.parent / "backend",
        queue_max=8,
        approval_timeout=2.0,
        writable_state=writable_state,
        mcp_native_tools_enabled=False,
        mcp_native_state=(
            Path(os.environ["AKI_MCP_NATIVE_STATE"]).expanduser()
            if os.environ.get("AKI_MCP_NATIVE_STATE")
            else None
        ),
        username=username,
        username_source=username_source,
        username_path=username_path,
        profile_config_path=(
            Path(os.environ["AKI_PROFILE_CONFIG"]).expanduser()
            if os.environ.get("AKI_PROFILE_CONFIG")
            else socket_path.parent / "aki.conf"
        ),
    )

    def factory(on_message, on_ready, on_exit):
        return LettaProcess(
            [sys.executable, str(ROOT / "tests" / "fake_letta.py")],
            cwd=str(ROOT),
            env=os.environ.copy(),
            on_message=on_message,
            on_ready=on_ready,
            on_exit=on_exit,
            backoff=(0.1, 0.1),
        )

    server = GatewayServer(config, backend_factory=factory)
    await server.start()
    if not await server.backend.wait_ready(5.0):
        raise RuntimeError("fake backend did not become ready")
    print(f"READY {socket_path}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--memory", required=True)
    parser.add_argument("--writable-state")
    args = parser.parse_args()
    asyncio.run(
        run(
            Path(args.socket),
            Path(args.memory),
            Path(args.writable_state) if args.writable_state else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
