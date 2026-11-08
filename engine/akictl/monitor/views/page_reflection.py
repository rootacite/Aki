"""Page 3: Autonomous Reflection Progress, Core Memory Inspector, and MemFS Tree."""

from __future__ import annotations

from rich.bar import Bar
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..agent_inspector import get_backend_dir
from ..presence_inspector import load_reflection_states
from ..state import MonitorState


def render_reflection_page(state: MonitorState, width: int = 120) -> Panel:
    """Render Page 3: Reflection & Core Memory Radar."""
    states = load_reflection_states()
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column("Col1", width=58)
    grid.add_column("Col2")

    # Left: Reflection progress per agent
    left_table = Table(title="[bold cyan]🧠 自主反思进度 (Reflection Rhythm)[/]", expand=True, box=None, padding=(0, 1))
    left_table.add_column("智能体 (会话)", width=14, no_wrap=True)
    left_table.add_column("累计", width=7, justify="right", no_wrap=True)
    left_table.add_column("进度", width=8, no_wrap=True)
    left_table.add_column("待反思", width=8, justify="right", no_wrap=True)
    left_table.add_column("状态", width=14, no_wrap=True)

    for st in states:
        total = st["total_steps"]
        pending = st["pending_steps"]
        threshold = 25  # default reflection step threshold
        bar = Bar(size=float(threshold), begin=0.0, end=float(min(threshold, pending)), width=6, color="magenta")

        name = st.get("agent_name") or st.get("profile", "").capitalize()
        p = st.get("profile", "")
        p_color = "cyan" if p == "aki" else ("yellow" if p == "fuyu" else ("green" if p == "haru" else "magenta"))
        badge = "[bold magenta]独处[/]" if st.get("is_solo") else "[bold green]群聊[/]"
        agent_label = f"[{p_color}]{name}[/] {badge}"

        if pending >= threshold:
            status_text = "[bold red]待触发 (已达阈值)[/]"
        elif pending >= threshold - 5:
            status_text = f"[bold yellow]即将触发 (差{threshold - pending}步)[/]"
        else:
            status_text = f"[dim green]正常 (差{threshold - pending}步)[/]"

        left_table.add_row(
            agent_label,
            f"{total} 步",
            bar,
            f"{pending}/{threshold}",
            status_text,
        )

    # Right: Core Memory Live Inspector
    right_text = Text()
    right_text.append("❖ 核心记忆演化与热区 (Core Memory Blocks):\n\n", style="bold cyan")
    right_text.append("• <human>: 记录关于您的事实、习惯偏好、技术重心与隐私边界。\n", style="white")
    right_text.append("• <persona>: 记录当前智能体经过反思沉淀后的稳定人格与自我认知。\n", style="white")
    right_text.append("• 反思子代理会自动在独立 Git worktree 中分析近期对话，产生 commit 提交流。\n\n", style="dim")
    right_text.append("💡 提示: 当待反思步数达到阈值时，akid 将自动在后台调度反思子代理。\n", style="yellow dim")

    right_panel = Panel(right_text, title="[bold]自主进化机制[/]", border_style="#89b4fa")

    grid.add_row(Panel(left_table, border_style="#45475a"), right_panel)

    return Panel(
        grid,
        title=Text("🧠 自主反思与记忆进化树 (Page 3)", style="bold cyan"),
        subtitle=Text("按 1-6 切换页面 | Tab 切换焦点", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
    )
