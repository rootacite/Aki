"""Theme, colors, and Rich-to-prompt_toolkit rendering helpers."""

from __future__ import annotations

from io import StringIO
from typing import Any
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from rich.console import Console

# Base style for prompt_toolkit containers and windows
PT_STYLE = Style.from_dict({
    "sidebar": "bg:#1e1e2e #cdd6f4",
    "sidebar.item": "#a6adc8",
    "sidebar.item.active": "bold #89b4fa bg:#313244",
    "sidebar.badge": "bold #f38ba8",
    "frame.border": "#45475a",
    "frame.border.focused": "#89b4fa",
    "frame.label": "bold #cdd6f4",
    "dialog": "bg:#181825 #cdd6f4",
    "dialog.body": "bg:#1e1e2e #cdd6f4",
    "dialog.border": "#89b4fa",
    "status": "bg:#11111b #a6adc8",
    "status.key": "bold #89b4fa",
    "status.alert": "bold #f38ba8",
})

# Color constants
COLOR_PRIMARY = "#89b4fa"      # Blue
COLOR_SECONDARY = "#cba6f7"    # Mauve
COLOR_SUCCESS = "#a6e3a1"      # Green
COLOR_WARNING = "#f9e2af"      # Yellow
COLOR_DANGER = "#f38ba8"       # Red
COLOR_MUTED = "#6c7086"        # Dim / Gray
COLOR_TEXT = "#cdd6f4"         # White / Normal
COLOR_BG_CARD = "#181825"
COLOR_BG_ALT = "#1e1e2e"

# Status Badges
BADGE_GROUP = "[bold green]🟢 群聊[/]"
BADGE_SOLO = "[bold magenta]🟣 独处[/]"
BADGE_OFFLINE = "[dim red]⚪ 离线[/]"


def render_rich_to_ansi(renderable: Any, width: int = 100) -> ANSI:
    """Render any Rich renderable (Panel, Table, Text) to prompt_toolkit ANSI."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        width=max(16, width),
        highlight=False,
    )
    console.print(renderable)
    return ANSI(output.getvalue())
