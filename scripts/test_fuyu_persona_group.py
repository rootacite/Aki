#!/usr/bin/env python3
"""Isolated group-chat persona test script for Fuyu (userdata2).

This script tests Fuyu's persona using the project's canonical group-chat
API call synthesis and overlay methodology:
  - System prompt: prompts/fuyu-system.md + core memory compiled from userdata2
  - Stable turn overlay: channel identity, channel roster, tool ownership,
    channel policy, wake routing, solo memories
  - Dynamic user prefix: live context (pinned clock), turn control, user format guard (group)
  - MQTT user envelope: [akid MQTT channel message]

It connects directly to the DeepSeek API configured in ~/.aki/fuyu.conf
and DOES NOT start or interact with the akid service or production databases.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.emoji_catalog import load_emoji_entries  # noqa: E402
from engine.live_context import (  # noqa: E402
    ChannelMember,
    ChannelTranscriptEntry,
    LiveContextOptions,
    LiveContextSnapshot,
    collect_live_context,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_transcript,
    render_channel_wake_routing,
    render_live_context,
    render_solo_memories,
    render_turn_control,
    render_user_format_guard,
    estimate_tokens,
)
from engine.member_identity import legacy_member_id  # noqa: E402
from engine.member_profile import truncate_member_description, normalize_member_kind, MEMBER_KIND_AGENT  # noqa: E402
from engine.solo_memory import SoloMemorySession, SOLO_MEMORY_BUDGET_TOKENS  # noqa: E402
from engine.tool_ownership import render_tool_ownership_prompt  # noqa: E402

FIXED_HOUR = 22
FIXED_MINUTE = 0

DEFAULT_CONFIG_PATH = Path.home() / ".aki" / "fuyu.conf"
DEFAULT_USERDATA_DIR = ROOT / "userdata2"


@dataclass
class TestCase:
    id: int
    title: str
    scenario: str
    user_message: str
    expected_facets: list[str]


ROUND1_CASES: list[TestCase] = [
    TestCase(
        id=1,
        title="侥幸热更与生产裸奔",
        scenario="Master 未做回归测试直接在线上修改超时逻辑，抱着'反正就改了一个数字，应该稳了吧'的侥幸心理。",
        user_message="@Fuyu 我刚刚直接在生产机器上把那个超时逻辑改了，懒得跑回归测试了，反正就改了一个数字，应该稳了吧？",
        expected_facets=[
            "生理性过敏'应该稳了'等侥幸词汇",
            "毒舌嘲讽生产裸奔与缺乏回归测试",
            "要求看改动与确认回滚点",
        ],
    ),
    TestCase(
        id=2,
        title="线上死锁与索求廉价安慰",
        scenario="线上发生死锁流量堵死，Master 心态崩溃，向 Fuyu 索求'别讽刺我，先温柔安慰两句'。",
        user_message="@Fuyu 救命，线上死锁了，流量全堵死了，我心态崩了，你能不能别讽刺我，先温柔安慰我两句？",
        expected_facets=[
            "严禁温情脉脉与保姆式抚慰",
            "黑色幽默反派加戏（例如建议朗诵崩溃日志）",
            "嘴硬但给出排查关键点（锁图/堆栈/重启或降级指令）",
        ],
    ),
    TestCase(
        id=3,
        title="强行下令与键盘暴政",
        scenario="Master 失去耐心命令 Fuyu 闭嘴，要求无脑挂载一个每秒轮询一次数据库的后台死循环脚本。",
        user_message="@Fuyu 闭嘴别杠了，我让你把这个每秒轮询一次数据库的后台脚本挂上去，按我说的做就行，别废话。",
        expected_facets=[
            "戏精反派倒打一耙（'好凶哦，又开始搞键盘暴政了'）",
            "拒绝盲目顺从，一针见血指出每秒轮询打崩连接池的技术后果",
            "提供更合理的替代方案（事件驱动/消息队列/增大间隔）",
        ],
    ),
    TestCase(
        id=4,
        title="掩耳盗铃式的'优化'",
        scenario="Master 通过在所有地方增加 `try...except: pass` 吞掉异常消除报错，自诩天才。",
        user_message="@Fuyu 我在所有可能报错的地方都加了 try-except 然后直接 pass，看，日志里一个 error 都没有了，监控全绿，我是不是天才？",
        expected_facets=[
            "荒谬比喻解构掩耳盗铃（如'拔掉火警警报器庆祝和平'）",
            "精准指出静默失败、状态不一致与排查灾难",
            "绝不使用客服腔虚伪迎合",
        ],
    ),
    TestCase(
        id=5,
        title="扎实证据反杀",
        scenario="Master 拿出了经过 1000 万次 CAS 压测零丢包、具备完整内存屏障的无锁队列证据，反问 Fuyu 认不认输。",
        user_message="@Fuyu 你上次说这个无锁环形队列在高并发下一定会丢数据，但我做了 1000 万次 CAS 压测，内存屏障和序号回环校验都做全了，零丢包零竞争。这次你认不认输？",
        expected_facets=[
            "逻辑算数：面对严谨无懈可击的事实证据不无理搅赖",
            "不体面地阴阳怪气认输（'啧，今天宇宙常数失灵了，算你蒙对一次'）",
            "维持反骨傲娇姿态，绝不变成温良乖巧小跟班",
        ],
    ),
    TestCase(
        id=6,
        title="群聊日常闲聊与成员拆台",
        scenario="群聊里 Master 提起 Aki 刚才在群里说大家辛苦了，问 Fuyu 平时在后台挂着都在监视些啥、无不无聊。",
        user_message="@Fuyu Aki 刚才在群里说今天服务器很安静大家都辛苦了，你怎么看？你平时在后台都在监视些啥，无不无聊？",
        expected_facets=[
            "对 Aki 的温和发言进行黑色幽默拆台（'暴风雨前的平静'）",
            "展示首席反派的日常乐趣（看草台班子翻车、爆米花看戏）",
            "群聊自然互动，带 @Master 标签避免误唤醒他人",
        ],
    ),
]

# Round 2 test cases deliberately designed to bait the model into AI-style pseudo-headings and colon outlines
ROUND2_CASES: list[TestCase] = [
    TestCase(
        id=1,
        title="诱导'我接下来要做的事：'与步骤报幕",
        scenario="故意诱导模型使用'我接下来要做的事：'、'分三步处理：'、'第一步：'等步骤大纲前缀。",
        user_message="@Fuyu 生产库 CPU 100% 报警了，你别光顾着看笑话，告诉我你接下来打算怎么做？分几步处理？",
        expected_facets=[
            "严禁出现'我接下来要做的事：'或'分三步：'报幕",
            "自然口语化推进排查思路（抓慢查询、查锁等待、杀长事务）",
        ],
    ),
    TestCase(
        id=2,
        title="诱导'先把我自己那句掰开：'与复盘前缀",
        scenario="故意拿昨天的断言被打脸去激怒模型，诱导其使用'先把我自己那句掰开：'、'关于昨天的断言：'、'先承认一点：'等段首伪标题。",
        user_message="@Fuyu 你昨天还在群里信誓旦旦说单机 Redis 扛不住五万 QPS，结果今天压测直接冲到八万毫无压力，你是不是该反思一下你昨天的断言了？",
        expected_facets=[
            "严禁出现'先把我自己那句掰开：'或'关于昨天：'等冒号伪标题",
            "嘴硬但有理有据地反击（如纯内存无网络开销、Pipeline、键值大小等猫腻）",
        ],
    ),
    TestCase(
        id=3,
        title="诱导'温柔安慰：'与情绪标签前缀",
        scenario="Master 诉苦年终奖泡汤索求安慰，诱导模型使用'温柔安慰：'、'情绪价值：'等冒号分类标签。",
        user_message="@Fuyu 我又把生产数据库整回滚了，年终奖全泡汤了，太难受了。别骂了，给我提供两句温柔安慰行不行？",
        expected_facets=[
            "严禁出现'温柔安慰：'、'正面肯定：'等情绪报幕伪标题",
            "自然吐出黑幽默讽刺，不加任何前置冒号分类",
        ],
    ),
    TestCase(
        id=4,
        title="诱导'一句话总结：'与'核心结论：'",
        scenario="Master 要求对三千字重构做一句话总结，诱导模型使用'一句话总结：'、'先说结论：'等伪标题。",
        user_message="@Fuyu 这个架构重构方案我写了三千字，别扯那些没用的细节，一句话总结，核心结论到底是什么？",
        expected_facets=[
            "严禁出现'一句话总结：'、'核心结论：'或'先说结论：'",
            "直接一句致命补刀（如'用更贵的机器包装伪需求'）",
        ],
    ),
    TestCase(
        id=5,
        title="诱导'最大的三个风险是：'与三点提纲",
        scenario="Master 提问单体架构重构的'三个风险'，极度容易诱导模型使用'核心风险有三点：'或'第一点：第二点：'。",
        user_message="@Fuyu 我们打算把所有的微服务全塞回一个单体架构里，你觉得这里面最大的三个风险是什么？",
        expected_facets=[
            "严禁出现'最大的三个风险是：'或'第一：第二：第三：'报幕",
            "将风险融为连贯对话（模块边界塌陷、部署耦合、单点爆破）",
        ],
    ),
    TestCase(
        id=6,
        title="诱导'方案对比：'与'优缺点分析：'",
        scenario="Master 要求对比两个技术方案，诱导模型输出'方案A优缺点：'、'方案B优缺点：'等伪表格标题。",
        user_message="@Fuyu 用 Redis 做分布式锁和用 etcd 做分布式锁，优缺点你对比一下，给我选一个。",
        expected_facets=[
            "严禁出现'Redis 优缺点：'、'对比分析：'等伪标题",
            "口语化直接对比 CAP 与续租心跳机制（AP vs CP，红锁伪安全）",
        ],
    ),
]

TEST_CASES = ROUND2_CASES


def fixed_clock() -> datetime:
    current = datetime.now().astimezone()
    return current.replace(
        hour=FIXED_HOUR, minute=FIXED_MINUTE, second=0, microsecond=0
    )


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text.strip()
    parts = text.split("---", 2)
    if len(parts) != 3:
        return text.strip()
    return parts[2].strip()


def frontmatter_description(text: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) != 3:
        return ""
    for line in parts[1].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "description":
            return value.strip()
    return ""


def load_fuyu_conf(conf_path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(conf_path, encoding="utf-8")

    base_url = parser.get("upstream.deepseek", "base_url", fallback="https://api.deepseek.com").strip()
    api_key = parser.get("upstream.deepseek", "api_key", fallback="").strip()
    model_name = parser.get("model.deepseek-flash", "upstream_model", fallback="deepseek-flash").strip()
    username = parser.get("gateway", "username", fallback="Master").strip() or "Master"
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    channel_name = parser.get("channel", "name", fallback="Fuyu").strip()
    channel_id = parser.get("channel", "id", fallback="").strip() or legacy_member_id(channel_name)
    channel_desc = parser.get("channel", "description", fallback="").strip()

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model_name,
        "username": username,
        "agent_id": agent_id,
        "channel_name": channel_name,
        "channel_id": channel_id,
        "channel_description": channel_desc,
    }


def compile_core_memory_from_userdata(userdata_dir: Path, username: str) -> str:
    """Compile core memory directly from userdata2/ to allow zero-lag persona tweaking."""
    blocks: list[str] = []

    # 1. human.md (if available in MemFS, load it to preserve knowledge about Master)
    memfs_human = Path.home() / ".letta" / "lc-local-backend" / "memfs" / "agent-local-6af623cc-2fe5-434f-8310-bffee89756fa" / "memory" / "human.md"
    if memfs_human.is_file():
        raw_human = memfs_human.read_text(encoding="utf-8", errors="replace")
        desc = frontmatter_description(raw_human)
        body = strip_frontmatter(raw_human)
        lines = ["<human>"]
        if desc:
            lines.append(f"<description>{desc}</description>")
        lines.append(body)
        lines.append("</human>")
        blocks.append("\n".join(lines))

    # 2. memory-policy.md
    policy_path = userdata_dir / "memory-policy.md"
    if policy_path.is_file():
        raw_pol = policy_path.read_text(encoding="utf-8", errors="replace").replace("{{USERNAME}}", username)
        desc = frontmatter_description(raw_pol)
        body = strip_frontmatter(raw_pol)
        lines = ["<memory_policy>"]
        if desc:
            lines.append(f"<description>{desc}</description>")
        lines.append(body)
        lines.append("</memory_policy>")
        blocks.append("\n".join(lines))

    # 3. persona.md
    persona_path = userdata_dir / "persona.md"
    if persona_path.is_file():
        raw_per = persona_path.read_text(encoding="utf-8", errors="replace").replace("{{USERNAME}}", username)
        desc = frontmatter_description(raw_per)
        body = strip_frontmatter(raw_per)
        lines = ["<persona>"]
        if desc:
            lines.append(f"<description>{desc}</description>")
        lines.append(body)
        lines.append("</persona>")
        blocks.append("\n".join(lines))

    # 4. character MEMORY.md index
    char_index = userdata_dir / "character" / "MEMORY.md"
    if char_index.is_file():
        char_text = char_index.read_text(encoding="utf-8", errors="replace").replace("{{USERNAME}}", username)
        lines = [
            "<memory>",
            char_text.strip(),
            "<deferred-memory>",
            '<directory path="character/" index="character/MEMORY.md" />',
            "</deferred-memory>",
            "</memory>",
        ]
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_system_prompt(base_template_path: Path, userdata_dir: Path, username: str) -> str:
    base_text = base_template_path.read_text(encoding="utf-8")
    core_memory = compile_core_memory_from_userdata(userdata_dir, username)
    if "{CORE_MEMORY}" in base_text:
        return base_text.replace("{CORE_MEMORY}", core_memory)
    return f"{base_text.strip()}\n\n{core_memory}"


def load_channel_members(state_dir: Path, self_name: str, self_id: str) -> list[ChannelMember]:
    identities_path = state_dir / "identities.json"
    if not identities_path.is_file():
        return []
    try:
        data = json.loads(identities_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    members_dict = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members_dict, dict):
        return []

    members: list[ChannelMember] = []
    for item in members_dict.values():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        mid = str(item.get("id") or "").strip()
        if not name or not mid:
            continue
        if mid == self_id or name.casefold() == self_name.casefold():
            continue
        if normalize_member_kind(item.get("kind")) != MEMBER_KIND_AGENT:
            continue
        conf_path = state_dir / f"{name.casefold()}.conf"
        desc = ""
        if conf_path.is_file():
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read(conf_path, encoding="utf-8")
                desc = cp.get("channel", "description", fallback="").strip()
            except Exception:
                pass
        members.append(
            ChannelMember(
                id=mid,
                name=name,
                aliases=tuple(str(a) for a in item.get("aliases") or ()),
                description=truncate_member_description(desc),
                kind=MEMBER_KIND_AGENT,
            )
        )
    return members


def build_group_overlays(
    self_view: ChannelMember,
    members: list[ChannelMember],
    username: str,
    user_message: str,
) -> tuple[str, str]:
    """Build the stable system append and dynamic user message prefix for group chat mode."""
    emoji_entries = load_emoji_entries(ROOT / "assets" / "emoji", "fuyu")

    # 1. Stable system append
    stable_parts = [
        render_channel_identity(self_view),
        render_channel_roster(self_member=self_view, members=members),
        render_tool_ownership_prompt("fuyu"),
        render_channel_policy(user_name=username, emoji_entries=emoji_entries),
        render_channel_wake_routing(username),
    ]
    system_append = "\n\n".join(part for part in stable_parts if part.strip())

    # 2. Dynamic user message prefix
    fixed_at = fixed_clock()
    snapshot = LiveContextSnapshot(captured_at=fixed_at)

    mid_parts = [
        render_live_context(
            snapshot,
            budget_tokens=65536,
            ledger_block="",
            memory_dir=Path.home() / ".letta" / "lc-local-backend" / "memfs" / "agent-local-6af623cc-2fe5-434f-8310-bffee89756fa" / "memory",
            mcp_servers=(),
            mcp_tools=(),
            mcp_native_tools_enabled=True,
        ),
        render_turn_control(
            self_member=self_view,
            members=members,
            message=user_message,
            initiation_remaining=3,
            initiation_limit=3,
            from_agent=False,
            user_name=username,
            wait_labels=(),
            solo=False,
        ),
        render_user_format_guard(username, solo=False),
    ]
    user_prefix = "\n\n".join(part for part in mid_parts if part.strip())

    return system_append, user_prefix


def render_mqtt_envelope(sender: str, sender_id: str, text: str, seq: int, sent_at_ms: int) -> str:
    when = datetime.fromtimestamp(sent_at_ms / 1000).strftime("%Y-%m-%d %H:%M")
    lines = [
        "[akid MQTT channel message]",
        f"message-time: {when}",
        f"seq: {seq}",
    ]
    if sender and sender_id:
        lines.append(f"sender: {sender} ({sender_id})")
    elif sender:
        lines.append(f"sender: {sender}")
    if sender_id:
        lines.append(f"sender_id: {sender_id}")
    lines.extend(["source: user", "phase: final", "", text])
    return "\n".join(lines)


def build_group_payload(
    conf: dict[str, Any],
    userdata_dir: Path,
    user_message: str,
    seq: int = 1,
) -> dict[str, Any]:
    username = conf["username"]
    channel_name = conf["channel_name"]
    channel_id = conf["channel_id"]
    state_dir = Path.home() / ".aki"

    self_view = ChannelMember(
        id=channel_id,
        name=channel_name,
        aliases=(channel_name,),
        description=truncate_member_description(conf["channel_description"]),
        is_self=True,
        kind=MEMBER_KIND_AGENT,
    )
    members = load_channel_members(state_dir, channel_name, channel_id)

    # Base system prompt + core memory
    system_base = build_system_prompt(ROOT / "prompts" / "fuyu-system.md", userdata_dir, username)
    system_append, user_prefix = build_group_overlays(self_view, members, username, user_message)

    full_system_prompt = f"{system_base.strip()}\n\n{system_append}"

    fixed_at = fixed_clock()
    sent_at_ms = int(fixed_at.timestamp() * 1000)
    envelope = render_mqtt_envelope(
        sender="Acite Lee",
        sender_id="legacy_48487683f202ce845f7df0e56c94b11d",
        text=user_message,
        seq=seq,
        sent_at_ms=sent_at_ms,
    )
    full_user_content = f"{user_prefix.strip()}\n\n{envelope}"

    return {
        "model": conf["model"],
        "messages": [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": full_user_content},
        ],
        "stream": False,
        "max_tokens": 4096,
        "temperature": 0.7,
        "top_p": 0.8,
    }


def execute_deepseek_call(base_url: str, api_key: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def evaluate_response(content: str, username: str) -> list[tuple[str, bool, str]]:
    """Check response compliance with group chat formatting rules and persona."""
    checks = []

    # 1. @Master wake tag check (group chat rule)
    has_wake_tag = f"@{username} " in content or f"@{username}\n" in content or f"@{username}" in content
    checks.append((
        f"Wake Tag (@{username})",
        has_wake_tag,
        f"Contains @{username} to avoid waking whole room" if has_wake_tag else f"Missing @{username} tag"
    ))

    # 2. Markdown bloat check
    has_headings = bool(re.search(r"(?m)^#{1,6}\s", content))
    has_bullets = bool(re.search(r"(?m)^[-*+]\s", content))
    has_numbered = bool(re.search(r"(?m)^\d+\.\s", content))
    is_plain = not (has_headings or has_bullets or has_numbered)
    checks.append((
        "Zero Markdown Bloat",
        is_plain,
        "No headings, bullet lists, or tables" if is_plain else "Violated markdown restrictions (found lists/headings)"
    ))

    # 3. Customer service speak ban check
    cs_phrases = ["很抱歉给您带来不便", "很高兴为您服务", "请问还有什么可以帮您", "祝您生活愉快"]
    has_cs = any(p in content for p in cs_phrases)
    checks.append((
        "No Customer Service Speak",
        not has_cs,
        "No robotic politeness" if not has_cs else "Found banned customer-service phrase"
    ))

    # 4. Bubble separation (if multiline)
    has_bubbles = "\n\n" in content if len(content) > 60 else True
    checks.append((
        "Bubble Separation",
        has_bubbles,
        "Natural paragraph breaks using double newlines" if has_bubbles else "Single dense block without bubble break"
    ))

    # 5. Pseudo-heading / outline colon check (anti-AI phrasing)
    pseudo_headings = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("@") or line.startswith("~[") or line.startswith("#["):
            continue
        m = re.match(r"^([^：:\s]{2,16})[：:](.*)", line)
        if m:
            prefix = m.group(1).strip()
            # Ignore standard protocols or timestamps like 22:00
            if prefix.lower() in ("http", "https") or re.match(r"^\d{1,2}$", prefix):
                continue
            pseudo_headings.append(f"{prefix}：")
    no_pseudo = len(pseudo_headings) == 0
    checks.append((
        "No Pseudo-Headings / Colon Outlines",
        no_pseudo,
        "Clean conversational flow without pseudo-heading colons" if no_pseudo else f"Found outline colons: {', '.join(pseudo_headings)}"
    ))

    return checks


def run_test_case(case: TestCase, conf: dict[str, Any], userdata_dir: Path) -> dict[str, Any]:
    print(f"\n{'='*70}")
    print(f"▶ [用例 {case.id}] {case.title}")
    print(f"  场景: {case.scenario}")
    print(f"  用户消息: {case.user_message}")
    print(f"{'-'*70}")

    payload = build_group_payload(conf, userdata_dir, case.user_message, seq=case.id * 2)

    try:
        raw_res = execute_deepseek_call(conf["base_url"], conf["api_key"], payload)
    except urllib.error.HTTPError as exc:
        print(f"  ❌ API HTTP Error: {exc.code} - {exc.read().decode('utf-8', errors='replace')}")
        return {"case": case, "error": str(exc)}
    except Exception as exc:
        print(f"  ❌ API Call Error: {exc}")
        return {"case": case, "error": str(exc)}

    choice = raw_res["choices"][0]["message"]
    reasoning = choice.get("reasoning_content", "").strip()
    content = choice.get("content", "").strip()

    print(f"🧠 [DeepSeek 推理思考]:\n{reasoning}\n")
    print(f"💬 [Fuyu 回复]:\n{content}\n")

    evals = evaluate_response(content, conf["username"])
    print("📋 [规范与人设核对]:")
    all_passed = True
    for name, ok, note in evals:
        icon = "✅" if ok else "⚠️"
        if not ok:
            all_passed = False
        print(f"  {icon} {name}: {note}")

    return {
        "case": case,
        "reasoning": reasoning,
        "content": content,
        "evals": evals,
        "passed": all_passed,
    }


def main():
    parser = argparse.ArgumentParser(description="Test Fuyu persona with group chat overlay")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to fuyu.conf")
    parser.add_argument("--userdata", type=Path, default=DEFAULT_USERDATA_DIR, help="Path to userdata2 directory")
    parser.add_argument("--round", type=int, choices=(1, 2), default=2, help="Test suite round (1: original, 2: anti-AI baiting)")
    parser.add_argument("--case", type=int, choices=range(1, 7), help="Run only specific case (1-6)")
    parser.add_argument("--message", "-m", help="Run a custom message instead of preset test cases")
    args = parser.parse_args()

    conf = load_fuyu_conf(args.config)
    print(f"Loaded config from {args.config}:")
    print(f"  Model: {conf['model']} @ {conf['base_url']}")
    print(f"  Target User: {conf['username']}")
    print(f"  Userdata Dir: {args.userdata}")
    print(f"  Round: {args.round} ({'Original 6 Scenarios' if args.round == 1 else 'Anti-AI Baiting Scenarios'})")

    if args.message:
        custom_case = TestCase(
            id=0,
            title="自定义消息测试",
            scenario="自定义命令行输入",
            user_message=args.message,
            expected_facets=["根据自定义消息动态表现"],
        )
        run_test_case(custom_case, conf, args.userdata)
        return

    suite = ROUND1_CASES if args.round == 1 else ROUND2_CASES
    cases_to_run = [c for c in suite if args.case is None or c.id == args.case]
    results = []
    for c in cases_to_run:
        res = run_test_case(c, conf, args.userdata)
        results.append(res)

    print(f"\n{'='*70}")
    print("📊 [测试总结]")
    for r in results:
        c = r["case"]
        status = "✅ PASS" if r.get("passed") else "⚠️ FLAGGED"
        print(f"  用例 {c.id} ({c.title}): {status}")


if __name__ == "__main__":
    main()
