"""Main prompt_toolkit application for akictl monitor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.containers import (
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.formatted_text import ANSI

from engine.akictl.chat import _connect
from engine.akictl.support import _rpc_call
from engine.client import default_socket_path

from .analysis import detect_fork
from .socket_listener import SocketListener
from .state import MonitorState
from .theme import PT_STYLE, render_rich_to_ansi
from .views.dialogs import (
    CompactConfirmDialog,
    ModelSwitchDialog,
    NsfwAdultConfirmDialog,
    ReasoningDialog,
    SoftResetWarningDialog,
)
from .views.page_agent_hub import render_agent_hub_page
from .views.page_analytics import render_analytics_page
from .views.page_api_calls import render_api_calls_page
from .views.page_presence_lock import render_presence_lock_page
from .views.page_reflection import render_reflection_page
from .views.page_tools_mcp import render_tools_mcp_page
from .views.sidebar import render_sidebar


class MonitorApp:
    """TUI Application for akictl monitor."""

    def __init__(self, initial_profile: str = "all") -> None:
        self.state = MonitorState()
        self.state.filter_profile = initial_profile
        self.listener = SocketListener(self.state)
        self.app: Application[None] | None = None

        # Build prompt_toolkit UI
        self._sidebar_control = FormattedTextControl(self._get_sidebar_text)
        self._content_control = FormattedTextControl(self._get_content_text)
        self._status_control = FormattedTextControl(self._get_status_text)
        self._modal_control = FormattedTextControl(self._get_modal_text)

        self.key_bindings = self._create_key_bindings()

    def _get_sidebar_text(self) -> ANSI:
        panel = render_sidebar(self.state)
        return render_rich_to_ansi(panel, width=18)

    def _get_content_text(self) -> ANSI:
        page = self.state.active_page
        width = 110
        if self.app and self.app.output:
            width = max(80, self.app.output.get_size().columns - 22)

        if page == 1:
            panel = render_api_calls_page(self.state, width=width)
        elif page == 2:
            panel = render_agent_hub_page(self.state, width=width)
        elif page == 3:
            panel = render_reflection_page(self.state, width=width)
        elif page == 4:
            panel = render_presence_lock_page(self.state, width=width)
        elif page == 5:
            panel = render_tools_mcp_page(self.state, width=width)
        elif page == 6:
            panel = render_analytics_page(self.state, width=width)
        else:
            panel = render_api_calls_page(self.state, width=width)

        return render_rich_to_ansi(panel, width=width)

    def _get_status_text(self) -> ANSI:
        status_msg = self.state.get_status()
        if status_msg:
            msg = f"  💡 {status_msg}"
        else:
            msg = "  [1-6] 切换侧栏页面 | [Tab] 切换焦点 | [M]换模型 | [R]Reasoning | [C]Compact | [S]SoftReset | [Q]退出"
        from rich.text import Text
        text = Text(msg, style="dim #a6adc8")
        return render_rich_to_ansi(text, width=120)

    def _get_modal_text(self) -> ANSI:
        if not self.state.active_modal:
            return ANSI("")
        rendered = self.state.active_modal.render()
        return render_rich_to_ansi(rendered, width=72)

    def _create_layout(self) -> Layout:
        sidebar_window = Window(
            content=self._sidebar_control,
            width=20,
            style="class:sidebar",
        )
        content_window = Window(
            content=self._content_control,
            style="class:frame",
        )
        status_window = Window(
            content=self._status_control,
            height=1,
            style="class:status",
        )

        body = HSplit([
            VSplit([sidebar_window, content_window]),
            status_window,
        ])

        modal_float = Float(
            content=Window(content=self._modal_control),
            transparent=True,
        )

        float_container = FloatContainer(
            content=body,
            floats=[modal_float],
        )

        return Layout(float_container)

    def _create_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("q")
        def _exit(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal = None
                self.state.notify_change()
                return
            event.app.exit()

        @kb.add("escape")
        def _escape(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal = None
                self.state.notify_change()

        # Modal interceptor for all other keys
        @kb.add("<any>")
        def _any_key(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                key_name = event.data
                close = self.state.active_modal.handle_key(key_name)
                if close:
                    self.state.active_modal = None
                self.state.notify_change()
                return

        # Navigation 1-6
        for page_num in range(1, 7):
            @kb.add(str(page_num))
            def _switch_page(event: KeyPressEvent, p=page_num) -> None:
                if self.state.active_modal:
                    return
                self.state.active_page = p
                self.state.notify_change()

        # Focus switch Tab / Shift+Tab
        @kb.add("tab")
        def _tab(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal.handle_key("tab")
                self.state.notify_change()
                return
            if self.state.active_page == 2 and self.state.selected_agent_tab == 4:
                # Toggle group vs solo context in Subtab 4
                self.state.context_view_mode = "solo" if self.state.context_view_mode == "group" else "group"
                self.state.notify_change()
                return

            focus_order = ["sidebar", "list", "detail"]
            try:
                curr_idx = focus_order.index(self.state.active_focus)
            except ValueError:
                curr_idx = 0
            self.state.active_focus = focus_order[(curr_idx + 1) % len(focus_order)]
            self.state.notify_change()

        @kb.add("s-tab")
        def _s_tab(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            focus_order = ["sidebar", "list", "detail"]
            try:
                curr_idx = focus_order.index(self.state.active_focus)
            except ValueError:
                curr_idx = 0
            self.state.active_focus = focus_order[(curr_idx - 1) % len(focus_order)]
            self.state.notify_change()

        # Subtabs [ and ]
        @kb.add("[")
        def _prev_subtab(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            if self.state.active_page == 1:
                self.state.selected_detail_tab = (self.state.selected_detail_tab - 1) % 6
            elif self.state.active_page == 2:
                self.state.selected_agent_tab = (self.state.selected_agent_tab - 1) % 5
            self.state.notify_change()

        @kb.add("]")
        def _next_subtab(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            if self.state.active_page == 1:
                self.state.selected_detail_tab = (self.state.selected_detail_tab + 1) % 6
            elif self.state.active_page == 2:
                self.state.selected_agent_tab = (self.state.selected_agent_tab + 1) % 5
            self.state.notify_change()

        # Up and Down navigation
        @kb.add("up")
        @kb.add("k")
        def _up(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal.handle_key("up")
                self.state.notify_change()
                return

            if self.state.active_focus == "sidebar":
                self.state.active_page = max(1, self.state.active_page - 1)
            elif self.state.active_focus == "list":
                if self.state.active_page == 1:
                    calls = self.state.filtered_calls()
                    if calls:
                        curr_idx = next((i for i, c in enumerate(calls) if c.request_id == self.state.selected_call_id), -1)
                        if curr_idx > 0:
                            self.state.selected_call_id = calls[curr_idx - 1].request_id
                elif self.state.active_page == 2:
                    p_keys = list(self.state.agents.keys())
                    if p_keys:
                        curr_idx = p_keys.index(self.state.selected_agent_profile) if self.state.selected_agent_profile in p_keys else 0
                        self.state.selected_agent_profile = p_keys[max(0, curr_idx - 1)]
            self.state.notify_change()

        @kb.add("down")
        @kb.add("j")
        def _down(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal.handle_key("down")
                self.state.notify_change()
                return

            if self.state.active_focus == "sidebar":
                self.state.active_page = min(6, self.state.active_page + 1)
            elif self.state.active_focus == "list":
                if self.state.active_page == 1:
                    calls = self.state.filtered_calls()
                    if calls:
                        curr_idx = next((i for i, c in enumerate(calls) if c.request_id == self.state.selected_call_id), -1)
                        if curr_idx < len(calls) - 1:
                            self.state.selected_call_id = calls[curr_idx + 1].request_id
                elif self.state.active_page == 2:
                    p_keys = list(self.state.agents.keys())
                    if p_keys:
                        curr_idx = p_keys.index(self.state.selected_agent_profile) if self.state.selected_agent_profile in p_keys else 0
                        self.state.selected_agent_profile = p_keys[min(len(p_keys) - 1, curr_idx + 1)]
            self.state.notify_change()

        # Space: pause stream or modal
        @kb.add("space")
        def _space(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                self.state.active_modal.handle_key("space")
                self.state.notify_change()
                return
            if self.state.active_page == 1:
                self.state.paused = not self.state.paused
                self.state.set_status("已暂停实时抓取" if self.state.paused else "已恢复实时抓取")
                self.state.notify_change()

        # Action: Model Switch (M)
        @kb.add("m")
        @kb.add("M")
        def _modal_model(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            profile = self.state.selected_agent_profile
            current_model = self.state.agents.get(profile, {}).group_model if profile in self.state.agents else ""
            self.state.active_modal = ModelSwitchDialog(
                profile=profile,
                current_model=current_model,
                on_confirm=lambda m, solo: self._do_switch_model(profile, m, solo),
            )
            self.state.notify_change()

        # Action: Reasoning (R)
        @kb.add("r")
        @kb.add("R")
        def _modal_reasoning(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            profile = self.state.selected_agent_profile
            current_effort = self.state.agents.get(profile, {}).reasoning_effort if profile in self.state.agents else "low"
            self.state.active_modal = ReasoningDialog(
                profile=profile,
                current_effort=current_effort,
                on_confirm=lambda eff, solo: self._do_set_reasoning(profile, eff, solo),
            )
            self.state.notify_change()

        # Action: Compact (C)
        @kb.add("c")
        @kb.add("C")
        def _modal_compact(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            profile = self.state.selected_agent_profile
            self.state.active_modal = CompactConfirmDialog(
                profile=profile,
                on_confirm=lambda omit: self._do_compact(profile, omit),
            )
            self.state.notify_change()

        # Action: Soft Reset (S)
        @kb.add("s")
        @kb.add("S")
        def _modal_soft_reset(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            profile = self.state.selected_agent_profile
            self.state.active_modal = SoftResetWarningDialog(
                profile=profile,
                on_confirm=lambda solo: self._do_soft_reset(profile, solo),
            )
            self.state.notify_change()

        # Action: NSFW Toggle (N)
        @kb.add("n")
        @kb.add("N")
        def _modal_nsfw(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                return
            if not self.state.nsfw_unlocked:
                self.state.active_modal = NsfwAdultConfirmDialog(
                    on_confirm=self._unlock_nsfw,
                )
            else:
                self.state.nsfw_unlocked = False
                self.state.set_status("已重新隐藏 NSFW 独处回忆")
            self.state.notify_change()

        # Enter key for modals
        @kb.add("enter")
        def _enter(event: KeyPressEvent) -> None:
            if self.state.active_modal:
                close = self.state.active_modal.handle_key("enter")
                if close:
                    self.state.active_modal = None
                self.state.notify_change()

        return kb

    def _unlock_nsfw(self) -> None:
        self.state.nsfw_unlocked = True
        self.state.set_status("已解锁 NSFW 私密回忆展示 (18+)")

    def _do_switch_model(self, profile: str, model: str, solo: bool) -> None:
        asyncio.create_task(self._async_rpc_call(
            profile,
            "aki.model.set",
            {"model": model, "solo": solo} if solo else {"model": model},
            success_msg=f"成功将 [{profile}] {'独处' if solo else '群聊'}模型切换为 {model}",
        ))

    def _do_set_reasoning(self, profile: str, effort: str, solo: bool) -> None:
        asyncio.create_task(self._async_rpc_call(
            profile,
            "aki.reasoning.set",
            {"effort": effort, "solo": solo} if solo else {"effort": effort},
            success_msg=f"成功将 [{profile}] {'独处' if solo else '群聊'} Reasoning 设为 {effort}",
        ))

    def _do_compact(self, profile: str, omit_tool_results: bool) -> None:
        self.state.set_status(f"正在压缩 [{profile}] 上下文并触发反思，请稍候...", duration=10.0)
        asyncio.create_task(self._async_rpc_call(
            profile,
            "aki.conversation.compact",
            {"timeout": 900, "omit_tool_results": omit_tool_results},
            success_msg=f"[{profile}] 上下文压缩完成！",
        ))

    def _do_soft_reset(self, profile: str, solo: bool) -> None:
        asyncio.create_task(self._async_rpc_call(
            profile,
            "aki.conversation.soft_reset",
            {"solo": solo},
            success_msg=f"[{profile}] {'独处' if solo else '群聊'} Soft Reset 完成 (快照已备份)！",
        ))

    async def _async_rpc_call(self, profile: str, method: str, params: dict[str, Any], success_msg: str) -> None:
        sock_path = default_socket_path(profile)
        try:
            client = _connect(sock_path, json_mode=False)
            try:
                await asyncio.to_thread(_rpc_call, client, method, params, timeout=30.0)
                self.state.set_status(success_msg, duration=4.0)
            finally:
                client.close()
        except Exception as exc:
            self.state.set_status(f"执行失败: {exc}", duration=5.0)

    async def run_async(self) -> None:
        """Run the monitor app within the asyncio loop."""
        layout = self._create_layout()
        self.app = Application(
            layout=layout,
            key_bindings=self.key_bindings,
            style=PT_STYLE,
            full_screen=True,
            mouse_support=True,
        )

        # Connect UI redraw listener
        self.state.register_change_listener(lambda: self.app.invalidate() if self.app else None)

        # Start socket discovery and streaming
        self.listener.start()
        poll_task = asyncio.create_task(self._poll_agent_states())

        try:
            await self.app.run_async()
        finally:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            await self.listener.stop()

    async def _poll_agent_states(self) -> None:
        """Periodically refresh agent live status (solo/group/offline) and models."""
        from .agent_inspector import load_agent_snapshots
        while True:
            try:
                snapshots = await asyncio.to_thread(load_agent_snapshots)
                changed = False
                for p, snap in snapshots.items():
                    curr = self.state.agents.get(p)
                    if (
                        curr is None
                        or curr.status != snap.status
                        or curr.group_model != snap.group_model
                        or curr.reasoning_effort != snap.reasoning_effort
                    ):
                        changed = True
                        break
                if changed or not self.state.agents:
                    self.state.agents = snapshots
                    self.state.notify_change()
            except Exception:
                pass
            await asyncio.sleep(2.5)
