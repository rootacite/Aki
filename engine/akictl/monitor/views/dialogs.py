"""Modal interactive dialogs: Model Switch, Reasoning, Compact, SoftReset, and NSFW 18+."""

from __future__ import annotations

from typing import Any, Callable
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from rich.align import Align
from rich.panel import Panel
from rich.text import Text

from ..theme import COLOR_DANGER, COLOR_PRIMARY, COLOR_SUCCESS, COLOR_WARNING


class BaseModal:
    title: str = "Dialog"

    def render(self) -> Any:
        raise NotImplementedError

    def handle_key(self, key: str) -> bool:
        """Handle key event. Return True if dialog should close."""
        if key in ("escape", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        """Handle mouse event. Return True if dialog should close."""
        return False


class NsfwAdultConfirmDialog(BaseModal):
    title = "⚠️ 18+ 成年确认 (Adult Verification)"

    def __init__(self, on_confirm: Callable[[], None]) -> None:
        self.on_confirm = on_confirm

    def render(self) -> Panel:
        text = Text()
        text.append("⚠️  注意: 即将解锁私密独处回忆\n\n", style="bold red")
        text.append("该智能体的独处历史中可能包含露骨、亲密或成人向 (NSFW) 交流内容。\n", style="white")
        text.append("请确认您已经年满 18 周岁，且处于私密安全的浏览环境。\n\n", style="white")
        text.append("按 [Enter] 或 [Y] 确认解锁  |  按 [Esc] 或 [N] 取消保持隐藏", style="bold yellow")

        return Panel(
            Align.center(text),
            title="[bold red]🔞 成年内容防误触警告[/]",
            border_style="bold red",
            padding=(1, 2),
            width=65,
        )

    def handle_key(self, key: str) -> bool:
        if key in ("enter", "y", "Y"):
            self.on_confirm()
            return True
        if key in ("escape", "n", "N", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            y = mouse_event.position.y
            if 0 <= y < len(lines):
                line = lines[y]
                if "确认解锁" in line or "[Enter]" in line or "[Y]" in line:
                    return self.handle_key("enter")
                if "取消保持隐藏" in line or "[Esc]" in line or "[N]" in line:
                    return self.handle_key("escape")
        return False


class CompactConfirmDialog(BaseModal):
    title = "上下文压缩 (Compact Context)"

    def __init__(self, profile: str, on_confirm: Callable[[bool], None]) -> None:
        self.profile = profile
        self.on_confirm = on_confirm
        self.omit_tool_results = True

    def render(self) -> Panel:
        text = Text()
        text.append(f"确认对智能体 [{self.profile.upper()}] 执行上下文压缩？\n\n", style="bold cyan")
        text.append("• 规则: 保留最新 40% 的消息，压缩更早历史并触发后台记忆反思。\n", style="white")
        check_str = "[x]" if self.omit_tool_results else "[ ]"
        text.append(f"\n选项: {check_str} 剔除工具结果 (omit-tool-results)\n", style="bold yellow")
        text.append("  (勾选后将压缩历史中的 toolResult 替换为 [Omitted]，进一步节省上下文)\n\n", style="dim")
        text.append("按 [Space] 切换勾选  |  按 [Enter] 确认执行  |  按 [Esc] 取消", style="bold white")

        return Panel(
            Align.center(text),
            title=f"[bold cyan]上下文压缩确认 ({self.profile})[/]",
            border_style="bold cyan",
            padding=(1, 2),
            width=68,
        )

    def handle_key(self, key: str) -> bool:
        if key in ("space", " "):
            self.omit_tool_results = not self.omit_tool_results
            return False
        if key in ("enter", "y", "Y"):
            self.on_confirm(self.omit_tool_results)
            return True
        if key in ("escape", "n", "N", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            y = mouse_event.position.y
            if 0 <= y < len(lines):
                line = lines[y]
                if "剔除工具结果" in line or "[x]" in line or "[ ]" in line:
                    self.omit_tool_results = not self.omit_tool_results
                    return False
                if "确认执行" in line or "[Enter]" in line:
                    return self.handle_key("enter")
                if "取消" in line or "[Esc]" in line:
                    return self.handle_key("escape")
        return False


class SoftResetWarningDialog(BaseModal):
    title = "⚠️ Soft Reset 重置确认"

    def __init__(self, profile: str, on_confirm: Callable[[bool], None]) -> None:
        self.profile = profile
        self.on_confirm = on_confirm
        self.solo_target = False

    def render(self) -> Panel:
        text = Text()
        text.append(f"⚠️  警告: 即将重置智能体 [{self.profile.upper()}] 的上下文轮次！\n\n", style="bold yellow")
        text.append("【影响说明】\n", style="bold white")
        text.append("• 清空当前活动轮次，保留全部本地频道记录与持久记忆。\n", style="white")
        text.append("• 系统会在重置前自动创建现场完整备份快照。\n\n", style="white")

        text.append("【风险提示】\n", style="bold red")
        text.append("• 本操作虽然影响可控且可逆，但恢复过程繁琐！\n", style="bold red")
        text.append("  (如需恢复需手动从备份提取 messages.jsonl 并重新重写反思状态)\n\n", style="dim red")

        mode_str = "[*] 独处上下文 (Solo)" if self.solo_target else "[*] 群聊主上下文 (Group)"
        text.append(f"重置目标: {mode_str} (按 [Tab] 或 [Space] 切换)\n\n", style="bold magenta")
        text.append("按 [Enter] 确认执行重置  |  按 [Esc] 取消并返回", style="bold white")

        return Panel(
            Align.center(text),
            title=f"[bold red]⚠️ 谨慎操作: Soft Reset 确认[/]",
            border_style="bold red",
            padding=(1, 2),
            width=70,
        )

    def handle_key(self, key: str) -> bool:
        if key in ("tab", "space", " "):
            self.solo_target = not self.solo_target
            return False
        if key in ("enter", "y", "Y"):
            self.on_confirm(self.solo_target)
            return True
        if key in ("escape", "n", "N", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            y = mouse_event.position.y
            if 0 <= y < len(lines):
                line = lines[y]
                if "重置目标" in line or "独处" in line or "群聊" in line or "[Tab]" in line:
                    self.solo_target = not self.solo_target
                    return False
                if "确认执行" in line or "[Enter]" in line:
                    return self.handle_key("enter")
                if "取消" in line or "[Esc]" in line:
                    return self.handle_key("escape")
        return False


class ModelSwitchDialog(BaseModal):
    title = "切换模型 (Switch Model)"

    AVAILABLE_MODELS = [
        "aki/gemini-3.8-flash",
        "aki/gemini-3.5-flash-lite",
        "aki/deepseek-flash",
        "aki/gemma-4-26b-a4b-it",
        "aki/grok-4.6",
    ]

    def __init__(self, profile: str, current_model: str, on_confirm: Callable[[str, bool], None]) -> None:
        self.profile = profile
        self.on_confirm = on_confirm
        self.solo_mode = False
        self.selected_idx = 0
        for i, m in enumerate(self.AVAILABLE_MODELS):
            if current_model and (current_model in m or m in current_model):
                self.selected_idx = i
                break

    def render(self) -> Panel:
        text = Text()
        scope_str = "[*] 独处模型 (Solo)" if self.solo_mode else "[*] 群聊模型 (Group)"
        text.append(f"切换智能体 [{self.profile.upper()}] 上游模型\n", style="bold cyan")
        text.append(f"作用目标: {scope_str} (按 [Tab] 切换群聊/独处)\n\n", style="bold yellow")

        for idx, m in enumerate(self.AVAILABLE_MODELS):
            is_sel = (idx == self.selected_idx)
            prefix = " ❯ " if is_sel else "   "
            style = "bold #11111b on #89b4fa" if is_sel else "white"
            text.append(f"{prefix}{m}\n", style=style)

        text.append("\n按 [↑/↓] 选择模型 | [Enter] 确认切换 | [Esc] 取消", style="dim")

        return Panel(
            text,
            title="[bold cyan]模型快速切换[/]",
            border_style="bold cyan",
            padding=(1, 2),
            width=65,
        )

    def handle_key(self, key: str) -> bool:
        if key in ("tab", " "):
            self.solo_mode = not self.solo_mode
            return False
        if key in ("up", "k"):
            self.selected_idx = (self.selected_idx - 1) % len(self.AVAILABLE_MODELS)
            return False
        if key in ("down", "j"):
            self.selected_idx = (self.selected_idx + 1) % len(self.AVAILABLE_MODELS)
            return False
        if key in ("enter",):
            chosen = self.AVAILABLE_MODELS[self.selected_idx]
            self.on_confirm(chosen, self.solo_mode)
            return True
        if key in ("escape", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.handle_key("up")
            return False
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.handle_key("down")
            return False
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            y = mouse_event.position.y
            if 0 <= y < len(lines):
                line = lines[y]
                if "作用目标" in line or "[*]" in line or "[Tab]" in line:
                    self.solo_mode = not self.solo_mode
                    return False
                for idx, m in enumerate(self.AVAILABLE_MODELS):
                    if m in line:
                        if self.selected_idx == idx:
                            return self.handle_key("enter")
                        else:
                            self.selected_idx = idx
                            return False
                if "确认" in line or "[Enter]" in line:
                    return self.handle_key("enter")
                if "取消" in line or "[Esc]" in line:
                    return self.handle_key("escape")
        return False


class ReasoningDialog(BaseModal):
    title = "设置 Reasoning 深度"
    LEVELS = ["none", "low", "medium", "high"]

    def __init__(self, profile: str, current_effort: str, on_confirm: Callable[[str, bool], None]) -> None:
        self.profile = profile
        self.on_confirm = on_confirm
        self.solo_mode = False
        self.selected_idx = 1
        for i, lvl in enumerate(self.LEVELS):
            if current_effort and current_effort.lower() == lvl:
                self.selected_idx = i
                break

    def render(self) -> Panel:
        text = Text()
        scope_str = "[*] 独处 Reasoning (Solo)" if self.solo_mode else "[*] 群聊 Reasoning (Group)"
        text.append(f"设置智能体 [{self.profile.upper()}] Reasoning 思考深度\n", style="bold cyan")
        text.append(f"作用目标: {scope_str} (按 [Tab] 切换群聊/独处)\n\n", style="bold yellow")

        for idx, lvl in enumerate(self.LEVELS):
            is_sel = (idx == self.selected_idx)
            prefix = " ❯ " if is_sel else "   "
            style = "bold #11111b on #89b4fa" if is_sel else "white"
            text.append(f"{prefix}{lvl.upper()}\n", style=style)

        text.append("\n按 [↑/↓] 选择档位 | [Enter] 确认 | [Esc] 取消", style="dim")

        return Panel(
            text,
            title="[bold cyan]Reasoning 深度调节[/]",
            border_style="bold cyan",
            padding=(1, 2),
            width=60,
        )

    def handle_key(self, key: str) -> bool:
        if key in ("tab", " "):
            self.solo_mode = not self.solo_mode
            return False
        if key in ("up", "k"):
            self.selected_idx = (self.selected_idx - 1) % len(self.LEVELS)
            return False
        if key in ("down", "j"):
            self.selected_idx = (self.selected_idx + 1) % len(self.LEVELS)
            return False
        if key in ("enter",):
            chosen = self.LEVELS[self.selected_idx]
            self.on_confirm(chosen, self.solo_mode)
            return True
        if key in ("escape", "q"):
            return True
        return False

    def handle_mouse(self, mouse_event: MouseEvent, lines: list[str]) -> bool:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.handle_key("up")
            return False
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.handle_key("down")
            return False
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            y = mouse_event.position.y
            if 0 <= y < len(lines):
                line = lines[y]
                if "作用目标" in line or "[*]" in line or "[Tab]" in line:
                    self.solo_mode = not self.solo_mode
                    return False
                for idx, lvl in enumerate(self.LEVELS):
                    if lvl.upper() in line.upper():
                        if self.selected_idx == idx:
                            return self.handle_key("enter")
                        else:
                            self.selected_idx = idx
                            return False
                if "确认" in line or "[Enter]" in line:
                    return self.handle_key("enter")
                if "取消" in line or "[Esc]" in line:
                    return self.handle_key("escape")
        return False
