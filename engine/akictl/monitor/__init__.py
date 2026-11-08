"""akictl monitor: Modern TUI dashboard for live API calls and agent management."""

from __future__ import annotations

import asyncio
from .app import MonitorApp


def launch_monitor(initial_profile: str = "all") -> int:
    """Launch the TUI monitor application."""
    app = MonitorApp(initial_profile=initial_profile)
    try:
        asyncio.run(app.run_async())
        return 0
    except (KeyboardInterrupt, SystemExit):
        return 0
    except Exception as exc:
        import sys
        print(f"monitor error: {exc}", file=sys.stderr)
        return 1
