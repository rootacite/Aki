"""Page 2: Agent Hub, MemFS Git stream, chat history, solo memories, and context cards."""

from __future__ import annotations

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..agent_inspector import (
    load_agent_snapshots,
    load_channel_history,
    load_conversation_context,
    load_solo_memories,
)
from ..state import MonitorState
from ..theme import (
    BADGE_GROUP,
    BADGE_OFFLINE,
    BADGE_SOLO,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_PRIMARY,
    COLOR_SECONDARY,
    COLOR_SUCCESS,
    COLOR_WARNING,
)

AGENT_SUBTABS = [
    "概览控制",
    "MemFS Git",
    "群聊记录",
    "独处回忆",
    "会话上下文",
]


def render_agent_hub_page(state: MonitorState, width: int = 120) -> Panel:
    """Render the dual-pane layout for Page 2."""
    if not state.agents:
        state.agents = load_agent_snapshots()

    left_panel = _render_agent_roster(state)
    right_panel = _render_agent_details(state, width=max(50, width - 36))

    outer_table = Table.grid(expand=True)
    outer_table.add_column("Left", width=32)
    outer_table.add_column("Right")
    outer_table.add_row(left_panel, right_panel)

    return Panel(
        outer_table,
        title=Text("👥 智能体档案与交互控制 (Page 2)", style="bold magenta"),
        subtitle=Text("快捷键: [M]换模型 | [R]Reasoning | [C]Compact | [S]SoftReset | [N]NSFW开关", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
        padding=(0, 0),
    )


def _render_agent_roster(state: MonitorState) -> Panel:
    """Render the left list of agents."""
    table = Table(box=None, expand=True, padding=(0, 1), show_header=True)
    table.add_column("智能体", width=10)
    table.add_column("状态", width=8, justify="right")

    for profile, snap in state.agents.items():
        is_selected = (profile == state.selected_agent_profile)
        row_style = "bold #11111b on #89b4fa" if is_selected else ""

        if snap.status == "group":
            st_badge = BADGE_GROUP
        elif snap.status == "solo":
            st_badge = BADGE_SOLO
        else:
            st_badge = BADGE_OFFLINE

        name_text = Text(f"{snap.name} ({profile[:1].upper()})")
        table.add_row(name_text, st_badge, style=row_style)

    is_focused = (state.active_focus == "list")
    return Panel(
        table,
        title="[bold]成员名册 (Agents)[/]",
        border_style="#89b4fa" if is_focused else "#45475a",
        padding=(0, 0),
    )


def _render_agent_details(state: MonitorState, width: int = 80) -> Panel:
    """Render the right detail panel with 5 tabs."""
    profile = state.selected_agent_profile
    snap = state.agents.get(profile)
    if not snap:
        return Panel(Text("未选中智能体", style="dim"), border_style="#45475a")

    # Tab bar
    tab_row = Text()
    for idx, name in enumerate(AGENT_SUBTABS):
        is_active = (idx == state.selected_agent_tab)
        if is_active:
            tab_row.append(f" [{idx + 1}.{name}] ", style="bold #11111b on #89b4fa")
        else:
            tab_row.append(f"  {idx + 1}.{name}  ", style="dim #a6adc8")
    tab_row.append("\n" + "─" * (width - 4), style="dim")

    # Content
    content = Text()
    if state.selected_agent_tab == 0:
        content = _view_agent_overview(snap)
    elif state.selected_agent_tab == 1:
        content = _view_memfs_git(snap)
    elif state.selected_agent_tab == 2:
        content = _view_channel_history(snap)
    elif state.selected_agent_tab == 3:
        content = _view_solo_memories(snap, state.nsfw_unlocked)
    elif state.selected_agent_tab == 4:
        content = _view_context_cards(snap, state.context_view_mode)

    main_grid = Table.grid(expand=True)
    main_grid.add_column("Body")
    main_grid.add_row(tab_row)
    main_grid.add_row(content)

    is_focused = (state.active_focus == "detail")
    return Panel(
        main_grid,
        title=f"[bold magenta]{snap.name} ({snap.profile}) 档案[/]",
        border_style="#89b4fa" if is_focused else "#45475a",
        padding=(0, 1),
    )


def _view_agent_overview(snap) -> Text:
    """Subtab 0: Overview and Control Actions."""
    text = Text()
    text.append(f"❖ 智能体角色与设定:\n", style="bold cyan")
    text.append(f"  {snap.description or '未设置角色描述'}\n\n", style="white")

    text.append("❖ 运行与模型配置:\n", style="bold cyan")
    text.append(f"• 当前状态: {snap.status.upper()}\n", style="green" if snap.status == "group" else "yellow")
    text.append(f"• 群聊模型: {snap.group_model or '默认'}\n", style="white")
    text.append(f"• 独处模型: {snap.solo_model or '跟随群聊'}\n", style="white")
    text.append(f"• 推理深度: {snap.reasoning_effort}\n", style="white")
    text.append(f"• Token 预算: 群聊 {snap.live_budget_tokens:,} tokens | 独处 {snap.solo_budget_tokens:,} tokens\n\n", style="dim")

    text.append("❖ 核心快捷控制指令 (按对应键即可触发):\n", style="bold yellow")
    text.append("  [M] 切换上游模型 (独处/群聊)\n", style="cyan")
    text.append("  [R] 设置 Reasoning 深度 (low/medium/high)\n", style="cyan")
    text.append("  [C] 执行 Compact 上下文压缩 (支持 omit 工具结果)\n", style="cyan")
    text.append("  [S] 执行 Soft Reset 重置上下文 (含安全可逆警告)\n", style="cyan")

    return text


def _view_memfs_git(snap) -> Text:
    """Subtab 1: MemFS path and Git commit log."""
    text = Text()
    text.append("❖ MemFS 记忆文件系统路径:\n", style="bold cyan")
    text.append(f"  {snap.memfs_path or '未找到 MemFS 路径'}\n\n", style="dim")

    text.append("❖ 近期 Git 记忆提交流 (Recent Commits):\n", style="bold cyan")
    if not snap.git_commits:
        text.append("  [暂无 Git 提交记录]\n", style="dim")
    else:
        for c in snap.git_commits:
            text.append(f"  • [{c['hash']}] ", style="bold yellow")
            text.append(f"({c['date']}) ", style="dim")
            text.append(f"{c['message']}\n", style="white")

    return text


def _view_channel_history(snap) -> Text:
    """Subtab 2: Channel history with token budget translucency."""
    is_solo = (snap.status == "solo")
    entries = load_channel_history(snap.profile, snap.live_budget_tokens, is_solo=is_solo)
    text = Text()
    title_str = f"❖ 独处频道记录 ({snap.profile}.solo.jsonl)" if is_solo else "❖ 群聊频道记录"
    text.append(f"{title_str} (近 {len(entries)} 条，超出预算部分半透明呈现):\n\n", style="bold cyan")

    if not entries:
        text.append("[暂无频道历史消息]\n", style="dim")
        return text

    for item in entries[-25:]:
        in_budget = item.get("in_budget", True)
        sender = item.get("sender", "user")
        ts = item.get("timestamp", "")
        content = item.get("text", "")

        if in_budget:
            text.append(f"[{ts[:16]}] ", style="dim")
            text.append(f"<{sender}>: ", style="bold green" if sender != snap.name else "bold cyan")
            text.append(f"{content}\n", style="white")
        else:
            # Translucent out-of-budget styling
            text.append(f"[{ts[:16]}] <{sender}>: {content}  [dim italic](超出预算 / 未送入上下文)[/]\n", style="dim #666666")

    return text


def _view_solo_memories(snap, nsfw_unlocked: bool) -> Text:
    """Subtab 3: Solo memories with NSFW toggle and token budget dimming."""
    sessions = load_solo_memories(snap.profile, snap.solo_budget_tokens, nsfw_unlocked)
    text = Text()
    status_nsfw = "[bold green]已解锁[/]" if nsfw_unlocked else "[bold yellow]已遮罩 (按 N 解锁)[/]"
    text.append(f"❖ 独处回忆时间线 (共 {len(sessions)} 次独处 | NSFW状态: {status_nsfw}):\n\n", style="bold cyan")

    if not sessions:
        text.append("[暂无独处回忆记录]\n", style="dim")
        return text

    for s in sessions[-12:]:
        in_budget = s.get("in_budget", True)
        nsfw_tag = "[red]NSFW[/] " if s.get("nsfw") else ""
        started = s.get("started_at", "")[:16]
        ended = s.get("ended_at", "")[:16]
        summary = s.get("summary", "")

        if in_budget:
            text.append(f"• [{started} ~ {ended}] {nsfw_tag}({s['entries_count']}轮):\n", style="bold yellow")
            text.append(f"   {summary}\n\n", style="white")
        else:
            # Translucent out-of-budget
            text.append(f"• [{started} ~ {ended}] {nsfw_tag}({s['entries_count']}轮): {summary} [dim italic](超预算)[/]\n\n", style="dim #666666")

    return text


def _view_context_cards(snap, mode: str) -> Text:
    """Subtab 4: Human-readable context bubble cards."""
    is_solo = (mode == "solo")
    cards = load_conversation_context(snap.agent_id, is_solo=is_solo)

    text = Text()
    mode_str = "🟣 独处会话上下文" if is_solo else "🟢 群聊主会话上下文"
    text.append(f"❖ {mode_str} (共 {len(cards)} 条消息 | 按 Tab 可切换群聊/独处):\n\n", style="bold cyan")

    if not cards:
        text.append("[上下文为空]\n", style="dim")
        return text

    for c in cards[-15:]:
        role = c["role"]
        in_ctx = c["in_context"]
        content = c["text"]
        ts = c["timestamp"][:19].replace("T", " ")

        # Badges
        badge_style = "bold green" if role == "assistant" else ("bold yellow" if role == "user" else "bold magenta")
        ctx_badge = "[bold green]ACTIVE[/]" if in_ctx else "[dim]ARCHIVED[/]"

        text.append(f"┌─ [{role.upper()}]  [{ts}]  {ctx_badge}\n", style=badge_style if in_ctx else "dim")
        # Format text lines
        for line in content.splitlines()[:6]:
            text.append(f"│  {line}\n", style="white" if in_ctx else "dim #777777")
        if len(content.splitlines()) > 6:
            text.append("│  ...[长文本已折叠]\n", style="dim")

        # Tool calls
        if c.get("tool_calls"):
            for tc in c["tool_calls"]:
                fn = tc.get("function") or {}
                text.append(f"│  ⚙ 工具调用: {fn.get('name')}\n", style="cyan")

        text.append("└────────────────────────────────────────\n\n", style="dim")

    return text
