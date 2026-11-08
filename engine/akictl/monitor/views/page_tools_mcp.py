"""Page 5: Tool ownership matrix, native MCP probe status, and sandbox boundaries."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..presence_inspector import load_tool_matrix
from ..state import MonitorState


def render_tools_mcp_page(state: MonitorState, width: int = 120) -> Panel:
    """Render Page 5: Tools & MCP Radar."""
    matrix = load_tool_matrix()

    table = Table(title="[bold cyan]🛠️ 智能体专属工具所有权图谱 (Tool Ownership Matrix)[/]", expand=True, box=None)
    table.add_column("智能体", width=8, style="bold")
    table.add_column("职责定位", width=22)
    table.add_column("专属排他工具 (Exclusive Tools)", style="cyan")
    table.add_column("受限/禁用 (Disallowed)", style="dim red")

    for agent, info in matrix.items():
        ex_str = ", ".join(info["exclusive"])
        dis_str = ", ".join(info["disallowed"]) or "无"
        table.add_row(agent, info["role"], ex_str, dis_str)

    mcp_text = Text()
    mcp_text.append("\n❖ 原生 MCP 探针与沙箱隔离 (Native MCP Probes & Sandboxing):\n", style="bold cyan")
    mcp_text.append("• Natsu 专属 MCP 探针命令: vendor/mcps/.venv/bin/mcps\n", style="white")
    mcp_text.append("• 探针配置文件: vendor/mcps/config/recon.yaml (仅限本地授权范围审计)\n", style="white")
    mcp_text.append("• 写入沙箱 (Bubblewrap / Writable Policy): 限制常规写操作只能落在 /tmp 与 ~/.local/share/tmp。\n", style="dim")

    grid = Table.grid(expand=True)
    grid.add_column("Main")
    grid.add_row(table)
    grid.add_row(mcp_text)

    return Panel(
        grid,
        title=Text("🛠️ 工具沙箱与 MCP 探针矩阵 (Page 5)", style="bold green"),
        subtitle=Text("按 1-6 切换页面 | Tab 切换焦点", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
    )
