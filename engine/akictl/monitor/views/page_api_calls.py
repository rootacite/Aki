"""Page 1: Live API Calls stream and deep KV cache inspection."""

from __future__ import annotations

import json
from rich.align import Align
from rich.bar import Bar
from rich.columns import Columns
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis import (
    calibrate_tokens,
    detect_fork,
    segment_prompt,
)
from ..state import ApiCallRecord, MonitorState
from ..theme import (
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_PRIMARY,
    COLOR_SECONDARY,
    COLOR_SUCCESS,
    COLOR_WARNING,
)

SUBTABS = [
    "分块校准",
    "分叉 Diff",
    "Cache/性能",
    "网络增量",
    "响应工具流",
    "Raw JSON",
]


def render_api_calls_page(state: MonitorState, width: int = 120) -> Panel:
    """Render the dual-pane layout for Page 1."""
    left_table = _render_calls_table(state)
    right_panel = _render_detail_panel(state, width=max(50, width - 42))

    outer_table = Table.grid(expand=True)
    outer_table.add_column("Left", width=35)
    outer_table.add_column("Right")
    outer_table.add_row(left_table, right_panel)

    return Panel(
        outer_table,
        title=Text("📡 API 监控与 KV Cache 深度透析 (Page 1)", style="bold cyan"),
        subtitle=Text("Tab: 切换聚焦 | Space: 暂停捕获 | [/] 切换子Tab | 1-6: 换页", style="dim"),
        border_style="#89b4fa" if state.active_focus != "sidebar" else "#45475a",
        padding=(0, 0),
    )


def _render_calls_table(state: MonitorState) -> Panel:
    """Render the left list of captured API calls."""
    calls = state.filtered_calls()
    table = Table(box=None, expand=True, padding=(0, 1), show_header=True)
    table.add_column("#", width=3, justify="right", style="dim", no_wrap=True)
    table.add_column("Agent", width=5, no_wrap=True)
    table.add_column("Tokens", width=7, justify="right", no_wrap=True)
    table.add_column("Cache", width=5, justify="right", no_wrap=True)
    table.add_column("Status", width=6, justify="center", no_wrap=True)

    if not calls:
        table.add_row("", "[dim]暂无捕获...[/]", "", "", "")
    else:
        for idx, call in enumerate(calls[-35:]):
            is_selected = (call.request_id == state.selected_call_id)
            row_style = "bold #11111b on #89b4fa" if is_selected else ""

            # Profile badge
            p_style = "cyan" if call.profile == "aki" else ("yellow" if call.profile == "fuyu" else ("green" if call.profile == "haru" else "magenta"))
            agent_str = f"[{p_style}]{call.profile[:5]}[/]"

            # Tokens
            t_str = f"{call.prompt_tokens // 1000}k" if call.prompt_tokens >= 1000 else str(call.prompt_tokens)

            # Cache
            c_rate = call.cache_rate
            c_str = f"{c_rate:.0f}%" if c_rate > 0 else "-"
            c_styled = f"[green]{c_str}[/]" if c_rate > 50 else (f"[yellow]{c_str}[/]" if c_rate > 0 else "[dim]-[/]")

            # Status
            if call.status == 200:
                s_str = f"[green]200[/]"
            elif call.status == 0:
                s_str = f"[yellow]...[/]"
            else:
                s_str = f"[red]{call.status}[/]"

            num_str = f"{call.seq or idx + 1}"
            table.add_row(num_str, agent_str, t_str, c_styled, s_str, style=row_style)

    is_focused = (state.active_focus == "list")
    return Panel(
        table,
        title=f"[bold]API Calls ({len(calls)})[/]",
        border_style="#89b4fa" if is_focused else "#45475a",
        padding=(0, 0),
    )


def _render_detail_panel(state: MonitorState, width: int = 80) -> Panel:
    """Render the right detail area with 6 tabs."""
    record = state.get_selected_call()
    if not record:
        return Panel(
            Align.center(Text("\n\n未选中任何 API Call\n等待请求接入...", style="dim")),
            title="API 详情",
            border_style="#45475a",
        )

    # Subtabs header
    tab_row = Text()
    for idx, name in enumerate(SUBTABS):
        is_active = (idx == state.selected_detail_tab)
        if is_active:
            tab_row.append(f" [{idx + 1}.{name}] ", style="bold #11111b on #89b4fa")
        else:
            tab_row.append(f"  {idx + 1}.{name}  ", style="dim #a6adc8")
    tab_row.append("\n" + "─" * (width - 4), style="dim")

    # Content per active subtab
    content = Text()
    prev = state.get_previous_call(record)

    if state.selected_detail_tab == 0:
        content = _view_calibration(record)
    elif state.selected_detail_tab == 1:
        content = _view_fork_diff(record, prev)
    elif state.selected_detail_tab == 2:
        content = _view_cache_perf(record)
    elif state.selected_detail_tab == 3:
        content = _view_network_delta(record, prev)
    elif state.selected_detail_tab == 4:
        content = _view_stream_tools(record)
    elif state.selected_detail_tab == 5:
        content = _view_raw_json(record)

    main_grid = Table.grid(expand=True)
    main_grid.add_column("Body")
    main_grid.add_row(tab_row)
    main_grid.add_row(content)

    call_header = f"Req #{record.seq} [{record.profile}] {record.requested_model} ({record.duration_ms}ms)"
    is_focused = (state.active_focus == "detail")
    return Panel(
        main_grid,
        title=f"[bold cyan]{call_header}[/]",
        border_style="#89b4fa" if is_focused else "#45475a",
        padding=(0, 1),
    )


def _view_calibration(record: ApiCallRecord) -> Table:
    """Subtab 0: Token breakdown calibration table."""
    segments = segment_prompt(record.payload)
    calibrated = calibrate_tokens(segments, record.prompt_tokens)

    token_header = "校准Token" if record.prompt_tokens > 0 else "预估Token"
    token_style = "bold green" if record.prompt_tokens > 0 else "bold yellow"

    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("分块名称 (Block)", style="cyan", min_width=15, ratio=3, no_wrap=True)
    table.add_column("分类", style="dim", width=6, no_wrap=True)
    table.add_column("字符数", justify="right", width=7, no_wrap=True)
    table.add_column(token_header, justify="right", style=token_style, width=9, no_wrap=True)
    table.add_column("占比", justify="right", style="yellow", width=6, no_wrap=True)
    table.add_column("分布条", min_width=8, ratio=2)

    total_tokens = record.prompt_tokens or sum(s.estimated_tokens for s in segments)

    for s in calibrated:
        bar = Bar(size=float(max(1, total_tokens)), begin=0.0, end=float(s.calibrated_tokens), width=12, color="cyan")
        table.add_row(
            s.name,
            s.category,
            f"{s.chars:,}",
            f"{s.calibrated_tokens:,}",
            f"{s.percentage:.1f}%",
            bar,
        )

    table.add_section()
    token_summary = f"{total_tokens:,}" if record.prompt_tokens > 0 else f"{total_tokens:,}*"
    table.add_row(
        Text("总上下文 (Total Prompt)", style="bold white"),
        "",
        Text(f"{sum(s.chars for s in calibrated):,}", style="bold white"),
        Text(token_summary, style=token_style),
        Text("100.0%", style="bold yellow"),
        "",
    )
    return table


def _view_fork_diff(record: ApiCallRecord, prev: ApiCallRecord | None) -> Text:
    """Subtab 1: Prompt fork divergence diff with surrounding characters."""
    fork = detect_fork(prev, record)

    text = Text()
    text.append("❖ 提示词分叉点定位 (Prompt Fork Divergence)\n", style="bold cyan")
    text.append(f"• 状态: {'⚠️ 发生分叉' if fork.has_fork else '✓ 无分叉/初始调用'}\n", style="bold yellow" if fork.has_fork else "bold green")
    text.append(f"• 诊断: {fork.reason}\n", style="white")

    if fork.has_fork:
        text.append(f"• 消息位置: messages[{fork.message_index}] ({fork.message_role}) -> {fork.block_name}\n", style="dim")
        text.append(f"• 字符偏移量: Char #{fork.char_offset:,} | 前置公共前缀: {fork.shared_chars:,} 字符 (~{fork.shared_tokens:,} tokens)\n", style="dim")
        text.append("─" * 60 + "\n", style="dim")

        # Prefix snippet
        text.append("...【KV Cache 命中公共前缀截断点】:\n", style="green bold")
        clean_pref = fork.prefix_snippet.replace("\n", "↵ ")
        text.append(f"  {clean_pref}\n\n", style="green")

        text.append(f"▼ ─────────────── [分叉切点: Char #{fork.char_offset:,}] ─────────────── ▼\n", style="bold magenta")

        # Old snippet
        clean_old = fork.old_snippet.replace("\n", "↵ ")
        text.append(f"[-] 前序请求内容 (Old):\n", style="red bold")
        text.append(f"    {clean_old[:120]}...\n\n", style="red")

        # New snippet
        clean_new = fork.new_snippet.replace("\n", "↵ ")
        text.append(f"[+] 本次触发分叉内容 (New):\n", style="cyan bold")
        text.append(f"    {clean_new[:120]}...\n\n", style="cyan")

        text.append("💡 缓存影响: 本次调用从该位置起失效，需重新 Prefill 计算后续 Token。\n", style="yellow dim")
    else:
        text.append("\n未发现分叉：本调用为初次冷启动或与上一调用完全共享前缀。\n", style="dim")

    return text


def _view_cache_perf(record: ApiCallRecord) -> Table:
    """Subtab 2: KV Cache and performance metrics."""
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("指标", style="cyan")
    table.add_column("数值", style="bold")
    table.add_column("说明", style="dim")

    c_tokens = record.cached_tokens
    p_tokens = record.prompt_tokens
    c_rate = record.cache_rate

    table.add_row("KV Cache 命中率", f"{c_rate:.1f}%", f"{c_tokens:,} / {p_tokens:,} cached tokens")
    table.add_row("首字延迟 (TTFT)", f"{record.ttft_ms} ms", "Time To First Token 从发起至首个 SSE chunk")
    table.add_row("网络总时延", f"{record.duration_ms} ms", "完成全部生成总耗时")

    # Tokens per sec
    comp_tokens = record.completion_tokens
    if record.duration_ms > 0 and comp_tokens > 0:
        tps = (comp_tokens / (record.duration_ms / 1000.0))
        table.add_row("生成速率", f"{tps:.1f} tokens/s", f"{comp_tokens} tokens / {record.duration_ms}ms")

    # Chunk stats
    table.add_row("SSE Chunks", f"{record.chunks_count} chunks", f"传输流式报文块数 ({record.total_chunk_bytes:,} 字节)")

    return table


def _view_network_delta(record: ApiCallRecord, prev: ApiCallRecord | None) -> Table:
    """Subtab 3: Basic network parameters and delta."""
    table = Table(expand=True, box=None, padding=(0, 1))
    table.add_column("参数", style="cyan")
    table.add_column("当前数值", style="bold white")
    table.add_column("增量对比 (vs 上次)", style="yellow")

    table.add_row("Upstream URL", record.upstream_url or "Local daemon", "-")
    table.add_row("API Path", record.path or "/v1/chat/completions", "-")
    table.add_row("HTTP Status", str(record.status), "-")
    table.add_row("Request Body", f"{record.body_bytes:,} 字节", f"SHA256: {record.body_sha256[:12]}...")

    # Deltas
    if prev:
        d_tokens = record.prompt_tokens - prev.prompt_tokens
        d_sign = "+" if d_tokens >= 0 else ""
        table.add_row("Prompt Tokens", f"{record.prompt_tokens:,}", f"{d_sign}{d_tokens:,} tokens")

        d_lat = record.duration_ms - prev.duration_ms
        d_lsign = "+" if d_lat >= 0 else ""
        table.add_row("Latency (ms)", f"{record.duration_ms} ms", f"{d_lsign}{d_lat} ms")
    else:
        table.add_row("Prompt Tokens", f"{record.prompt_tokens:,}", "[基准调用]")
        table.add_row("Latency (ms)", f"{record.duration_ms} ms", "[基准调用]")

    return table


def _view_stream_tools(record: ApiCallRecord) -> Text:
    """Subtab 4: Stream response text and tool calls."""
    text = Text()
    if record.response_tool_calls:
        text.append("🛠️ 工具调用 (Tool Calls):\n", style="bold yellow")
        for tc in record.response_tool_calls:
            fn = tc.get("function") or {}
            fn_name = fn.get("name") or tc.get("name") or "unknown_tool"
            fn_args = fn.get("arguments") or ""
            text.append(f"  • {fn_name}: {fn_args[:100]}\n", style="cyan")
        text.append("─" * 50 + "\n", style="dim")

    text.append("📝 回复内容 (Response Preview):\n", style="bold green")
    if record.response_text:
        text.append(record.response_text[:800], style="white")
        if len(record.response_text) > 800:
            text.append("\n...[截断，完整内容请参阅日志]", style="dim")
    else:
        text.append("[无文本流回传 / 纯工具调用或等待中]", style="dim")

    return text


def _view_raw_json(record: ApiCallRecord) -> Text:
    """Subtab 5: Pretty-printed JSON view."""
    data = {
        "request_id": record.request_id,
        "profile": record.profile,
        "timestamp": record.timestamp,
        "model": record.requested_model,
        "upstream_model": record.upstream_model,
        "usage": record.usage,
        "timings": record.timings,
        "tool_names": record.tool_names,
    }
    raw_str = json.dumps(data, ensure_ascii=False, indent=2)
    return Text(raw_str, style="dim white")
