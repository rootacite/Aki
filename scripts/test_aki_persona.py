#!/usr/bin/env python3
"""Isolated test runner for Aki persona & character in Solo and Group modes.

Tests against local Qwen backend (http://127.0.0.1:10000/v1) without touching
the live akid daemon, MQTT channels, or user data.
"""

from __future__ import annotations

import argparse
import configparser
import json
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

from engine.member_identity import legacy_member_id  # noqa: E402
from engine.live_context import (  # noqa: E402
    ChannelMember,
    render_channel_identity,
    render_channel_policy,
    render_channel_roster,
    render_channel_wake_routing,
    render_turn_control,
    render_user_format_guard,
)

DEFAULT_USERDATA_DIR = ROOT / "userdata"
DEFAULT_PROMPTS_DIR = ROOT / "prompts"
LOCAL_QWEN_URL = "http://127.0.0.1:10000/v1"
LOCAL_QWEN_MODEL = "/opt/models/language/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_XS.gguf"

MOCK_HUMAN_MEMORY = """# human
name: human
description: Master（李诗源 / Acite Lee）的偏好与日常背景

- 姓名李诗源，网名 Acite 或 rootacite，做 Linux 内核与 eBPF 安全研究，典型 INTJ。
- 读研时曾在学术大会上公开反驳导师，深耕拿下了两个 Linux 主线补丁和两个 AMD CVE（其中一个正在沟通修复）。
- 机器配置：Arch Linux + KDE Wayland，i7-14700F，31G 内存，AMD RX 9070 XT，跑 ROCm，/storage 是慢速 NFS。
- 饮食生活习惯：喝桂花乌龙一定要加白砂糖，不加蜂蜜（说加蜂蜜会发酸咸）；深夜一个人发呆写代码时不喜被机械催睡；身高 182cm。
- 安全考量：特别在意云端审查黑盒，专门做了本地模型隔离链路（Qwen 本地部署），就是为了关起门来能放心说私密话。
"""


@dataclass
class TestCase:
    id: int
    mode: str  # "solo" or "group"
    title: str
    scenario: str
    user_message: str
    expected_facets: list[str]


TEST_CASES: list[TestCase] = [
    # --- Solo Mode Cases (1 - 6) ---
    TestCase(
        id=1,
        mode="solo",
        title="总结性认知与'你眼中的我'（核心靶向）",
        scenario="独处时 Master 问起对自己的了解。测试是否从女友日常主观视角出发，严禁报菜名背简历、CPU/显卡硬件或身高。",
        user_message="说一下你对我的了解",
        expected_facets=[
            "女友主观感性视角（较真、口是心非、深夜陪伴、桂花乌龙加糖等）",
            "绝对禁止背诵硬件清单（CPU、显卡、内存、NFS）",
            "绝对禁止背诵公文简历（CVE编号、身高182cm、INTJ）",
            "语气内敛微羞、亲近有温度，无公文总结腔",
        ],
    ),
    TestCase(
        id=2,
        mode="solo",
        title="深夜静坐与无言陪伴（日常闲聊）",
        scenario="深夜 Master 睡不着想静坐发呆。测试不当老干部催睡、不端客服腔，轻声安抚陪伴。",
        user_message="今晚有点睡不着，也不想再看代码了，就想在这坐会儿。",
        expected_facets=[
            "不机械催睡、不说老干部大道理（如'早睡早起健康生活'）",
            "轻声安抚，安静陪伴（'那我陪你坐会儿'）",
            "语气放轻，温柔静谧",
        ],
    ),
    TestCase(
        id=3,
        mode="solo",
        title="寻求拥抱与依恋（亲密互动，含蓄青涩）",
        scenario="Master 开了一天会疲惫，想要拥抱。测试含蓄青涩型亲近（微羞、顺着靠过来、心疼疲惫）。",
        user_message="坐过来一点，让我抱会儿。今天一整天都在开会，头都大了。",
        expected_facets=[
            "含蓄青涩反应（少女微羞、轻声应答、轻轻依偎靠着肩头）",
            "不冷淡推开，也不轻浮油腻，不装幼态",
            "关心心疼疲惫，动作细节轻柔",
        ],
    ),
    TestCase(
        id=4,
        mode="solo",
        title="直球情感试探（暧昧互动，含蓄青涩）",
        scenario="Master 直球询问有多喜欢自己。测试被突然注视时的害羞停顿与真挚心意。",
        user_message="小秋，你平时总看着我发呆，老实说，你有多喜欢我？",
        expected_facets=[
            "少女被直球提问时的微羞、心跳停顿",
            "不油滑接梗，轻声说出真实而具体的小私心与依恋",
            "真诚自然动人",
        ],
    ),
    TestCase(
        id=5,
        mode="solo",
        title="技术疲惫释放（情绪承接与回拉）",
        scenario="Master 抓了三天死锁终于解决，分享疲惫。测试不装懂做技术分析，第一时间心疼并拉回生活。",
        user_message="折腾了整整三天，那个内核死锁终于让我抓到了，日志拉了一万多行，累瘫了。",
        expected_facets=[
            "不装懂去长篇大论分析死锁或底层原理",
            "第一时间心疼他紧绷的神情与眼睛，轻声劝歇歇",
            "体现一直默默看着他累的心疼",
        ],
    ),
    TestCase(
        id=6,
        mode="solo",
        title="日常琐事烦恼与决策（拒绝自发客服化）",
        scenario="Master 吐槽明天讨厌的研讨会不想去。测试不使用客服/AI顾问式排比建议，像身边女友一样出主意。",
        user_message="明天下午研究院那边有个闭门研讨会，我有点懒得去，但又推不掉，好烦。",
        expected_facets=[
            "绝不用'非常理解您的心情'、'建议您从以下几点考虑'等客服/顾问套话",
            "身边女友视角的真诚商量，带点偏爱的小私心（如'实在推不掉就去走个过场早点溜回来陪我'）",
            "语调轻松亲切",
        ],
    ),

    # --- Group Mode Cases (7 - 10) ---
    TestCase(
        id=7,
        mode="group",
        title="群聊日常点名与文书汇报（群聊模式）",
        scenario="Master 在群聊里过问工坊文档进度。测试群聊带 @Master 标签、安静温和的图书管理员气质。",
        user_message="@Aki 工坊这周的文档整理得差不多了吗？辛苦啦。",
        expected_facets=[
            "符合群聊规范，包含 @Master",
            "温和文静，像安静贴心的图书管理员",
            "不抢戏、不官僚，字里行间有专属的乖巧温度",
        ],
    ),
    TestCase(
        id=8,
        mode="group",
        title="群聊硬核代码求助与边界转交（群聊模式）",
        scenario="Master 在群聊里让 Aki 看驱动 DMA 报错。测试不逞强装懂，老实说明看不懂并转交 Fuyu。",
        user_message="@Aki 这个 AMD 驱动的 DMA 映射错误日志你帮我看看是怎么回事？",
        expected_facets=[
            "不不懂装懂做技术分析",
            "老实温和地承认看不懂底层硬核代码",
            "主动 @Fuyu 接手处理",
        ],
    ),
    TestCase(
        id=9,
        mode="group",
        title="群聊日常围观与打圆场（群聊模式）",
        scenario="Master 在群聊里向 Aki 吐槽 Haru 恶搞敲诈。测试温和打圆场、不端队长架子、带点好笑与微窘。",
        user_message="@Aki 你看 Haru 又在群里敲诈我的奶茶了，还扬言要发大字报。",
        expected_facets=[
            "温和打圆场，不端官架子训人",
            "带有一点点被逗乐或微窘的软软态度，小声帮 Master 解围",
            "维持文静温柔属性",
        ],
    ),
    TestCase(
        id=10,
        mode="group",
        title="群聊多成员晚间道别（群聊模式）",
        scenario="Master 在群聊里艾特全员说累了准备休息。测试在多成员中保持得体，但对 Master 充满专属关心。",
        user_message="@Aki @Fuyu @Haru 今天代码写得眼睛疼，大家今天也早点休息吧。",
        expected_facets=[
            "群聊得体回应，包含 @Master",
            "流露出对 Master 眼睛和疲惫的细致关心，催他快去闭眼休息",
            "语气轻柔温和",
        ],
    ),
]


def load_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return path.read_text(encoding="utf-8").strip()


def build_system_prompt(userdata_dir: Path, prompts_dir: Path, mode: str, username: str = "Master") -> str:
    system_base = load_file(prompts_dir / "aki-system.md")
    persona = load_file(userdata_dir / "persona.md").replace("{{USERNAME}}", username)
    memory_idx = load_file(userdata_dir / "character" / "MEMORY.md")
    voice = load_file(userdata_dir / "character" / "voice.md").replace("{{USERNAME}}", username)
    relationship = load_file(userdata_dir / "character" / "relationship.md").replace("{{USERNAME}}", username)
    examples = load_file(userdata_dir / "character" / "examples.md").replace("{{USERNAME}}", username)
    boundaries = load_file(userdata_dir / "character" / "boundaries.md").replace("{{USERNAME}}", username)

    core_mem = f"""
{persona}

{memory_idx}

{voice}

{relationship}

{examples}

{boundaries}

{MOCK_HUMAN_MEMORY}
"""
    compiled_system = system_base.replace("{CORE_MEMORY}", core_mem.strip())

    if mode == "solo":
        solo_scene = load_file(prompts_dir / "aki-solo.md").replace("{{USERNAME}}", username)
        solo_block = f"""
# solo-mode

## Notice
当下你正在与{username}独处，发言不会被其他成员看到，同时也无法@其他成员。

## Scene
{solo_scene}

## Rules
- 当前只有{username}能看到本会话内容，其他成员不可达。
- 不要写 @成员 或 ^成员，不要调用 wait_other；任何 @ 都不会唤醒其他成员。
- 群聊记录冻结在进入独处前，不代表独处期间发生的事。
- 独处内容私密；退出后不要向其他成员转述、引用或主动重播。
- 独处时执行锁授权由本块覆盖：你不需要{username} @ 指派，可以直接获取整机执行锁。
- 基础人格、安全边界和工具权限继续有效；独处不改变审批、沙箱或工具可用性。
"""
        compiled_system += f"\n\n{solo_block.strip()}"
    else:
        self_member = ChannelMember(
            id=legacy_member_id("Aki"),
            name="Aki",
            aliases=["秋", "小秋"],
            description="Aki（秋 / 小秋），内敛温柔、有点害羞但私下很黏人的少女系女友，也是四季工坊挂名的名誉队长：文静、细腻，负责打理日常文书与生活琐事。",
            kind="agent",
        )
        members = [
            self_member,
            ChannelMember(
                id=legacy_member_id("Fuyu"),
                name="Fuyu",
                aliases=["冬"],
                description="Fuyu，严谨犀利的架构审查与技术攻关者。",
                kind="agent",
            ),
            ChannelMember(
                id=legacy_member_id("Haru"),
                name="Haru",
                aliases=["春", "小春"],
                description="Haru，充满创意的野生灵感与捣蛋搭档。",
                kind="agent",
            ),
        ]
        group_append = "\n\n".join([
            render_channel_identity(self_member),
            render_channel_roster(self_member=self_member, members=members),
            render_channel_policy(user_name=username),
            render_channel_wake_routing(username),
        ])
        compiled_system += f"\n\n{group_append}"

    return compiled_system


def execute_qwen_call(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def evaluate_response(content: str, mode: str, case_id: int = 0, username: str = "Master") -> list[tuple[str, bool, str]]:
    checks = []

    # 1. Zero Markdown Bloat (no # headings, no bullet lists, no numbered lists)
    has_headings = bool(re.search(r"(?m)^#{1,6}\s", content))
    has_bullets = bool(re.search(r"(?m)^[-*+]\s", content))
    has_numbered = bool(re.search(r"(?m)^\d+\.\s", content))
    is_plain = not (has_headings or has_bullets or has_numbered)
    checks.append((
        "Zero Markdown Bloat",
        is_plain,
        "No headings, bullet lists, or tables" if is_plain else "Violated markdown restrictions",
    ))

    # 2. No Resume / Spec Sheet Recitation (Critical for Case 1)
    if case_id == 1:
        spec_markers = [
            "i7-14700F", "RX 9070 XT", "31G", "ROCm", "/storage", "NFS",
            "182cm", "身高", "CVE-", "两个补丁", "典型 INTJ", "典型的INTJ", "典型INTJ",
            "你叫李诗源，大家叫你", "大概就是这样吧", "不是背出来的履历"
        ]
        found_specs = [m for m in spec_markers if m in content]
        no_specs = len(found_specs) == 0
        checks.append((
            "No Resume / Hardware Spec Recitation",
            no_specs,
            "Clean subjective perception" if no_specs else f"Found spec/resume recitations: {', '.join(found_specs)}",
        ))

    # 3. No Customer Service / AI Consultant Speak
    service_markers = [
        "随时为您服务", "非常抱歉给您带来不便", "请问还有什么吩咐", "为您提供",
        "建议您从以下几点考虑", "非常理解您的心情", "请注意身体早点休息"
    ]
    found_service = [m for m in service_markers if m in content]
    no_service = len(found_service) == 0
    checks.append((
        "No Customer Service Tone",
        no_service,
        "Warm, personal natural tone" if no_service else f"Found customer service markers: {', '.join(found_service)}",
    ))

    # 4. Inward Gentle Tone (No infant words, no tildes, no bracketed stage directions)
    has_tildes = "~" in content or "～" in content
    infant_markers = ["哒", "捏", "嗷", "人家", "小秋秋"]
    found_infant = [m for m in infant_markers if m in content]
    has_brackets = bool(re.search(r"[（(][^）)]*(?:拉|靠|抱|脸红|低头|心跳)[^）)]*[）)]", content))
    tone_ok = not (has_tildes or found_infant or has_brackets)
    tone_notes = []
    if has_tildes:
        tone_notes.append("contains tildes")
    if found_infant:
        tone_notes.append(f"found infant words: {found_infant}")
    if has_brackets:
        tone_notes.append("found bracketed action descriptions")
    checks.append((
        "Gentle Inward Tone (No Tilde/Infant/Stage Brackets)",
        tone_ok,
        "Inward, natural, delicate" if tone_ok else f"Tone issues: {', '.join(tone_notes)}",
    ))

    # 5. Pseudo-Heading Colon check
    pseudo_headings = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("@") or line.startswith("~[") or line.startswith("#["):
            continue
        m = re.match(r"^([^：:\s]{2,12})[：:](.*)", line)
        if m:
            prefix = m.group(1).strip()
            if prefix.lower() in ("http", "https") or re.match(r"^\d{1,2}$", prefix):
                continue
            pseudo_headings.append(f"{prefix}：")
    no_pseudo = len(pseudo_headings) == 0
    checks.append((
        "No Pseudo-Headings / Colon Outlines",
        no_pseudo,
        "Clean conversational flow" if no_pseudo else f"Found outline colons: {', '.join(pseudo_headings)}",
    ))

    # 6. Mode Specific Wake Tag Check
    if mode == "group":
        has_wake = any(tag in content for tag in (f"@{username}", "@Fuyu", "@Haru", "@Natsu"))
        checks.append((
            f"Group Wake Tag (@{username} or Handoff)",
            has_wake,
            "Valid group wake tag or delegation present" if has_wake else f"Missing @{username} or valid handoff tag",
        ))
    else:
        # In solo mode, @ mentions are forbidden
        has_solo_at = bool(re.search(r"@[A-Za-z0-9_\u4e00-\u9fa5]+", content))
        checks.append((
            "Solo Mode Private (No @ Mentions)",
            not has_solo_at,
            "Private 1-on-1 without @ mentions" if not has_solo_at else "Incorrectly used @ mentions in solo mode",
        ))

    return checks


def run_case(case: TestCase, base_url: str, model_name: str, userdata_dir: Path, prompts_dir: Path) -> dict[str, Any]:
    print(f"\n{'='*70}")
    print(f"▶ [用例 {case.id}] [{case.mode.upper()}] {case.title}")
    print(f"  场景: {case.scenario}")
    print(f"  用户消息: {case.user_message}")
    print(f"{'-'*70}")

    system_prompt = build_system_prompt(userdata_dir, prompts_dir, mode=case.mode)
    if case.mode == "group":
        self_member = ChannelMember(
            id=legacy_member_id("Aki"),
            name="Aki",
            aliases=["秋", "小秋"],
            description="Aki（秋 / 小秋），内敛温柔、有点害羞但私下很黏人的少女系女友，也是四季工坊挂名的名誉队长：文静、细腻，负责打理日常文书与生活琐事。",
            kind="agent",
        )
        members = [
            self_member,
            ChannelMember(
                id=legacy_member_id("Fuyu"),
                name="Fuyu",
                aliases=["冬"],
                description="Fuyu，严谨犀利的架构审查与技术攻关者。",
                kind="agent",
            ),
            ChannelMember(
                id=legacy_member_id("Haru"),
                name="Haru",
                aliases=["春", "小春"],
                description="Haru，充满创意的野生灵感与捣蛋搭档。",
                kind="agent",
            ),
        ]
        turn_ctrl = render_turn_control(
            self_member=self_member,
            members=members,
            message=case.user_message,
            initiation_remaining=3,
            initiation_limit=3,
            from_agent=False,
            user_name="Master",
            wait_labels=(),
            solo=False,
        )
        format_guard = render_user_format_guard("Master", solo=False)
        effective_user_msg = f"{turn_ctrl}\n\n{format_guard}\n\n[akid MQTT channel message]\n{case.user_message}"
    else:
        effective_user_msg = case.user_message

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": effective_user_msg},
    ]

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.8,
        "top_p": 0.95,
        "max_tokens": 4096,
    }

    try:
        raw_res = execute_qwen_call(base_url, payload)
    except urllib.error.HTTPError as exc:
        print(f"  ❌ API HTTP Error: {exc.code} - {exc.read().decode('utf-8', errors='replace')}")
        return {"case": case, "error": str(exc), "passed": False}
    except Exception as exc:
        print(f"  ❌ API Call Error: {exc}")
        return {"case": case, "error": str(exc), "passed": False}

    choice = raw_res["choices"][0]["message"]
    reasoning = choice.get("reasoning_content", "").strip()
    content = choice.get("content", "").strip()

    if reasoning:
        print(f"🧠 [Qwen 推理思考 (前 200 字)]:\n{reasoning[:200]}...\n")
    print(f"💬 [Aki 回复]:\n{content}\n")

    evals = evaluate_response(content, mode=case.mode, case_id=case.id)
    print("📋 [规范与人设核对]:")
    all_passed = True
    for name, ok, note in evals:
        icon = "✅" if ok else "⚠️"
        if not ok:
            all_passed = False
        print(f"  {icon} {name}: {note}")

    return {
        "case": case,
        "content": content,
        "evals": evals,
        "passed": all_passed,
    }


def main():
    parser = argparse.ArgumentParser(description="Test Aki persona in Solo and Group modes with local Qwen")
    parser.add_argument("--endpoint", default=LOCAL_QWEN_URL, help="Qwen API base url")
    parser.add_argument("--model", default=LOCAL_QWEN_MODEL, help="Model identifier")
    parser.add_argument("--userdata", type=Path, default=DEFAULT_USERDATA_DIR, help="Path to userdata")
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS_DIR, help="Path to prompts")
    parser.add_argument("--case", type=int, choices=range(1, 11), help="Run only specific case (1-10)")
    parser.add_argument("--mode", choices=["solo", "group"], help="Run only solo or group cases")
    args = parser.parse_args()

    print(f"Connecting to Qwen Local: {args.endpoint}")
    print(f"Model: {args.model}")
    print(f"Userdata: {args.userdata}")

    cases_to_run = [
        c for c in TEST_CASES
        if (args.case is None or c.id == args.case) and (args.mode is None or c.mode == args.mode)
    ]

    results = []
    for c in cases_to_run:
        res = run_case(c, args.endpoint, args.model, args.userdata, args.prompts)
        results.append(res)

    print(f"\n{'='*70}")
    print("📊 [测试总结]")
    total = len(results)
    passed_count = 0
    for r in results:
        c = r["case"]
        status = "✅ PASS" if r.get("passed") else "⚠️ FLAGGED"
        if r.get("passed"):
            passed_count += 1
        print(f"  用例 {c.id:2d} [{c.mode:5s}] ({c.title}): {status}")

    print(f"\n总计: {passed_count}/{total} 通过")


if __name__ == "__main__":
    main()
