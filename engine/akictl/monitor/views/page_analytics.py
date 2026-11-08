"""Page 6: Cross-agent token usage, KV cache cumulative savings, and latency metrics."""

from __future__ import annotations

from rich.bar import Bar
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..state import MonitorState


def render_analytics_page(state: MonitorState, width: int = 120) -> Panel:
    """Render Page 6: Token & Latency Analytics."""
    calls = list(state.api_calls)

    # 1. Group statistics by profile
    profile_stats = {}
    for c in calls:
        p = c.profile
        if p not in profile_stats:
            profile_stats[p] = {
                "calls": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "completion_tokens": 0,
                "total_duration": 0,
            }
        st = profile_stats[p]
        st["calls"] += 1
        st["prompt_tokens"] += c.prompt_tokens
        st["cached_tokens"] += c.cached_tokens
        st["completion_tokens"] += c.completion_tokens
        st["total_duration"] += c.duration_ms

    table = Table(title="[bold cyan]📊 智能体调用与 Token 消耗大盘[/]", expand=True, box=None)
    table.add_column("Agent", style="bold")
    table.add_column("Calls", justify="right")
    table.add_column("Prompt Tokens", justify="right", style="cyan")
    table.add_column("Cached Tokens", justify="right", style="green")
    table.add_column("Cache 比例", justify="right", style="yellow")
    table.add_column("Avg Latency", justify="right", style="magenta")

    for p, st in profile_stats.items():
        total_p = st["prompt_tokens"]
        total_c = st["cached_tokens"]
        rate = (total_c / total_p * 100.0) if total_p > 0 else 0.0
        avg_lat = (st["total_duration"] // max(1, st["calls"]))
        table.add_row(
            p.upper(),
            f"{st['calls']}",
            f"{total_p:,}",
            f"{total_c:,}",
            f"{rate:.1f}%",
            f"{avg_lat} ms",
        )

    # 2. Cumulative summary
    summary_text = Text()
    total_calls = len(calls)
    total_prompt = sum(c.prompt_tokens for c in calls)
    total_cached = sum(c.cached_tokens for c in calls)
    overall_rate = (total_cached / total_prompt * 100.0) if total_prompt > 0 else 0.0

    summary_text.append("\n❖ 全局 KV Cache 累计节省效益 (Global KV Cache Efficiency):\n", style="bold cyan")
    summary_text.append(f"• 累计捕获请求: {total_calls} 次\n", style="white")
    summary_text.append(f"• 累计传输 Prompt Tokens: {total_prompt:,} tokens\n", style="white")
    summary_text.append(f"• 累计命中缓存 Tokens: {total_cached:,} tokens (命中率 {overall_rate:.1f}%)\n", style="bold green")
    summary_text.append(f"• 相当于直接省去约 {total_cached * 0.0005:.1f} 秒 Prefill 预填充等待时间！\n", style="dim yellow")

    grid = Table.grid(expand=True)
    grid.add_column("Main")
    grid.add_row(table)
    grid.add_row(summary_text)

    return Panel(
        grid,
        title=Text("📊 消耗、时延与模型时序大盘 (Page 6)", style="bold cyan"),
        subtitle=Text("按 1-6 切换页面 | Tab 切换焦点", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
    )
