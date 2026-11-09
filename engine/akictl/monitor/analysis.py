"""Prompt segmentation, token calibration, fork divergence diffing, and metrics calculation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass
class PromptSegment:
    name: str
    category: str
    chars: int
    estimated_tokens: int
    calibrated_tokens: int = 0
    percentage: float = 0.0


@dataclass
class ForkResult:
    has_fork: bool
    reason: str
    message_index: int = -1
    message_role: str = ""
    block_name: str = ""
    char_offset: int = 0
    shared_chars: int = 0
    shared_tokens: int = 0
    prefix_snippet: str = ""
    old_snippet: str = ""
    new_snippet: str = ""


def estimate_tokens(text: str) -> int:
    """Fast multilingual heuristic tokenizer: ~1 token per 1.5 CJK chars or 3.8 Latin chars."""
    if not text:
        return 0
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other_count = len(text) - cjk_count
    return max(1, int(cjk_count / 1.5 + other_count / 3.8))


def _extract_section(text: str, tag: str, stop_at_paragraph: bool = False) -> tuple[str, str]:
    """Extract either XML <tag>...</tag> or Markdown '# tag...' block."""
    if not text:
        return "", ""
    # 1. XML pattern
    xml_pattern = re.compile(rf"<{tag}\b[^>]*>.*?</{tag}>", re.DOTALL)
    match = xml_pattern.search(text)
    if match:
        matched_str = match.group(0)
        remaining = text[:match.start()] + text[match.end():]
        return matched_str, remaining

    # 2. Markdown pattern: ^# tag\b[^\n]*\n.*?(?=^# [^\n]+|\Z)
    if stop_at_paragraph:
        md_pattern = re.compile(
            rf"(?:^|\n)# (?:{re.escape(tag)})\b[^\n]*\n.*?(?=\n# [^\n]+|\n\n|\Z)",
            re.DOTALL,
        )
    else:
        md_pattern = re.compile(
            rf"(?:^|\n)# (?:{re.escape(tag)})\b[^\n]*\n.*?(?=\n# [^\n]+|\Z)",
            re.DOTALL,
        )
    match = md_pattern.search(text)
    if match:
        matched_str = match.group(0).strip()
        remaining = text[:match.start()] + text[match.end():]
        return matched_str, remaining

    return "", text


def segment_prompt(payload: Mapping[str, Any]) -> list[PromptSegment]:
    """Breakdown request payload into logical prompt segments."""
    segments: list[PromptSegment] = []
    messages = payload.get("messages")
    if not isinstance(messages, Sequence) or not messages:
        return segments

    # 1. System Prompt Breakdown
    first_msg = messages[0] if messages else {}
    if isinstance(first_msg, Mapping) and first_msg.get("role") == "system":
        raw_sys = first_msg.get("content")
        sys_text = raw_sys if isinstance(raw_sys, str) else json.dumps(raw_sys, ensure_ascii=False)
        sys_rem = sys_text

        # Extract Core Memory tags (human, persona, memory-policy, memory)
        core_parts: list[str] = []
        for tag in ("human", "persona", "memory-policy", "memory"):
            block, sys_rem = _extract_section(sys_rem, tag)
            if block:
                core_parts.append(block)
        if core_parts:
            core_text = "\n\n".join(core_parts)
            segments.append(
                PromptSegment(
                    "Core Memory",
                    "Memory",
                    len(core_text),
                    estimate_tokens(core_text),
                )
            )

        # Extract Solo Blocks (solo-mode, solo-memories, solo-transcript)
        solo_parts: list[str] = []
        for tag in ("solo-mode", "solo-memories", "solo-transcript", "solo"):
            block, sys_rem = _extract_section(sys_rem, tag)
            if block:
                solo_parts.append(block)
        if solo_parts:
            solo_text = "\n\n".join(solo_parts)
            segments.append(
                PromptSegment(
                    "Solo State & Memories",
                    "Solo",
                    len(solo_text),
                    estimate_tokens(solo_text),
                )
            )

        # Extract Channel Transcript if present in system message
        ch_trans, sys_rem = _extract_section(sys_rem, "channel-transcript")
        if ch_trans:
            segments.append(
                PromptSegment(
                    "Channel Transcript",
                    "Channel",
                    len(ch_trans),
                    estimate_tokens(ch_trans),
                )
            )

        # Extract Channel Context & Routing
        routing_parts: list[str] = []
        for tag in (
            "channel-context",
            "channel-wake-routing",
            "channel-wait-for",
            "tool-ownership",
            "channel-identity",
            "channel-policy",
            "nighttalk",
        ):
            block, sys_rem = _extract_section(sys_rem, tag)
            if block:
                routing_parts.append(block)
        if routing_parts:
            routing_text = "\n\n".join(routing_parts)
            segments.append(
                PromptSegment(
                    "Channel Context & Routing",
                    "Routing",
                    len(routing_text),
                    estimate_tokens(routing_text),
                )
            )

        # Extract Live Context if injected in system prompt
        live_sys, sys_rem = _extract_section(sys_rem, "live-context")
        if not live_sys:
            live_sys, sys_rem = _extract_section(sys_rem, "runtime-context")
        if live_sys:
            segments.append(
                PromptSegment(
                    "Live Desktop Context",
                    "Context",
                    len(live_sys),
                    estimate_tokens(live_sys),
                )
            )

        # Extract MemFS / Deferred Memory block
        memfs_match = re.search(
            r"(?:^|\n)(?:## MemFS\b.*?(?=\n# |\Z)|<deferred-memory\b.*?</deferred-memory>)",
            sys_rem,
            re.DOTALL,
        )
        if memfs_match:
            memfs_text = memfs_match.group(0).strip()
            sys_rem = sys_rem[:memfs_match.start()] + sys_rem[memfs_match.end():]
            segments.append(
                PromptSegment(
                    "MemFS Injected Files",
                    "MemFS",
                    len(memfs_text),
                    estimate_tokens(memfs_text),
                )
            )

        # Base System Persona & Runtime Rules
        if sys_rem.strip():
            segments.append(
                PromptSegment(
                    "Base Persona & Runtime Rules",
                    "System",
                    len(sys_rem),
                    estimate_tokens(sys_rem),
                )
            )

        # Subsequent messages
        history_msgs = messages[1:-1]
        final_msg = messages[-1] if len(messages) > 1 else None
    else:
        history_msgs = messages[:-1]
        final_msg = messages[-1] if messages else None

    # 2. Tools Schema
    tools = payload.get("tools")
    if isinstance(tools, Sequence) and tools:
        tools_json = json.dumps(tools, ensure_ascii=False)
        segments.append(
            PromptSegment(
                f"Tools Schema ({len(tools)} tools)",
                "Tools",
                len(tools_json),
                estimate_tokens(tools_json),
            )
        )

    # 3. History Messages
    if history_msgs:
        total_hist_chars = 0
        user_count = 0
        asst_count = 0
        tool_count = 0
        for m in history_msgs:
            if not isinstance(m, Mapping):
                continue
            role = m.get("role", "unknown")
            if role == "user":
                user_count += 1
            elif role == "assistant":
                asst_count += 1
            elif role in ("tool", "function"):
                tool_count += 1
            content = m.get("content", "")
            c_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            total_hist_chars += len(c_text)
        label = f"History Messages ({len(history_msgs)} turns: {user_count}u/{asst_count}a/{tool_count}t)"
        segments.append(PromptSegment(label, "History", total_hist_chars, estimate_tokens(" " * total_hist_chars)))

    # 4. Current Input (with Live Context overlay extraction if present)
    if final_msg and isinstance(final_msg, Mapping):
        f_role = final_msg.get("role", "user")
        f_content = final_msg.get("content", "")
        f_text = f_content if isinstance(f_content, str) else json.dumps(f_content, ensure_ascii=False)
        f_rem = f_text

        # Extract live desktop context prepended to user message
        live_usr, f_rem = _extract_section(f_rem, "live-context", stop_at_paragraph=True)
        if not live_usr:
            live_usr, f_rem = _extract_section(f_rem, "runtime-context", stop_at_paragraph=True)
        if live_usr:
            segments.append(
                PromptSegment(
                    "Live Desktop Context",
                    "Context",
                    len(live_usr),
                    estimate_tokens(live_usr),
                )
            )

        # Extract format guards / turn controls prepended to user message
        guard_parts: list[str] = []
        for g_tag in ("format-guard", "turn-control", "channel-turn", "solo-wait-user"):
            g_blk, f_rem = _extract_section(f_rem, g_tag, stop_at_paragraph=True)
            if g_blk:
                guard_parts.append(g_blk)
        if guard_parts:
            guard_text = "\n\n".join(guard_parts)
            segments.append(
                PromptSegment(
                    "Turn Control & Guards",
                    "Guard",
                    len(guard_text),
                    estimate_tokens(guard_text),
                )
            )

        clean_user = f_rem.strip()
        if clean_user or not segments:
            segments.append(
                PromptSegment(
                    f"Current Input [{f_role}]",
                    "Input",
                    len(clean_user or f_text),
                    estimate_tokens(clean_user or f_text),
                )
            )

    return segments


def calibrate_tokens(segments: list[PromptSegment], actual_prompt_tokens: int) -> list[PromptSegment]:
    """Proportionally calibrate estimated tokens to match actual upstream prompt tokens."""
    if not segments:
        return []
    total_est = sum(s.estimated_tokens for s in segments)
    if total_est <= 0:
        return segments

    target_total = actual_prompt_tokens if actual_prompt_tokens > 0 else total_est
    ratio = target_total / total_est

    running_sum = 0
    for s in segments:
        cal = max(1, round(s.estimated_tokens * ratio))
        s.calibrated_tokens = cal
        running_sum += cal

    # Adjust rounding discrepancy on largest segment
    diff = target_total - running_sum
    if diff != 0:
        largest = max(segments, key=lambda x: x.calibrated_tokens)
        largest.calibrated_tokens += diff

    for s in segments:
        s.percentage = (s.calibrated_tokens / target_total) * 100.0 if target_total > 0 else 0.0

    return segments


def _find_divergence(text_a: str, text_b: str) -> tuple[int, str, str, str]:
    """Return common_prefix_len, prefix_snippet, text_a_snippet, text_b_snippet."""
    min_len = min(len(text_a), len(text_b))
    idx = 0
    while idx < min_len and text_a[idx] == text_b[idx]:
        idx += 1

    pref_start = max(0, idx - 100)
    pref_snippet = text_a[pref_start:idx]
    a_snippet = text_a[idx:idx + 150]
    b_snippet = text_b[idx:idx + 150]
    return idx, pref_snippet, a_snippet, b_snippet


def detect_fork(prev_record: Any | None, curr_record: Any) -> ForkResult:
    """Compare two API call records and pinpoint the exact prompt divergence point."""
    if prev_record is None or not getattr(prev_record, "payload", None):
        return ForkResult(
            has_fork=False,
            reason="首个 API Call - 无前序比对基准 (Cold Initial Turn)",
        )

    prev_payload = prev_record.payload
    curr_payload = curr_record.payload
    prev_msgs = prev_payload.get("messages", [])
    curr_msgs = curr_payload.get("messages", [])

    if not isinstance(prev_msgs, list) or not isinstance(curr_msgs, list):
        return ForkResult(has_fork=False, reason="无法解析消息格式")

    # 1. Compare messages sequence
    min_msgs = min(len(prev_msgs), len(curr_msgs))
    for i in range(min_msgs):
        m_prev = prev_msgs[i] if isinstance(prev_msgs[i], Mapping) else {}
        m_curr = curr_msgs[i] if isinstance(curr_msgs[i], Mapping) else {}

        r_prev = m_prev.get("role", "")
        r_curr = m_curr.get("role", "")
        t_prev = m_prev.get("content", "")
        t_curr = m_curr.get("content", "")

        str_prev = t_prev if isinstance(t_prev, str) else json.dumps(t_prev, ensure_ascii=False)
        str_curr = t_curr if isinstance(t_curr, str) else json.dumps(t_curr, ensure_ascii=False)

        if str_prev != str_curr:
            offset, pref_snip, old_snip, new_snip = _find_divergence(str_prev, str_curr)
            # Diagnose block in system prompt
            block = "Message Content"
            if r_curr == "system":
                if "<channel-context>" in new_snip or "message-time:" in new_snip:
                    block = "<channel-context> 动态时间/在线状态更新"
                elif "## MemFS" in new_snip:
                    block = "## MemFS 记忆文件变更"
                elif "<human>" in new_snip or "<persona>" in new_snip:
                    block = "Core Memory 反思变更"
                elif "<solo" in new_snip:
                    block = "Solo 独处上下文更新"
                else:
                    block = "System Prompt 规则配置变动"
            elif i == min_msgs - 1:
                block = "当前最新轮次用户输入"
            else:
                block = f"历史对话第 {i} 轮被压缩或替换"

            # Compute shared prefix tokens
            shared_chars = sum(
                len(prev_msgs[j].get("content", "") if isinstance(prev_msgs[j].get("content"), str) else json.dumps(prev_msgs[j].get("content", "")))
                for j in range(i)
            ) + offset
            shared_tokens = estimate_tokens(" " * shared_chars)

            reason = f"在消息 [{i}] ({r_curr}) 的 {block} 处发生分叉"
            return ForkResult(
                has_fork=True,
                reason=reason,
                message_index=i,
                message_role=r_curr,
                block_name=block,
                char_offset=offset,
                shared_chars=shared_chars,
                shared_tokens=shared_tokens,
                prefix_snippet=pref_snip,
                old_snippet=old_snip,
                new_snippet=new_snip,
            )

    # If all existing messages matched but count differs
    if len(curr_msgs) > len(prev_msgs):
        new_m = curr_msgs[min_msgs] if isinstance(curr_msgs[min_msgs], Mapping) else {}
        r_new = new_m.get("role", "unknown")
        cnt_new = new_m.get("content", "")
        snip = cnt_new[:150] if isinstance(cnt_new, str) else json.dumps(cnt_new, ensure_ascii=False)[:150]
        shared_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in prev_msgs)
        return ForkResult(
            has_fork=True,
            reason=f"前序 {min_msgs} 条消息 100% 保持一致；末尾新增第 [{min_msgs}] 条消息 ({r_new})",
            message_index=min_msgs,
            message_role=r_new,
            block_name="末尾新增消息",
            char_offset=0,
            shared_chars=shared_chars,
            shared_tokens=estimate_tokens(" " * shared_chars),
            prefix_snippet="...[前序历史完全命中缓存]...",
            old_snippet="[无 - 原请求已结束]",
            new_snippet=snip,
        )

    return ForkResult(has_fork=False, reason="两次调用的提示词完全相同 (100% Identical Prompt)")
