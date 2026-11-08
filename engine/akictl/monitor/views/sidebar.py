"""Vertical Sidebar navigation rendering for Page 1-6."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..state import MonitorState
from ..theme import COLOR_PRIMARY, COLOR_MUTED, COLOR_TEXT


NAV_PAGES = [
    ("1", "📡 API 监控", "API & KV Cache"),
    ("2", "👥 智能中枢", "Agent Hub"),
    ("3", "🧠 自主反思", "Reflection"),
    ("4", "🔐 态势与锁", "Presence & Lock"),
    ("5", "🛠️ 工具探针", "Tools & MCP"),
    ("6", "📊 消耗时延", "Analytics"),
]


def render_sidebar(state: MonitorState) -> Panel:
    """Render the vertical navigation sidebar."""
    table = Table.grid(padding=(1, 0))
    table.add_column("Icon", width=16)

    # Title
    table.add_row(Text("❖ AKI MONITOR", style="bold cyan"))
    table.add_row(Text("─" * 15, style="dim"))

    for key, name, sub in NAV_PAGES:
        page_num = int(key)
        is_active = (state.active_page == page_num)
        is_focused = (state.active_focus == "sidebar" and is_active)

        if is_focused:
            style = "bold #11111b on #89b4fa"
            key_text = f" [{key}] "
            title_text = f" {name}\n   {sub}"
        elif is_active:
            style = "bold #89b4fa on #313244"
            key_text = f" [{key}] "
            title_text = f" {name}\n   {sub}"
        else:
            style = "#a6adc8"
            key_text = f"  {key}  "
            title_text = f" {name}"

        row_content = Text(key_text, style="bold magenta" if not is_active else "bold yellow")
        row_content.append(title_text, style=style)
        table.add_row(row_content)

    table.add_row(Text("─" * 15, style="dim"))
    # Quick status indicators
    live_calls = len(state.api_calls)
    pause_label = "⏸ 暂停" if state.paused else "▶ 实时"
    table.add_row(Text(f"捕获: {live_calls}条\n状态: {pause_label}", style="dim green" if not state.paused else "dim yellow"))

    return Panel(
        table,
        border_style="bold #89b4fa" if state.active_focus == "sidebar" else "#45475a",
        padding=(0, 1),
    )
