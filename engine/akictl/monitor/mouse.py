"""Mouse interaction support for akictl monitor TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

if TYPE_CHECKING:
    from .app import MonitorApp
    from .state import MonitorState


def find_clicked_item(line: str, x: int, items: list[str], margin: int = 2) -> int | None:
    """Find the index of the item clicked in a line, within [pos - margin, pos + len + margin]."""
    for idx, item in enumerate(items):
        pos = line.find(item)
        if pos != -1:
            start = max(0, pos - margin)
            end = pos + len(item) + margin
            if start <= x <= end:
                return idx
    return None


def handle_sidebar_mouse(state: MonitorState, event: MouseEvent, lines: list[str]) -> None:
    """Handle mouse interactions on the left navigation sidebar."""
    if event.event_type == MouseEventType.SCROLL_UP:
        state.active_page = max(1, state.active_page - 1)
        state.notify_change()
        return

    if event.event_type == MouseEventType.SCROLL_DOWN:
        state.active_page = min(6, state.active_page + 1)
        state.notify_change()
        return

    if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
        y = event.position.y
        if 0 <= y < len(lines):
            line = lines[y]
            # Page navigation check
            if "1" in line or "API" in line:
                state.active_page = 1
            elif "2" in line or "智能中枢" in line or "Agent" in line:
                state.active_page = 2
            elif "3" in line or "反思" in line or "Reflection" in line:
                state.active_page = 3
            elif "4" in line or "态势" in line or "Lock" in line:
                state.active_page = 4
            elif "5" in line or "探针" in line or "MCP" in line or "工具" in line:
                state.active_page = 5
            elif "6" in line or "消耗" in line or "Analytics" in line:
                state.active_page = 6
            elif "状态:" in line or "捕获:" in line or "暂停" in line or "实时" in line:
                state.paused = not state.paused
                state.set_status("已暂停实时抓取" if state.paused else "已恢复实时抓取")

        state.active_focus = "sidebar"
        state.notify_change()


def handle_content_mouse(app: MonitorApp, event: MouseEvent, lines: list[str]) -> None:
    """Handle mouse interactions on the main content window."""
    state = app.state

    # If a modal is open, clicking anywhere on the content area closes the modal
    if state.active_modal:
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            state.active_modal = None
            state.notify_change()
        return

    x = event.position.x
    y = event.position.y
    page = state.active_page

    if page == 1:
        _handle_page1_mouse(app, event, lines, x, y)
    elif page == 2:
        _handle_page2_mouse(app, event, lines, x, y)
    else:
        # Generic scroll/focus for other pages
        if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            pass
        elif event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            state.active_focus = "detail"
            state.notify_change()


def _handle_page1_mouse(app: MonitorApp, event: MouseEvent, lines: list[str], x: int, y: int) -> None:
    """Handle mouse events for Page 1: API Calls & KV Cache."""
    state = app.state

    # First check if user clicked directly on a subtab label in any line
    if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
        if 0 <= y < len(lines):
            line = lines[y]
            subtabs = ["分块校准", "分叉 Diff", "Cache/性能", "网络增量", "响应工具流", "Raw JSON"]
            clicked_tab = find_clicked_item(line, x, subtabs)
            if clicked_tab is not None:
                state.selected_detail_tab = clicked_tab
                state.active_focus = "detail"
                state.notify_change()
                return

    # Page 1 split width is 35 columns
    is_left = (x < 35)

    if is_left:
        state.active_focus = "list"
        calls = state.filtered_calls()

        if event.event_type == MouseEventType.SCROLL_UP:
            if calls:
                curr_idx = next((i for i, c in enumerate(calls) if c.request_id == state.selected_call_id), -1)
                if curr_idx > 0:
                    state.selected_call_id = calls[curr_idx - 1].request_id
                    state.notify_change()
            return

        if event.event_type == MouseEventType.SCROLL_DOWN:
            if calls:
                curr_idx = next((i for i, c in enumerate(calls) if c.request_id == state.selected_call_id), -1)
                if curr_idx < len(calls) - 1:
                    state.selected_call_id = calls[curr_idx + 1].request_id
                    state.notify_change()
            return

        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            visible_calls = calls[-35:]
            # Row 0: outer border, Row 1: panel header, Row 2: table header, Row 3+: call rows
            if y >= 3:
                idx = y - 3
                if 0 <= idx < len(visible_calls):
                    state.selected_call_id = visible_calls[idx].request_id
            state.notify_change()
            return

    else:
        state.active_focus = "detail"

        if event.event_type == MouseEventType.SCROLL_UP:
            state.selected_detail_tab = (state.selected_detail_tab - 1) % 6
            state.notify_change()
            return

        if event.event_type == MouseEventType.SCROLL_DOWN:
            state.selected_detail_tab = (state.selected_detail_tab + 1) % 6
            state.notify_change()
            return

        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            # Check subtabs click on lines 2 or 3
            if 0 <= y < len(lines):
                line = lines[y]
                subtabs = ["分块校准", "分叉 Diff", "Cache/性能", "网络增量", "响应工具流", "Raw JSON"]
                clicked_tab = find_clicked_item(line, x, subtabs)
                if clicked_tab is not None:
                    state.selected_detail_tab = clicked_tab
            state.notify_change()


def _handle_page2_mouse(app: MonitorApp, event: MouseEvent, lines: list[str], x: int, y: int) -> None:
    """Handle mouse events for Page 2: Agent Hub & Controls."""
    state = app.state

    # First check if user clicked directly on a subtab label in any line
    if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
        if 0 <= y < len(lines):
            line = lines[y]
            agent_subtabs = ["概览控制", "MemFS Git", "群聊记录", "独处回忆", "会话上下文"]
            clicked_tab = find_clicked_item(line, x, agent_subtabs)
            if clicked_tab is not None:
                state.selected_agent_tab = clicked_tab
                state.active_focus = "detail"
                state.notify_change()
                return

    # Page 2 split width is 32 columns
    is_left = (x < 32)

    if is_left:
        state.active_focus = "list"
        profiles = list(state.agents.keys())

        if event.event_type == MouseEventType.SCROLL_UP:
            if profiles:
                curr_idx = profiles.index(state.selected_agent_profile) if state.selected_agent_profile in profiles else 0
                state.selected_agent_profile = profiles[max(0, curr_idx - 1)]
                state.notify_change()
            return

        if event.event_type == MouseEventType.SCROLL_DOWN:
            if profiles:
                curr_idx = profiles.index(state.selected_agent_profile) if state.selected_agent_profile in profiles else 0
                state.selected_agent_profile = profiles[min(len(profiles) - 1, curr_idx + 1)]
                state.notify_change()
            return

        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            # Row 0: border, Row 1: panel header, Row 2: table header, Row 3+: agent rows
            if y >= 3:
                idx = y - 3
                if 0 <= idx < len(profiles):
                    state.selected_agent_profile = profiles[idx]
            state.notify_change()
            return

    else:
        state.active_focus = "detail"

        if event.event_type == MouseEventType.SCROLL_UP:
            state.selected_agent_tab = (state.selected_agent_tab - 1) % 5
            state.notify_change()
            return

        if event.event_type == MouseEventType.SCROLL_DOWN:
            state.selected_agent_tab = (state.selected_agent_tab + 1) % 5
            state.notify_change()
            return

        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            if 0 <= y < len(lines):
                line = lines[y]
                agent_subtabs = ["概览控制", "MemFS Git", "群聊记录", "独处回忆", "会话上下文"]
                clicked_tab = find_clicked_item(line, x, agent_subtabs)
                if clicked_tab is not None:
                    state.selected_agent_tab = clicked_tab
                else:
                    # Check Subtab 0 quick action triggers
                    if state.selected_agent_tab == 0:
                        if "[M]" in line or "切换上游模型" in line:
                            app.open_model_switch()
                        elif "[R]" in line or "Reasoning" in line:
                            app.open_reasoning()
                        elif "[C]" in line or "Compact" in line:
                            app.open_compact()
                        elif "[S]" in line or "Soft Reset" in line:
                            app.open_soft_reset()
                    # Check Subtab 4 mode toggle
                    elif state.selected_agent_tab == 4:
                        if "切换" in line or "独处" in line or "群聊" in line or "[Tab]" in line:
                            state.context_view_mode = "solo" if state.context_view_mode == "group" else "group"
            state.notify_change()


def handle_status_mouse(app: MonitorApp, event: MouseEvent, lines: list[str]) -> None:
    """Handle mouse interactions on the bottom status bar."""
    state = app.state

    if state.active_modal:
        if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            state.active_modal = None
            state.notify_change()
        return

    if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
        x = event.position.x
        y = event.position.y
        if 0 <= y < len(lines):
            line = lines[y]
            actions = [
                ("[1-6]", "pages"),
                ("[Tab]", "tab"),
                ("[M]", "model"),
                ("[R]", "reasoning"),
                ("[C]", "compact"),
                ("[S]", "soft_reset"),
                ("[Q]", "quit"),
                ("换模型", "model"),
                ("Reasoning", "reasoning"),
                ("Compact", "compact"),
                ("SoftReset", "soft_reset"),
                ("退出", "quit"),
                ("切换焦点", "tab"),
            ]
            for trigger, act in actions:
                pos = line.find(trigger)
                if pos != -1 and pos - 1 <= x <= pos + len(trigger) + 1:
                    if act == "model":
                        app.open_model_switch()
                    elif act == "reasoning":
                        app.open_reasoning()
                    elif act == "compact":
                        app.open_compact()
                    elif act == "soft_reset":
                        app.open_soft_reset()
                    elif act == "tab":
                        order = ["sidebar", "list", "detail"]
                        curr = order.index(state.active_focus) if state.active_focus in order else 0
                        state.active_focus = order[(curr + 1) % len(order)]
                        state.notify_change()
                    elif act == "quit":
                        if app.app:
                            app.app.exit()
                    break


def handle_modal_mouse(app: MonitorApp, event: MouseEvent, lines: list[str]) -> None:
    """Handle mouse interactions on modal dialogs."""
    state = app.state
    if not state.active_modal:
        return

    close = state.active_modal.handle_mouse(event, lines)
    if close:
        state.active_modal = None
    state.notify_change()
