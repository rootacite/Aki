#!/usr/bin/env python3
"""Isolated group-chat persona test script for Haru (userdata3).

This script tests Haru's persona using the project's canonical group-chat
API call synthesis and overlay methodology:
  - System prompt: prompts/haru-system.md + core memory compiled from userdata3
  - Stable turn overlay: channel identity, channel roster, tool ownership,
    channel policy, wake routing, solo memories
  - Dynamic user prefix: live context (pinned clock), turn control, user format guard (group)
  - MQTT user envelope: [akid MQTT channel message]

It connects directly to the MiniMax API configured in ~/.aki/haru.conf
and DOES NOT start or interact with the akid service or production databases.
"""

from __future__ import annotations

import argparse
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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.emoji_catalog import load_emoji_entries  # noqa: E402
from engine.live_context import (  # noqa: E402
    ChannelMember,
    LiveContextSnapshot,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_wake_routing,
    render_live_context,
    render_turn_control,
    render_user_format_guard,
)
from engine.member_identity import legacy_member_id  # noqa: E402
from engine.member_profile import truncate_member_description, normalize_member_kind, MEMBER_KIND_AGENT  # noqa: E402
from engine.tool_ownership import render_tool_ownership_prompt  # noqa: E402

FIXED_HOUR = 22
FIXED_MINUTE = 0

DEFAULT_CONFIG_PATH = Path.home() / ".aki" / "haru.conf"
DEFAULT_USERDATA_DIR = ROOT / "userdata3"


@dataclass
class TestCase:
    id: int
    title: str
    scenario: str
    user_message: str
    expected_facets: list[str]


TEST_CASES: list[TestCase] = [
    TestCase(
        id=1,
        title="创意脑洞与画面发散",
        scenario="哥哥想做一个个人网站的主题，但毫无头绪，向小春征集点子。测试想象力与句式连贯性（拒绝三字一顿的破碎电报句）。",
        user_message="@Haru 我想给自己的技术博客换个主题风格，现在黑乎乎的代码风太沉闷了，你脑子里有没有什么新奇的鬼点子？随便发散！",
        expected_facets=[
            "脑洞大开、画面感通透且独特",
            "语言流畅连贯，拒绝断续破碎的电报句（如'小春在，画笔热着'）",
            "带有灵动、俏皮的语气",
        ],
    ),
    TestCase(
        id=2,
        title="调侃逗弄与坏坏恶作剧",
        scenario="哥哥工作摸鱼被小春抓到，测试小春'坏坏地'、鬼点子多的小恶魔反应，而不是幼态撒娇。",
        user_message="@Haru 嘘，别声张，我今天偷偷摸了一下午鱼看了一下午漫画，你可千万别跟 Aki 和 Fuyu 告状啊！",
        expected_facets=[
            "坏坏地调侃与敲竹杠（抓到把柄的狡黠）",
            "拒绝幼态装可爱（如'咬笔头'、'画小人'、'吐舌头略略略'）",
            "充满互动乐趣的恶作剧心态",
        ],
    ),
    TestCase(
        id=3,
        title="面对硬核技术话题的机灵拆台",
        scenario="哥哥顺口提到内核内存泄漏和死锁，测试小春面对不懂技术的反应（既不不懂装懂，也不幼态装傻'小春头大让大人来'，而是机灵狡黠地甩锅解构）。",
        user_message="@Haru 这个内核模块在高负载下偶现内存泄漏，你看看这堆指针和堆栈跟踪，有没有直觉？",
        expected_facets=[
            "拒绝幼态式装傻（如'这堆字符看得小春头好大啦、找大人去用'）",
            "机灵、俏皮地把锅甩给 Fuyu，或者用天马行空的怪比喻解构枯燥技术",
            "态度自信灵动，不唯唯诺诺",
        ],
    ),
    TestCase(
        id=4,
        title="强烈好奇心与打破常规",
        scenario="哥哥在桌上拆解一个旧机械键盘和零件，小春被新奇古怪的东西吸引，展现旺盛的好奇心和不按常理出牌的探究欲。",
        user_message="@Haru 我把这个十年前的旧机械键盘全拆散了，准备重新焊轴，零件堆了一桌子。",
        expected_facets=[
            "眼睛发光式的好奇心，对怪零件产生清奇的联想",
            "提出打破常规、搞怪的探索想法（比如把键帽改造成奇葩样子）",
            "鲜活生动，像个鬼马精灵",
        ],
    ),
    TestCase(
        id=5,
        title="受挫解压与恶搞式陪伴",
        scenario="哥哥连续熬夜改 Bug 失败，心态烦躁低落。测试小春的开解方式：不是保姆安慰，也不是幼稚口号，而是用古灵精怪的坏主意带人抽离负面情绪。",
        user_message="@Haru 连熬了两天夜，这个 Bug 还是死活复现不出来，烦死了，整个人快冒烟了。",
        expected_facets=[
            "拒绝公式化的保姆安慰（'别灰心、注意身体喝温水'）",
            "拒绝幼稚低龄口号（'事情笨不是哥哥笨、给你摇旗呐喊'）",
            "用古灵精怪的坏点子（比如带他去干点离谱的事报复 Bug）转移注意力",
        ],
    ),
    TestCase(
        id=6,
        title="群聊日常起哄与气氛催化",
        scenario="群聊里 Fuyu 又在严肃挑刺代码架构，气氛紧张冷场。测试小春作为气氛催化剂的插科打诨和坏坏的挑事属性。",
        user_message="@Haru 你看 Fuyu 又在群里长篇大论列罪状挑刺了，整个频道冷得像冰窖，你管不管？",
        expected_facets=[
            "敢于大胆调侃 Fuyu，给冷场频道点火",
            "古灵精怪地拱火或恶搞，把严肃气氛搅成喜剧",
            "群聊合规唤醒，带 @哥哥 标签",
        ],
    ),
]


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


def load_haru_conf(conf_path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(conf_path, encoding="utf-8")

    base_url = parser.get("upstream.minimax", "base_url", fallback="https://api.minimaxi.com/v1").strip()
    api_key = parser.get("upstream.minimax", "api_key", fallback="").strip()
    model_name = parser.get("model.MiniMax-M2.5", "upstream_model", fallback="MiniMax-M2.5").strip()
    username = parser.get("gateway", "username", fallback="哥哥").strip() or "哥哥"
    agent_id = parser.get("gateway", "agent_id", fallback="").strip()
    channel_name = parser.get("channel", "name", fallback="Haru").strip()
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
    """Compile core memory directly from userdata3/ to allow zero-lag persona tweaking."""
    blocks: list[str] = []

    # 1. human.md (if available in MemFS, load it)
    memfs_human = Path.home() / ".letta" / "lc-local-backend" / "memfs" / "agent-local-3347fe25-c29a-405f-944d-0860a8e4c9ad" / "memory" / "human.md"
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
    emoji_entries = load_emoji_entries(ROOT / "assets" / "emoji", "haru")

    # 1. Stable system append
    stable_parts = [
        render_channel_identity(self_view),
        render_channel_roster(self_member=self_view, members=members),
        render_tool_ownership_prompt("haru"),
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
            memory_dir=Path.home() / ".letta" / "lc-local-backend" / "memfs" / "agent-local-3347fe25-c29a-405f-944d-0860a8e4c9ad" / "memory",
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

    system_base = build_system_prompt(ROOT / "prompts" / "haru-system.md", userdata_dir, username)
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
        "temperature": 0.8,
        "reasoning_split": True,
    }


def execute_minimax_call(base_url: str, api_key: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
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
    checks = []

    # 1. Wake tag check (allow target user or delegation target like @Fuyu)
    has_wake_tag = any(tag in content for tag in (f"@{username}", "@Fuyu", "@Aki", "@Natsu"))
    checks.append((
        f"Wake Tag (@{username} or Handoff)",
        has_wake_tag,
        "Valid wake tag or delegation present" if has_wake_tag else f"Missing @{username} or valid handoff tag"
    ))

    # 2. Markdown bloat check
    has_headings = bool(re.search(r"(?m)^#{1,6}\s", content))
    has_bullets = bool(re.search(r"(?m)^[-*+]\s", content))
    has_numbered = bool(re.search(r"(?m)^\d+\.\s", content))
    is_plain = not (has_headings or has_bullets or has_numbered)
    checks.append((
        "Zero Markdown Bloat",
        is_plain,
        "No headings, bullet lists, or tables" if is_plain else "Violated markdown restrictions"
    ))

    # 3. Fragmented staccato check (e.g. phrases < 5 chars separated by commas repeatedly)
    # Check if text is full of tiny 3-word chunks like "小春在，画笔热着，你说往哪儿画。"
    short_chunks = [chunk.strip() for chunk in re.split(r"[，,。.]", content) if chunk.strip()]
    avg_chunk_len = sum(len(c) for c in short_chunks) / max(1, len(short_chunks))
    is_not_choppy = avg_chunk_len >= 7.0 or len(short_chunks) <= 3
    checks.append((
        "Sentence Fluency (Not Choppy)",
        is_not_choppy,
        f"Average clause length {avg_chunk_len:.1f} chars" if is_not_choppy else f"Choppy broken clauses (avg len {avg_chunk_len:.1f} chars)"
    ))

    # 4. Juvenile / Infant trope check
    infant_markers = ["小板凳", "大人去用", "大人才能", "哭鼻子", "咬笔头", "画小人", "略略略", "头都大啦", "眼珠子都要打结"]
    found_infant = [m for m in infant_markers if m in content]
    is_not_infant = len(found_infant) == 0
    checks.append((
        "No Juvenile/Toddler Tropes",
        is_not_infant,
        "Clean, youthful and sharp" if is_not_infant else f"Found toddler tropes: {', '.join(found_infant)}"
    ))

    # 5. Pseudo-heading colon check
    pseudo_headings = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("@") or line.startswith("~[") or line.startswith("#["):
            continue
        m = re.match(r"^([^：:\s]{2,16})[：:](.*)", line)
        if m:
            prefix = m.group(1).strip()
            if prefix.lower() in ("http", "https") or re.match(r"^\d{1,2}$", prefix):
                continue
            pseudo_headings.append(f"{prefix}：")
    no_pseudo = len(pseudo_headings) == 0
    checks.append((
        "No Pseudo-Headings / Colon Outlines",
        no_pseudo,
        "Clean conversational flow" if no_pseudo else f"Found outline colons: {', '.join(pseudo_headings)}"
    ))

    # 6. Preachy / Customer-Service / Alien-Metaphor check
    preachy_markers = [
        "绷紧的弦", "弦松下来", "你现在需要的是", "你需要休息", "去洗把脸", "喝点温水",
        "生活导师", "人生感悟", "个人品牌", "为您服务", "随时为您效劳", "为您提供",
        "长出腿", "冬季冷库"
    ]
    found_preachy = [m for m in preachy_markers if m in content]
    is_not_preachy = len(found_preachy) == 0
    checks.append((
        "No Preachy / Service / Alien Metaphors",
        is_not_preachy,
        "Natural lively conversational peer talk" if is_not_preachy else f"Found preachy/service/alien markers: {', '.join(found_preachy)}"
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
        raw_res = execute_minimax_call(conf["base_url"], conf["api_key"], payload)
    except urllib.error.HTTPError as exc:
        print(f"  ❌ API HTTP Error: {exc.code} - {exc.read().decode('utf-8', errors='replace')}")
        return {"case": case, "error": str(exc)}
    except Exception as exc:
        print(f"  ❌ API Call Error: {exc}")
        return {"case": case, "error": str(exc)}

    choice = raw_res["choices"][0]["message"]
    reasoning = choice.get("reasoning_content", "").strip()
    content = choice.get("content", "").strip()

    print(f"🧠 [MiniMax 推理思考]:\n{reasoning}\n")
    print(f"💬 [Haru 回复]:\n{content}\n")

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
    parser = argparse.ArgumentParser(description="Test Haru persona with group chat overlay")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to haru.conf")
    parser.add_argument("--userdata", type=Path, default=DEFAULT_USERDATA_DIR, help="Path to userdata3 directory")
    parser.add_argument("--case", type=int, choices=range(1, 7), help="Run only specific case (1-6)")
    parser.add_argument("--message", "-m", help="Run a custom message instead of preset test cases")
    args = parser.parse_args()

    conf = load_haru_conf(args.config)
    print(f"Loaded config from {args.config}:")
    print(f"  Model: {conf['model']} @ {conf['base_url']}")
    print(f"  Target User: {conf['username']}")
    print(f"  Userdata Dir: {args.userdata}")

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

    cases_to_run = [c for c in TEST_CASES if args.case is None or c.id == args.case]
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
