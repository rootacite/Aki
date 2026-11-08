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
