"""Page 4: Presence radar, late night matrix, and execution lock status."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..presence_inspector import load_execution_lock_status, load_presence_status
from ..state import MonitorState


def render_presence_lock_page(state: MonitorState, width: int = 120) -> Panel:
    """Render Page 4: Presence & Lock Matrix."""
    presence = load_presence_status()
    lock = load_execution_lock_status()

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column("Col1", ratio=1)
    grid.add_column("Col2", ratio=1)

    # 1. Presence Info
    p_text = Text()
    p_text.append("❖ 用户生活态势感知 (Presence Monitor):\n\n", style="bold cyan")
    act_min = int(presence.get("active_seconds", 0) // 60)
    p_text.append(f"• 在线活跃状态: {'🟢 在线' if presence.get('last_active') else '⚪ 离线/暂离'}\n", style="bold green" if presence.get('last_active') else "dim")
    p_text.append(f"• 本次连续工作时长: {act_min} 分钟\n", style="white")
    p_text.append(f"• 疲劳提醒阈值: {int(presence.get('next_work_threshold', 7200) // 60)} 分钟\n", style="white")
    p_text.append(f"• 最近一次离开时长: {int(presence.get('idle_seconds', 0) // 60)} 分钟\n", style="white")
    p_text.append(f"• 最近一次归来时刻: {presence.get('last_return_at') or '无'}\n\n", style="dim")

    # Late night
    dates = presence.get("late_night_dates", [])
    p_text.append(f"❖ 熬夜记录与夜聊雷达 (Late Night Matrix):\n", style="bold yellow")
    p_text.append(f"• 近期熬夜记录天数: {len(dates)} 天 ({', '.join(dates[-4:]) if dates else '无'})\n", style="white")
    p_text.append("• 夜聊触发机制: 晚间 23:00 ~ 04:00 间检测到疲劳或归来时触发私语问候。\n", style="dim")

    left_panel = Panel(p_text, title="[bold]生活态势感知[/]", border_style="#45475a")

    # 2. Execution Lock & Wait-For Chain
    l_text = Text()
    l_text.append("❖ 整机跨进程执行锁 (Execution Lock):\n\n", style="bold cyan")
    l_text.append(f"• 锁文件路径: {lock.get('lock_path')}\n", style="dim")
    held = lock.get("held", False)
    l_text.append(f"• 锁定状态: {'🔴 已锁定 (有智能体正在执行写操作)' if held else '🟢 空闲 (可安全获取写操作锁)'}\n", style="bold red" if held else "bold green")
    l_text.append(f"• 当前持有者: {lock.get('holder')}\n\n", style="white")

    l_text.append("❖ 群聊发言前序依赖链 (Wait-For Chain):\n", style="bold cyan")
    l_text.append("• Haru (春) ──► 必须等待: Aki (秋), Fuyu (冬)\n", style="yellow")
    l_text.append("• Natsu (夏) ──► 保持静默，仅响应显式 @Natsu\n", style="magenta")
    l_text.append("• Aki (秋)   ──► 首位发起 / 自由发言无依赖\n", style="green")

    right_panel = Panel(l_text, title="[bold]锁与协作链[/]", border_style="#45475a")

    grid.add_row(left_panel, right_panel)

    return Panel(
        grid,
        title=Text("🔐 态势感知、独处与整机执行锁 (Page 4)", style="bold yellow"),
        subtitle=Text("按 1-6 切换页面 | Tab 切换焦点", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
    )
