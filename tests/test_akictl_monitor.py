"""Automated unit tests for akictl monitor."""

from __future__ import annotations

import json
import pytest

from engine.akictl.monitor.analysis import (
    ForkResult,
    calibrate_tokens,
    detect_fork,
    estimate_tokens,
    segment_prompt,
)
from engine.akictl.monitor.presence_inspector import (
    load_execution_lock_status,
    load_presence_status,
    load_tool_matrix,
)
from engine.akictl.monitor.state import (
    ApiCallRecord,
    MonitorState,
)
from engine.akictl.monitor.views.dialogs import (
    CompactConfirmDialog,
    ModelSwitchDialog,
    NsfwAdultConfirmDialog,
    ReasoningDialog,
    SoftResetWarningDialog,
)


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    cjk = "你好，世界！这是一段中文测试。"
    assert estimate_tokens(cjk) > 5
    latin = "Hello world, this is an English test."
    assert estimate_tokens(latin) > 5


def test_segment_prompt_and_calibration():
    sys_content = (
        "# Identity\nYou are Aki.\n"
        "<human>\n关于李诗源的记忆\n</human>\n"
        "<channel-context>\n在线状态: 活跃\n</channel-context>\n"
        "## MemFS\nmemory/skills/git.md\n"
    )
    payload = {
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": "第一轮历史消息"},
            {"role": "assistant", "content": "好的，我知道了。"},
            {"role": "user", "content": "这是最新的用户提问"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "read_file", "description": "read a file"}}
        ],
    }

    segments = segment_prompt(payload)
    assert len(segments) >= 4

    names = [s.name for s in segments]
    assert any("Core Memory" in n for n in names)
    assert any("Channel Context" in n for n in names)
    assert any("MemFS" in n for n in names)
    assert any("Tools Schema" in n for n in names)
    assert any("Current Input" in n for n in names)

    actual_upstream_tokens = 95200
    calibrated = calibrate_tokens(segments, actual_upstream_tokens)
    assert sum(s.calibrated_tokens for s in calibrated) == actual_upstream_tokens
    for s in calibrated:
        assert s.percentage > 0.0


def test_solo_prompt_segmentation():
    sys_content = (
        "You are Aki.\n"
        "<human>\n关于Master的记忆\n</human>\n"
        "<persona>\nAki的人设\n</persona>\n"
        "<memory-policy>\n记忆策略\n</memory-policy>\n"
        "# solo-mode\n## Notice\n独处模式生效中\n"
        "# solo-memories\n过去独处的摘要记录\n"
        "# solo-transcript\n独处历史消息\n"
    )
    user_content = (
        "# live-context\n当前正在打开终端窗口\n\n"
        "# format-guard\n不要输出多余的标记\n\n"
        "小秋，今天辛苦啦！"
    )
    payload = {
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_content},
        ],
        "tools": [{"name": f"tool_{i}"} for i in range(10)],
    }
    segments = segment_prompt(payload)
    names = [s.name for s in segments]

    assert any("Core Memory" in n for n in names)
    assert any("Solo State & Memories" in n for n in names)
    assert any("Live Desktop Context" in n for n in names)
    assert any("Turn Control & Guards" in n for n in names)
    assert any("Current Input" in n for n in names)

    # Check zero tokens handling
    calibrated_zero = calibrate_tokens(segments, 0)
    assert sum(s.calibrated_tokens for s in calibrated_zero) > 0
    assert any(s.percentage > 0.0 for s in calibrated_zero)


def test_detect_fork_initial_call():
    record = ApiCallRecord(request_id="req1", profile="aki", payload={"messages": [{"role": "user", "content": "hi"}]})
    fork = detect_fork(None, record)
    assert not fork.has_fork
    assert "首个 API Call" in fork.reason


def test_detect_fork_system_prompt_change():
    prev_payload = {
        "messages": [
            {"role": "system", "content": "You are Aki. Timestamp: 2026-11-08 12:00:00. Rules: Be gentle."},
            {"role": "user", "content": "hello"},
        ]
    }
    curr_payload = {
        "messages": [
            {"role": "system", "content": "You are Aki. Timestamp: 2026-11-08 12:05:00. Rules: Be gentle."},
            {"role": "user", "content": "hello"},
        ]
    }

    prev_record = ApiCallRecord(request_id="r1", profile="aki", payload=prev_payload)
    curr_record = ApiCallRecord(request_id="r2", profile="aki", payload=curr_payload)

    fork = detect_fork(prev_record, curr_record)
    assert fork.has_fork
    assert fork.message_index == 0
    assert fork.message_role == "system"
    assert "You are Aki. Timestamp: 2026-11-08 12:0" in fork.prefix_snippet
    assert "0:00" in fork.old_snippet
    assert "5:00" in fork.new_snippet


def test_detect_fork_added_message():
    prev_payload = {"messages": [{"role": "user", "content": "hi"}]}
    curr_payload = {"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}

    prev_rec = ApiCallRecord(request_id="r1", profile="aki", payload=prev_payload)
    curr_rec = ApiCallRecord(request_id="r2", profile="aki", payload=curr_payload)

    fork = detect_fork(prev_rec, curr_rec)
    assert fork.has_fork
    assert fork.message_index == 1
    assert "新增第 [1] 条消息" in fork.reason


def test_api_call_record_metrics():
    record = ApiCallRecord(
        request_id="test1",
        profile="aki",
        usage={"prompt_tokens": 10000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 8000}},
        duration_ms=1250,
    )
    assert record.prompt_tokens == 10000
    assert record.completion_tokens == 200
    assert record.cached_tokens == 8000
    assert record.cache_rate == 80.0


def test_monitor_state_filtering_and_selection():
    state = MonitorState()
    r1 = ApiCallRecord(request_id="a1", profile="aki", usage={"prompt_tokens": 100})
    r2 = ApiCallRecord(request_id="f1", profile="fuyu", usage={"prompt_tokens": 200})
    state.add_or_update_call(r1)
    state.add_or_update_call(r2)

    assert len(state.filtered_calls()) == 2
    state.filter_profile = "fuyu"
    assert len(state.filtered_calls()) == 1
    assert state.filtered_calls()[0].request_id == "f1"

    state.filter_profile = "all"
    assert state.get_selected_call().request_id == "a1"
    assert state.get_previous_call(r2).request_id == "a1"


def test_modal_dialogs():
    # NSFW dialog
    confirmed = []
    dlg_nsfw = NsfwAdultConfirmDialog(on_confirm=lambda: confirmed.append("nsfw"))
    # Escape closes without confirming
    assert dlg_nsfw.handle_key("escape") is True
    assert confirmed == []
    # Enter confirms and closes
    assert dlg_nsfw.handle_key("enter") is True
    assert "nsfw" in confirmed

    # Compact dialog
    compact_opts = []
    dlg_compact = CompactConfirmDialog(profile="aki", on_confirm=lambda omit: compact_opts.append(omit))
    assert dlg_compact.omit_tool_results is True
    dlg_compact.handle_key("space")
    assert dlg_compact.omit_tool_results is False
    dlg_compact.handle_key("enter")
    assert compact_opts == [False]

    # Soft reset dialog
    soft_opts = []
    dlg_soft = SoftResetWarningDialog(profile="fuyu", on_confirm=lambda solo: soft_opts.append(solo))
    assert dlg_soft.solo_target is False
    dlg_soft.handle_key("tab")
    assert dlg_soft.solo_target is True
    dlg_soft.handle_key("enter")
    assert soft_opts == [True]

    # Model switch dialog
    model_opts = []
    dlg_model = ModelSwitchDialog(profile="aki", current_model="aki/gemini-3.8-flash", on_confirm=lambda m, s: model_opts.append((m, s)))
    dlg_model.handle_key("tab")
    dlg_model.handle_key("down")
    dlg_model.handle_key("enter")
    assert len(model_opts) == 1
    assert model_opts[0][1] is True  # solo_mode True

    # Reasoning dialog
    reason_opts = []
    dlg_reason = ReasoningDialog(profile="aki", current_effort="low", on_confirm=lambda r, s: reason_opts.append((r, s)))
    dlg_reason.handle_key("down")
    dlg_reason.handle_key("enter")
    assert len(reason_opts) == 1


def test_inspectors():
    presence = load_presence_status()
    assert isinstance(presence, dict)
    lock = load_execution_lock_status()
    assert isinstance(lock, dict)
    assert "lock_path" in lock
    tools = load_tool_matrix()
    assert "Haru" in tools
    assert "Natsu" in tools

    from engine.akictl.monitor.presence_inspector import load_reflection_states
    reflections = load_reflection_states()
    assert isinstance(reflections, list)
    if reflections:
        r0 = reflections[0]
        assert "agent_name" in r0
        assert "profile" in r0
        assert "conv_type" in r0
        assert r0["agent_name"] in ("Aki", "Fuyu", "Haru", "Natsu")


def test_mouse_sidebar_and_status():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from engine.akictl.monitor.app import MonitorApp
    from engine.akictl.monitor.mouse import handle_sidebar_mouse, handle_status_mouse

    app = MonitorApp()
    state = app.state

    # 1. Sidebar scroll
    state.active_page = 1
    handle_sidebar_mouse(state, MouseEvent(Point(0, 0), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()), [])
    assert state.active_page == 2
    handle_sidebar_mouse(state, MouseEvent(Point(0, 0), MouseEventType.SCROLL_UP, MouseButton.NONE, frozenset()), [])
    assert state.active_page == 1

    # 2. Sidebar click
    sidebar_lines = [
        "│ ❖ AKI MONITOR  │",
        "│ ─────────────… │",
        "│  [1]  📡 API   │",
        "│   2   👥       │",
        "│ 智能中枢       │",
        "│   3   🧠       │",
        "│ 状态: ▶ 实时   │",
    ]
    # Click page 2
    handle_sidebar_mouse(state, MouseEvent(Point(5, 3), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), sidebar_lines)
    assert state.active_page == 2
    assert state.active_focus == "sidebar"

    # Click pause toggle
    assert state.paused is False
    handle_sidebar_mouse(state, MouseEvent(Point(5, 6), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), sidebar_lines)
    assert state.paused is True

    # 3. Status bar click
    status_lines = [
        "  [1-6] 切换侧栏页面 | [Tab] 切换焦点 | [M]换模型 | [R]Reasoning | [C]Compact | [S]SoftReset | [Q]退出"
    ]
    # Click [Tab] (x around 25)
    tab_x = status_lines[0].find("[Tab]")
    handle_status_mouse(app, MouseEvent(Point(tab_x, 0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), status_lines)
    assert state.active_focus in ("list", "detail", "sidebar")

    # Click [M]换模型 -> opens ModelSwitchDialog
    model_x = status_lines[0].find("[M]")
    handle_status_mouse(app, MouseEvent(Point(model_x, 0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), status_lines)
    assert state.active_modal is not None
    assert isinstance(state.active_modal, ModelSwitchDialog)


def test_mouse_page1_and_page2():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from engine.akictl.monitor.app import MonitorApp
    from engine.akictl.monitor.mouse import handle_content_mouse

    app = MonitorApp()
    state = app.state

    # Setup 3 dummy calls
    r1 = ApiCallRecord(request_id="c1", profile="aki", seq=1)
    r2 = ApiCallRecord(request_id="c2", profile="aki", seq=2)
    state.add_or_update_call(r1)
    state.add_or_update_call(r2)
    state.selected_call_id = "c1"

    # Page 1: Left list click and scroll
    state.active_page = 1
    content_lines_page1 = [
        "top border",
        "sub header",
        "table header",
        "row 1: c1",
        "row 2: c2",
    ]
    # Click row 2 (y=4, x=10 -> left side)
    handle_content_mouse(app, MouseEvent(Point(10, 4), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), content_lines_page1)
    assert state.selected_call_id == "c2"
    assert state.active_focus == "list"

    # Scroll up on left side
    handle_content_mouse(app, MouseEvent(Point(10, 4), MouseEventType.SCROLL_UP, MouseButton.NONE, frozenset()), content_lines_page1)
    assert state.selected_call_id == "c1"

    # Right detail subtab click
    subtab_line = "│  [1.分块校准]   2.分叉 Diff    3.Cache/性能    4.网络增量    5.响应工具流    6.Raw JSON"
    content_lines_with_subtab = ["b0", "b1", subtab_line]
    cache_tab_x = subtab_line.find("Cache/性能")
    handle_content_mouse(app, MouseEvent(Point(cache_tab_x, 2), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), content_lines_with_subtab)
    assert state.selected_detail_tab == 2
    assert state.active_focus == "detail"

    # Right detail subtab scroll
    handle_content_mouse(app, MouseEvent(Point(50, 5), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset()), content_lines_with_subtab)
    assert state.selected_detail_tab == 3

    # Page 2: Agent Hub
    state.active_page = 2
    from engine.akictl.monitor.agent_inspector import AgentSnapshot
    state.agents = {
        "aki": AgentSnapshot("aki", "Aki", "group"),
        "fuyu": AgentSnapshot("fuyu", "Fuyu", "solo"),
    }
    state.selected_agent_profile = "aki"
    # Left list click: y=4 corresponds to fuyu (idx=1)
    handle_content_mouse(app, MouseEvent(Point(10, 4), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), ["b0", "b1", "b2", "aki", "fuyu"])
    assert state.selected_agent_profile == "fuyu"

    # Subtab 0 quick action click: [M] 切换上游模型
    state.selected_agent_tab = 0
    lines_p2 = [
        "b0", "b1", "subtabs",
        "...", "...", "...", "...", "...",
        "❖ 核心快捷控制指令 (按对应键即可触发):",
        "  [M] 切换上游模型 (独处/群聊)",
    ]
    m_y = 9
    handle_content_mouse(app, MouseEvent(Point(40, m_y), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), lines_p2)
    assert state.active_modal is not None
    assert isinstance(state.active_modal, ModelSwitchDialog)


def test_modal_mouse_interaction():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    # 1. Compact dialog mouse click
    compact_opts = []
    dlg_compact = CompactConfirmDialog(profile="aki", on_confirm=lambda omit: compact_opts.append(omit))
    compact_lines = [
        "确认对智能体 [AKI] 执行上下文压缩？",
        "选项: [x] 剔除工具结果 (omit-tool-results)",
        "按 [Space] 切换勾选  |  按 [Enter] 确认执行  |  按 [Esc] 取消",
    ]
    assert dlg_compact.omit_tool_results is True
    # Click checkbox row
    dlg_compact.handle_mouse(MouseEvent(Point(10, 1), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), compact_lines)
    assert dlg_compact.omit_tool_results is False
    # Click confirm row
    close = dlg_compact.handle_mouse(MouseEvent(Point(15, 2), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), compact_lines)
    assert close is True
    assert compact_opts == [False]

    # 2. Model switch dialog mouse click
    model_opts = []
    dlg_model = ModelSwitchDialog(profile="aki", current_model="aki/gemini-3.8-flash", on_confirm=lambda m, s: model_opts.append((m, s)))
    model_lines = [
        "切换智能体 [AKI] 上游模型",
        "作用目标: [*] 群聊模型 (Group) (按 [Tab] 切换群聊/独处)",
        "",
        " ❯ aki/gemini-3.8-flash",
        "   aki/gemini-3.5-flash-lite",
        "   aki/deepseek-flash",
    ]
    # Click scope toggle row
    dlg_model.handle_mouse(MouseEvent(Point(10, 1), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), model_lines)
    assert dlg_model.solo_mode is True

    # Click second model (aki/gemini-3.5-flash-lite)
    dlg_model.handle_mouse(MouseEvent(Point(10, 4), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), model_lines)
    assert dlg_model.selected_idx == 1

    # Click it again to confirm
    close = dlg_model.handle_mouse(MouseEvent(Point(10, 4), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()), model_lines)
    assert close is True
    assert model_opts == [("aki/gemini-3.5-flash-lite", True)]


def test_monitor_app_mouse_control_dispatch():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from engine.akictl.monitor.app import MonitorApp

    app = MonitorApp()
    # Trigger initial text generation to populate _*_plain_lines
    app._get_sidebar_text()
    app._get_content_text()
    app._get_status_text()

    # 1. Sidebar control receives scroll down
    app.state.active_page = 1
    res = app._sidebar_control.mouse_handler(
        MouseEvent(Point(5, 5), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset())
    )
    assert res is None
    assert app.state.active_page == 2

    # 2. Content control receives scroll on right detail
    app.state.selected_agent_tab = 0
    res = app._content_control.mouse_handler(
        MouseEvent(Point(50, 5), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset())
    )
    assert res is None
    assert app.state.selected_agent_tab == 1

    # 3. Status control receives click on [M]
    res = app._status_control.mouse_handler(
        MouseEvent(Point(40, 0), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    assert res is None
    assert app.state.active_modal is not None

    # 4. Clicking content area while modal is open closes the modal
    res = app._content_control.mouse_handler(
        MouseEvent(Point(10, 10), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    )
    assert res is None
    assert app.state.active_modal is None


