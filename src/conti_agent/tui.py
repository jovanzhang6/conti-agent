from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.cursor_shapes import CursorShape, SimpleCursorShapeConfig
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout import (
    CompletionsMenu,
    ConditionalContainer,
    Dimension,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType, MouseEvent
from prompt_toolkit.widgets import TextArea
from .activity import format_tool_completed, format_tool_started
from .commands import CommandContext


STARTUP_LOGO = r"""
  ____    ___    _   _    _____  ___
 / ___|  / _ \  | \ | |  |_   _| |_ _|
| |     | | | | |  \| |    | |   | |
| |___  | |_| | | |\  |    | |   | |
 \____|  \___/  |_| \_|   |___| |___|
"""

# 单帧渲染的字符预算：pt 每帧会对全部可见内容重新折行，
# 超过预算就从最旧的消息开始省略（渲染层省略，不影响会话账本）。
_RENDER_CHAR_BUDGET = 48_000


@dataclass
class ChatMessage:
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)
    streaming: bool = False
    render_cache: tuple[str, list[tuple[str, str]]] | None = field(
        default=None, repr=False, compare=False
    )
    # 工具活动的可展开详情（参数与结果预览），Ctrl+O 切换显示。
    details: str | None = field(default=None, repr=False, compare=False)


_MD_INLINE = re.compile(r"(`[^`]+`|\*\*[^*\n]+?\*\*|\*[^*\n]+?\*)")


def _inline_fragments(text: str) -> list[tuple[str, str]]:
    """行内 Markdown：`代码`、**加粗**、*斜体*。"""
    fragments: list[tuple[str, str]] = []
    position = 0
    for match in _MD_INLINE.finditer(text):
        if match.start() > position:
            fragments.append(("", text[position:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            fragments.append(("class:md-code", token[1:-1]))
        elif token.startswith("**"):
            fragments.append(("class:md-bold", token[2:-2]))
        else:
            fragments.append(("class:md-italic", token[1:-1]))
        position = match.end()
    if position < len(text):
        fragments.append(("", text[position:]))
    return fragments or [("", "")]


def _markdown_fragments(text: str) -> list[tuple[str, str]]:
    """基础 Markdown 渲染：标题/加粗/行内代码/围栏代码/列表/引用/表格/分隔线。"""
    fragments: list[tuple[str, str]] = []

    def emit(style: str, content: str) -> None:
        fragments.append((style, content))
        fragments.append(("", "\n"))

    in_code_block = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            emit("class:md-codeblock", "  " + line)
            continue
        if not stripped:
            emit("", "")
            continue
        if stripped.startswith(">"):
            emit("class:md-quote", stripped)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = stripped.strip("|").split("|")
            if all(re.fullmatch(r":?\s*-{2,}\s*:?", cell.strip()) for cell in cells):
                emit("class:md-rule", "├" + "┬".join("─" * 6 for _ in cells) + "┤")
                continue
            row: list[tuple[str, str]] = []
            for index, cell in enumerate(cells):
                if index:
                    row.append(("class:md-rule", " │ "))
                row.extend(_inline_fragments(cell.strip()))
            fragments.extend(row)
            fragments.append(("", "\n"))
            continue
        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            emit("class:md-rule", "─" * 24)
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading:
            emit("class:md-heading", heading.group(2))
            continue
        bullet = re.match(r"([-*+])\s+(.*)", stripped)
        if bullet:
            fragments.append(("class:md-bullet", "• "))
            fragments.extend(_inline_fragments(bullet.group(2)))
            fragments.append(("", "\n"))
            continue
        fragments.extend(_inline_fragments(line))
        fragments.append(("", "\n"))
    return fragments


@dataclass
class ViewportState:
    """对话流的显式视口状态。

    坐标统一使用 prompt_toolkit wrap 模式的“逻辑行”（未折行的原始行）：
    scroll_offset 是视口锚点（游标所在逻辑行），content_height 是总逻辑行数，
    window_height 与 page_lines 是显示行数。follow_bottom 表示“跟随最新消息”；
    用户上翻后进入手动阅读模式，新消息不再抢占视图。
    """

    scroll_offset: int = 0
    follow_bottom: bool = True
    content_height: int = 0
    window_height: int = 0
    page_lines: int = 0

    @property
    def last_anchor(self) -> int:
        return max(0, self.content_height - 1)

    @property
    def page_size(self) -> int:
        return max(1, self.page_lines or self.window_height)

    def clamp(self) -> int:
        self.scroll_offset = max(0, min(self.scroll_offset, self.last_anchor))
        return self.scroll_offset

    def scroll_up(self, amount: int = 5) -> None:
        if amount <= 0:
            return
        # scroll_offset 由滚动条每帧从渲染结果同步回真实视口顶部，
        # 从跟随模式离开时直接从当前位置上移，而不是从内容末尾推算。
        self.follow_bottom = False
        self.scroll_offset = max(0, self.scroll_offset - amount)

    def scroll_down(self, amount: int = 5) -> None:
        if amount <= 0 or self.follow_bottom:
            return
        # 是否到达底部由渲染结果（最后一行是否可见）判定并恢复跟随。
        self.scroll_offset = min(self.last_anchor, self.scroll_offset + amount)

    def page_up(self) -> None:
        self.scroll_up(self.page_size - 1)

    def page_down(self) -> None:
        self.scroll_down(self.page_size - 1)

    def scroll_to_top(self) -> None:
        self.follow_bottom = False
        self.scroll_offset = 0

    def scroll_to_bottom(self) -> None:
        self.follow_bottom = True


class TuiState:
    """TUI 的纯状态层，便于不启动终端就测试。"""

    def __init__(self, runtime_info: dict[str, Any]) -> None:
        self.runtime_info = runtime_info
        self.messages: list[ChatMessage] = []
        self.status = "准备就绪"
        self.session_id = "新会话"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.busy = False
        self.tool_count = 0
        self.error_count = 0
        self.pending_activities: dict[str, tuple[str, dict[str, Any]]] = {}
        self.pending_activity_messages: dict[str, Any] = {}
        self.activity_expanded = False
        self.add_system(
            "输入任务后按 Enter 发送。/help 查看命令，Ctrl+C 取消当前任务，Ctrl+Q 退出。"
        )

    def append_message(self, role: str, text: str = "",
                       *, streaming: bool = False) -> ChatMessage:
        message = ChatMessage(role=role, text=text, streaming=streaming)
        self.messages.append(message)
        return message

    def add_system(self, text: str) -> ChatMessage:
        return self.append_message("system", text)

    def append_tool_activity(self, text: str) -> ChatMessage:
        """工具/系统活动行：直接进入主对话流（codex 风格）。"""
        return self.append_message("activity", text)

    def interrupt_activities(self) -> None:
        """任务被中断时，把未完成的"开始…"活动行标记为已中断。"""
        for message in self.pending_activity_messages.values():
            if not message.text.startswith(("✓", "✗")):
                message.text = f"✗ {message.text}（已中断）"
        self.pending_activity_messages.clear()

    def stream_delta(self, text: str) -> None:
        assistant = next(
            (item for item in reversed(self.messages)
             if item.role == "assistant" and item.streaming),
            None,
        )
        if assistant is None:
            assistant = self.append_message("assistant", streaming=True)
        assistant.text += text

    def finish_stream(self, final_text: str) -> None:
        for item in reversed(self.messages):
            if item.role == "assistant" and item.streaming:
                item.streaming = False
                item.text = final_text or item.text
                break

    def record_event(self, event: Any) -> None:
        event_type = event.type
        payload = event.payload
        if event_type == "tool.requested":
            self.tool_count += 1
            call_id = str(payload.get("tool_call_id", ""))
            tool_name = str(payload.get("tool_name", ""))
            arguments = payload.get("arguments", {})
            self.pending_activities[call_id] = (tool_name, arguments)
            message = self.append_tool_activity(
                format_tool_started(tool_name, arguments)
            )
            self.pending_activity_messages[call_id] = message
        elif event_type == "tool.completed":
            call_id = str(payload.get("tool_call_id", ""))
            tool_name, arguments = self.pending_activities.pop(
                call_id, (str(payload.get("tool_name", "")), {})
            )
            elapsed = payload.get("metadata", {}).get("elapsed")
            summary = format_tool_completed(
                tool_name, arguments,
                is_error=bool(payload.get("is_error")), elapsed=elapsed,
            )
            mark = "✗ " if payload.get("is_error") else "✓ "
            hint = "" if payload.get("is_error") else "（Ctrl+O 看详情）"
            message = self.pending_activity_messages.pop(call_id, None)
            if message is not None:
                message.text = mark + summary + hint
                message.render_cache = None
            else:
                message = self.append_tool_activity(mark + summary + hint)
            # 可展开详情：参数 + 结果预览（Ctrl+O 切换显示）。
            try:
                args_text = json.dumps(arguments, ensure_ascii=False)
            except TypeError:
                args_text = str(arguments)
            output = str(payload.get("output") or "")
            message.details = (
                f"参数：{args_text}\n"
                f"结果：{output[:1200]}" + ("…（已截断）" if len(output) > 1200 else "")
            )[:1500]
        elif event_type == "usage.recorded":
            self.usage["input_tokens"] += int(payload.get("input_tokens", 0))
            self.usage["output_tokens"] += int(payload.get("output_tokens", 0))
        elif event_type == "run.retry":
            self.append_tool_activity(f"↻ Provider 重试 {payload.get('attempt')}")
        elif event_type == "context.compacting":
            message = self.append_tool_activity("⚙ 正在压缩上下文…")
            self.pending_activity_messages["__compaction__"] = message
        elif event_type == "context.compacted":
            note = ("上下文超限" if payload.get("reason") == "overflow"
                    else "回复被截断" if payload.get("reason") == "truncated"
                    else "上下文接近上限")
            message = self.pending_activity_messages.pop("__compaction__", None)
            text = f"⚙ {note}，已压缩早期历史为摘要"
            if message is not None:
                message.text = text
            else:
                self.append_tool_activity(text)
        elif event_type == "team.delivered":
            # 团队消息/交付送达队长：一行活动 + 可 Ctrl+O 看全文。
            text = str(payload.get("text") or "")
            first_line = text.splitlines()[0] if text else ""
            self.append_tool_activity(f"👥 {first_line}（详情见对话流）")
        elif event_type == "run.failed":
            # 只更新状态与计数；错误详情由任务异常路径统一展示一次。
            self.error_count += 1
            self.status = "运行失败"
        elif event_type == "message.created":
            # 每轮模型文本到此定稿；下轮文本会另起新消息，
            # 保证工具活动行与对话保持真实时序。
            self.finish_stream(str(payload.get("text") or ""))

    def render_conversation(self) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        if not self.messages:
            return [("class:muted", "还没有对话。\n输入你的第一个任务。")]
        # 渲染预算：超预算时省略最早的消息，避免每帧重新折行全部历史
        # 导致渲染耗时随对话增长失控（翻页/流式卡死的根因）。
        messages = self.messages
        start = 0
        total_chars = 0
        for message in messages:
            total_chars += len(message.text)
        if total_chars > _RENDER_CHAR_BUDGET:
            overflow = total_chars - _RENDER_CHAR_BUDGET
            while start < len(messages) - 8 and overflow > 0:
                overflow -= len(messages[start].text) + 20
                start += 1
        if start:
            fragments.append(("class:muted",
                              f"…… 已省略最早的 {start} 条消息"
                              "（完整记录见会话账本）\n"))
        for offset, message in enumerate(messages[start:], start):
            if offset > start:
                # 活动行与相邻内容紧凑排列，不插空行。
                gap = ("\n" if (message.role == "activity"
                                or messages[offset - 1].role == "activity")
                       else "\n\n")
                fragments.append(("", gap))
            if message.role == "activity":
                fragments.append(("class:activity", message.text))
                if message.details and self.activity_expanded:
                    fragments.append(("", "\n"))
                    for line in message.details.split("\n"):
                        fragments.append(("class:activity-detail",
                                          "    " + line + "\n"))
                continue
            icon, style = {
                "user": ("▶ ", "class:user-heading"),
                "assistant": ("◆ ", "class:assistant-heading"),
                "system": ("ℹ ", "class:system-heading"),
            }.get(message.role, ("• ", "class:muted"))
            fragments.append((style, f"{icon}{message.role.upper()}"))
            if message.streaming:
                fragments.append(("class:streaming", "  ● STREAMING"))
            fragments.append(("", "\n"))
            body_text = message.text or ("..." if message.streaming else "")
            if message.role == "system" or not body_text:
                fragments.append(("", body_text))
                continue
            cached = message.render_cache
            if cached is not None and cached[0] == body_text:
                fragments.extend(cached[1])
            else:
                rendered = _markdown_fragments(body_text)
                message.render_cache = (body_text, rendered)
                fragments.extend(rendered)
        return fragments

    def render_sidebar(self) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []

        def heading(title: str) -> None:
            fragments.append(("class:sidebar-heading", title))
            fragments.append(("", "\n"))

        heading("运行时")
        fragments.append(("class:key", "provider  "))
        fragments.append(("", f"{self.runtime_info.get('provider', '-')}\n"))
        fragments.append(("class:key", "model     "))
        fragments.append(("", f"{self.runtime_info.get('model', '-')}\n"))
        fragments.append(("class:key", "protocol  "))
        fragments.append(("", f"{self.runtime_info.get('protocol', '-')}\n"))
        fragments.append(("class:key", "mode      "))
        fragments.append(("", f"{self.runtime_info.get('permission_mode', '-')}\n"))
        fragments.append(("class:key", "workspace "))
        fragments.append(("", f"{self.runtime_info.get('workspace', '-')}\n"))

        fragments.append(("", "\n"))
        heading("本次会话")
        fragments.append(("class:key", "session   "))
        fragments.append(("", f"{str(self.session_id)[:18]}\n"))
        fragments.append(("class:key", "tools     "))
        fragments.append(("", f"{self.tool_count}\n"))
        fragments.append(("class:key", "累计tokens"))
        fragments.append(("", (
            f"{self.usage['input_tokens']} in / "
            f"{self.usage['output_tokens']} out\n"
        )))
        fragments.append(("class:key", "errors    "))
        fragments.append(("", f"{self.error_count}\n"))
        return fragments


class ConversationControl(FormattedTextControl):
    """对话流控件：渲染前同步视口尺寸，并把游标锚定在可视区内。

    Window._scroll 在 create_content 之后运行，因此这里先记录
    当前渲染的真实行数；游标固定在视口顶部（或贴底模式的最后一行），
    避免内置的“游标必须可见”逻辑把滚动位置拽回底部。
    """

    def __init__(self, state: TuiState, viewport: ViewportState) -> None:
        super().__init__(
            state.render_conversation,
            show_cursor=False,
            get_cursor_position=self._cursor_position,
        )
        self.viewport = viewport

    def create_content(self, width: int, height: int | None) -> Any:
        content = super().create_content(width, height)
        # 布局阶段 height 为 None，此时不能覆盖真实渲染尺寸。
        if height is None:
            return content
        self.viewport.content_height = content.line_count
        self.viewport.window_height = int(height)
        # super() 计算游标时用的还是上一帧行数；这里用本帧真实行数
        # 重建 UIContent，保证“游标锚点”驱动的滚动不滞后。
        corrected = Point(0, self._cursor_line(content.line_count))
        if content.cursor_position != corrected:
            return UIContent(
                get_line=content.get_line,
                line_count=content.line_count,
                show_cursor=False,
                cursor_position=corrected,
            )
        return content

    def _cursor_line(self, line_count: int) -> int:
        viewport = self.viewport
        if viewport.follow_bottom:
            return max(0, line_count - 1)
        return min(viewport.clamp(), max(0, line_count - 1))

    def _cursor_position(self) -> Point:
        return Point(0, self._cursor_line(self.viewport.content_height))


class ViewportWindow(Window):
    """对话窗体：滚轮事件接入显式视口，而不是依赖 Window 内部滚动。"""

    def __init__(self, viewport: ViewportState, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.viewport = viewport

    def _scroll(self, ui_content: Any, width: int, height: int) -> None:
        # wrap 模式下 Window 的 vertical_scroll 只增不减（粘性贴底）；
        # 手动阅读时每帧先把滚动复位到锚点，游标可见性逻辑会保持该位置。
        if not self.viewport.follow_bottom:
            self.vertical_scroll = self.viewport.clamp()
        super()._scroll(ui_content, width, height)

    def _mouse_handler(self, mouse_event: MouseEvent) -> Any:
        event_type = mouse_event.event_type
        if event_type == MouseEventType.SCROLL_UP:
            self.viewport.scroll_up(3)
            return None
        if event_type == MouseEventType.SCROLL_DOWN:
            self.viewport.scroll_down(3)
            return None
        return super()._mouse_handler(mouse_event)


class ScrollbarControl(UIControl):
    """滚动条内容控件。

    显示位置以对话窗体上一帧（本帧已更新）的 render_info.vertical_scroll
    为准：wrap_lines 模式下 Window 的滚动由游标锚点驱动，视口的
    scroll_offset 只是输入值，真实位置必须从渲染结果读回。
    每次调用都重新计算，不能缓存。
    """

    def __init__(self, viewport: ViewportState, body_window: Any) -> None:
        self.viewport = viewport
        self.body_window = body_window

    def create_content(self, width: int, height: int | None) -> UIContent:
        lines = list(split_lines(self._fragments()))
        return UIContent(
            get_line=lambda index: lines[index],
            line_count=len(lines),
            show_cursor=False,
            cursor_position=Point(0, 0),
        )

    def preferred_height(self, width: int, max_available_height: int,
                         wrap_lines: bool = False,
                         get_line_prefix: Any = None) -> int:
        return 3

    def _sync_from_body(self) -> tuple[int, int] | None:
        info = self.body_window.render_info
        if info is None:
            return None
        viewport = self.viewport
        offset = max(0, int(info.vertical_scroll))
        content = max(0, int(info.content_height))
        displayed = getattr(info, "displayed_lines", None)
        viewport.content_height = content
        viewport.window_height = max(1, int(info.window_height))
        viewport.page_lines = max(1, len(displayed)) if displayed else viewport.window_height
        viewport.scroll_offset = offset
        # 最后一行可见即视为在底部：滚轮/翻页滚到底、内容缩短时恢复跟随。
        if info.bottom_visible:
            viewport.follow_bottom = True
        return offset, content

    def _fragments(self) -> list[tuple[str, str]]:
        state = self._sync_from_body()
        if state is None:
            return []
        viewport = self.viewport
        offset, content = state
        if content <= 1 or content <= viewport.window_height:
            return []
        fragments: list[tuple[str, str]] = [("class:scrollbar-arrow", "▲\n")]
        track = max(1, viewport.window_height - 2)
        thumb = max(1, min(
            track,
            round(track * viewport.window_height / content),
        ))
        max_anchor = max(1, content - 1)
        # 跟随模式必然贴底，滑块直接钉在轨道末端，避免逻辑行/显示行
        # 单位差异导致贴底时滑块停在中途。
        ratio = 1.0 if viewport.follow_bottom else min(1.0, offset / max_anchor)
        thumb_top = round(ratio * (track - thumb))
        for row in range(track):
            if thumb_top <= row < thumb_top + thumb:
                fragments.append(("class:scrollbar-thumb", "█\n"))
            else:
                fragments.append(("class:scrollbar-track", "│\n"))
        fragments.append(("class:scrollbar-arrow", "▼"))
        return fragments


class ScrollbarWindow(Window):
    """对话流右侧的一列滚动条：显示位置，支持滚轮、点击跳转和拖动。"""

    def __init__(self, viewport: ViewportState, body_window: Any,
                 **kwargs: Any) -> None:
        super().__init__(ScrollbarControl(viewport, body_window), width=1, **kwargs)
        self.viewport = viewport

    def _mouse_handler(self, mouse_event: MouseEvent) -> Any:
        event_type = mouse_event.event_type
        viewport = self.viewport
        if event_type == MouseEventType.SCROLL_UP:
            viewport.scroll_up(3)
            return None
        if event_type == MouseEventType.SCROLL_DOWN:
            viewport.scroll_down(3)
            return None
        if event_type not in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE):
            return NotImplemented
        info = self.render_info
        if info is None:
            return None
        if viewport.content_height <= 1:
            return None
        height = int(info.window_height)
        y = mouse_event.position.y - int(getattr(info, "_y_offset", 0))
        if y <= 0:
            viewport.scroll_up(3)
        elif y >= height - 1:
            viewport.scroll_down(3)
        else:
            track = max(1, height - 2)
            fraction = min(1.0, max(0.0, (y - 1) / track))
            viewport.follow_bottom = False
            viewport.scroll_offset = round(fraction * viewport.last_anchor)
            # 拖到最底部等同重新跟随最新消息。
            if viewport.scroll_offset >= viewport.last_anchor:
                viewport.follow_bottom = True
        return None


def _short_tokens(count: int) -> str:
    if count >= 1_000_000:
        value = count / 1_000_000
        return f"{value:.1f}M" if value < 10 else f"{value:.0f}M"
    return f"{round(count / 1000):.0f}K"


class ContiTui:
    """独立的 prompt_toolkit 全屏界面，不依赖任何原项目视觉。"""

    def __init__(self, runtime: Any, *, output: Any = None, input: Any = None) -> None:
        self.runtime = runtime
        self.output = output
        self.input = input
        self.state = TuiState(runtime.describe())
        self.viewport = ViewportState()
        self.sidebar_visible = True
        self._pending_invalidate = False
        self._request_input_future: asyncio.Future | None = None
        self._request_input_options: list[str] | None = None
        self._request_input_selected = 0
        self._request_input_message: ChatMessage | None = None
        self.current_task: asyncio.Task | None = None
        self._auto_turns = 0
        self.command_registry = runtime.commands
        self.command_completer = CommandCompleter(self)
        self.conversation_control = ConversationControl(
            self.state, self.viewport
        )
        self.sidebar_control = FormattedTextControl(
            self.state.render_sidebar,
            show_cursor=False,
        )
        self.input_control = self._make_input()
        self.command_context = self._command_context()
        self.layout = self._build_layout()
        self.application = self._build_application()
        self.runtime.async_input_handler = self._answer_request_input
        if self.output is None and sys.platform == "win32":
            # 真实终端：启用 VT 并补上光标形状支持（闪烁竖线）。
            enable_console_vt()
            self.application.output = BlinkingCursorOutput(self.application.output)

    def _command_context(self) -> CommandContext:
        session_id = None if self.state.session_id == "新会话" else self.state.session_id
        return CommandContext(
            self.runtime,
            session_id=str(session_id) if session_id is not None else None,
            compact_session=self._compact_session,
            undo_checkpoint=getattr(self.runtime, "undo_last", None),
            activity_provider=lambda: [m.text for m in self.state.messages
                                       if m.role == "activity"][-20:],
        )

    async def _interrupt_current(self) -> None:
        """Ctrl+C / Esc：中断当前任务或跳过 pending 的提问。"""
        future = self._request_input_future
        if future is not None and not future.done():
            future.set_result("（用户按 Esc 跳过了这个问题）")
            return
        if self.current_task and not self.current_task.done():
            self.state.busy = False
            self.state.status = "正在取消当前任务"
            self.current_task.cancel()
            self.state.add_system("已发送取消请求。")
        else:
            self.state.status = "没有正在运行的任务"
        self.invalidate()

    def _waiting_with_options(self) -> bool:
        return (self._request_input_future is not None
                and not self._request_input_future.done()
                and bool(self._request_input_options))

    def _submit_input(self) -> None:
        """统一提交：等待提问时解析回答（编号/自定义/选中项），否则发送任务。"""
        future = self._request_input_future
        options = self._request_input_options or []
        if future is not None and not future.done():
            text = self.input_control.text.strip()
            self.input_control.text = ""
            if text.isdigit() and 1 <= int(text) <= len(options):
                future.set_result(options[int(text) - 1])
            elif text:
                future.set_result(text)
            elif options:
                future.set_result(options[self._request_input_selected])
            return
        prompt = self.input_control.text.strip()
        if not prompt:
            return
        self.input_control.text = ""
        asyncio.ensure_future(self.handle_prompt(prompt))

    def _make_input(self) -> Any:
        kb = KeyBindings()
        self.key_bindings = kb

        @kb.add("enter", eager=True)
        async def _send(event: Any) -> None:
            # 输入框未聚焦时的兜底提交；聚焦时由控件级绑定接管。
            self._submit_input()

        @kb.add("c-c")
        async def _cancel(event: Any) -> None:
            await self._interrupt_current()

        @kb.add("escape", eager=True)
        async def _escape(event: Any) -> None:
            await self._interrupt_current()

        @kb.add("c-q")
        async def _quit(event: Any) -> None:
            if self.current_task and not self.current_task.done():
                self.current_task.cancel()
            self.application.exit(result="exit")

        @kb.add("c-b")
        async def _toggle_panel(event: Any) -> None:
            self._toggle_panel()

        @kb.add("c-o", eager=True)
        async def _toggle_activity_details(event: Any) -> None:
            self.state.activity_expanded = not self.state.activity_expanded
            self.invalidate()

        @kb.add("pageup", eager=True)
        async def _page_up(event: Any) -> None:
            self.viewport.page_up()

        @kb.add("pagedown", eager=True)
        async def _page_down(event: Any) -> None:
            self.viewport.page_down()

        @kb.add("c-up", eager=True)
        async def _line_up(event: Any) -> None:
            self.viewport.scroll_up(2)

        @kb.add("c-down", eager=True)
        async def _line_down(event: Any) -> None:
            self.viewport.scroll_down(2)

        input_empty = Condition(lambda: not self.input_control.text)

        @kb.add("home", filter=input_empty, eager=True)
        async def _to_top(event: Any) -> None:
            self.viewport.scroll_to_top()

        @kb.add("end", filter=input_empty, eager=True)
        async def _to_bottom(event: Any) -> None:
            self.viewport.scroll_to_bottom()

        input_kb = KeyBindings()
        # 控件级绑定优先级最高：multiline 下 Enter 一定发送，Ctrl+J 换行。
        @input_kb.add("enter", eager=True)
        async def _send_from_input(event: Any) -> None:
            self._submit_input()

        @input_kb.add("c-j")
        async def _insert_newline(event: Any) -> None:
            self.input_control.buffer.insert_text("\n")

        waiting_options = Condition(self._waiting_with_options)

        @input_kb.add("up", filter=waiting_options, eager=True)
        async def _option_up(event: Any) -> None:
            self._move_request_selection(-1)

        @input_kb.add("down", filter=waiting_options, eager=True)
        async def _option_down(event: Any) -> None:
            self._move_request_selection(1)

        input_area = TextArea(
            multiline=True,
            wrap_lines=True,
            completer=self.command_completer,
            complete_while_typing=True,
            focus_on_click=True,
        )
        input_area.control.key_bindings = input_kb
        return input_area

    def _conversation(self) -> Any:
        body = ViewportWindow(
            self.viewport,
            self.conversation_control,
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=self._conversation_scroll,
        )
        return VSplit([
            Window(width=1, char=" "),
            body,
            ScrollbarWindow(self.viewport, body),
            Window(width=1, char=" "),
        ])

    def _conversation_scroll(self, window: Any) -> int:
        """非折行模式的滚动回退；wrap 模式由 ConversationControl 游标锚点驱动。"""
        viewport = self.viewport
        info = window.render_info
        max_scroll = max(0, int(info.content_height) - int(info.window_height))
        if viewport.follow_bottom:
            viewport.scroll_offset = max_scroll
            return viewport.scroll_offset
        return max(0, min(viewport.scroll_offset, max_scroll))

    def _sidebar(self) -> Any:
        content = Window(
            self.sidebar_control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        return VSplit([
            Window(width=1, char="▏", style="class:separator"),
            ScrollablePane(content, display_arrows=False),
        ], width=37)

    def _input_meta_fragments(self) -> list[tuple[str, str]]:
        info = self.state.runtime_info
        tokens, window, percent = self.runtime.context_usage()
        usage_style = "class:status-busy" if percent >= 85 else "class:status-key"
        return [
            ("class:status-key", f" {info.get('model', '-')} "),
            ("class:status-sep", "│"),
            ("class:muted", " Enter 发送 · Ctrl+J 换行 "),
            ("class:status-sep", "│"),
            (usage_style,
             f" 上下文 {_short_tokens(tokens)}/{_short_tokens(window)}"
             f" · {percent}% "),
        ]

    def _input_area(self) -> Any:
        return VSplit([
            Window(width=1, char=" "),
            HSplit([
                Window(
                    FormattedTextControl(self._input_meta_fragments),
                    height=1,
                    style="class:input-meta",
                ),
                self.input_control,
            ]),
        ], height=4, style="class:input-area")

    def _build_layout(self) -> Any:
        conversation = self._conversation()
        left = HSplit([
                conversation,
                self._input_area(),
            ], height=Dimension(weight=1))
        children: list[Any] = [left]
        if self.sidebar_visible:
            children.append(self._sidebar())
        return VSplit(children)

    def _header_fragments(self) -> list[tuple[str, str]]:
        info = self.state.runtime_info
        return [
            ("class:logo", " CONTI-AGENT "),
        ]

    def _model_status_fragments(self) -> list[tuple[str, str]]:
        style = "class:status-busy" if self.state.busy else "class:status-idle"
        return [(style, f" {self.state.status} ")]

    def _shortcut_fragments(self) -> list[tuple[str, str]]:
        return [
            ("class:status-key", " Enter 发送 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+C 取消 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+B 面板 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+Q 退出 "),
            ("class:status-sep", "│"),
            ("class:status-key", " PageUp/PageDown 翻页 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+O 详情 "),
        ]

    def _status_fragments(self) -> list[tuple[str, str]]:
        return self._model_status_fragments()

    def _screen_too_small(self) -> bool:
        try:
            size = self.application.output.get_size()
        except Exception:
            return False
        return size.columns < 40 or size.rows < 8

    def _too_small_fragments(self) -> list[tuple[str, str]]:
        return [
            ("class:system-heading", "终端窗口太小\n\n"),
            ("", "请把终端窗口放大到至少 40 列 × 8 行，再使用对话界面。\n"),
        ]

    def _root_layout(self) -> Any:
        too_small = Condition(self._screen_too_small)
        header = Window(
            FormattedTextControl(self._header_fragments, show_cursor=False),
            height=1,
            style="class:header",
        )
        footer = VSplit([
            Window(FormattedTextControl(self._model_status_fragments)),
            Window(
                FormattedTextControl(self._shortcut_fragments),
                align=WindowAlign.RIGHT,
            ),
        ], height=1)
        hint = Window(
            FormattedTextControl(self._too_small_fragments, show_cursor=False),
            style="class:muted",
        )
        # 补全菜单必须挂在 FloatContainer 上，否则候选永远不会显示。
        return FloatContainer(
            HSplit([
                ConditionalContainer(header, filter=~too_small),
                ConditionalContainer(self.layout, filter=~too_small),
                ConditionalContainer(footer, filter=~too_small),
                ConditionalContainer(hint, filter=too_small),
            ]),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(
                        max_height=6,
                        scroll_offset=4,
                    ),
                ),
            ],
        )

    def _rebuild_layout(self) -> None:
        self.layout = self._build_layout()
        self.application.layout = Layout(
            self._root_layout(),
            focused_element=self.input_control,
        )

    def _toggle_panel(self) -> None:
        self.sidebar_visible = not self.sidebar_visible
        self._rebuild_layout()

    def _build_application(self) -> Application:
        layout = Layout(self._root_layout(), focused_element=self.input_control)
        return Application(
            layout=layout,
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=True,
            cursor=SimpleCursorShapeConfig(CursorShape.BLINKING_BEAM),
            style=self._style(),
            output=self.output,
            input=self.input,
        )

    def _style(self) -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({
            "header": "bg:#101820 #dbe7ee",
            "logo": "bg:#0ea5e9 #04121d bold",
            "status-idle": "#7ee787 bold",
            "status-busy": "#ffbd2e bold",
            "user-heading": "#7dd3fc bold",
            "assistant-heading": "#c792ea bold",
            "system-heading": "#7ee787 bold",
            "streaming": "#ffbd2e",
            "muted": "#5c6c7c",
            "separator": "#22303c",
            "input-area": "bg:#0d151d",
            "input-accent": "#0ea5e9",
            "sidebar-heading": "#0ea5e9 bold",
            "key": "#8fa7b7",
            "activity": "#a5b6c3",
            "status-key": "#89ddff",
            "status-sep": "#33414d",
            "scrollbar-arrow": "#33414d",
            "scrollbar-thumb": "#0ea5e9",
            "scrollbar-track": "#1c2733",
            "activity-detail": "#71808f",
            "md-heading": "#7dd3fc bold",
            "md-bold": "#eef5fa bold",
            "md-italic": "italic #b9c8d4",
            "md-code": "#7ee787",
            "md-codeblock": "#9fb4c4",
            "md-bullet": "#0ea5e9 bold",
            "md-quote": "italic #8fa7b7",
            "md-rule": "#33414d",
            "completion-menu": "bg:#101820 #dbe7ee",
            "completion-menu.completion": "bg:#101820 #dbe7ee",
            "completion-menu.completion.current": "bg:#0ea5e9 #04121d bold",
            "completion-menu.meta.completion": "bg:#101820 #5c6c7c",
            "completion-menu.meta.completion.current": "bg:#0ea5e9 #04121d",
        })

    def invalidate(self) -> None:
        self.application.invalidate()

    def invalidate_soon(self) -> None:
        """流式输出的节流刷新：约 12fps，避免每条 delta 触发全量渲染。"""
        if self._pending_invalidate:
            return
        self._pending_invalidate = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._pending_invalidate = False
            self.application.invalidate()
            return
        loop.call_later(0.08, self._flush_invalidate)

    def _flush_invalidate(self) -> None:
        self._pending_invalidate = False
        self.application.invalidate()

    async def handle_prompt(self, prompt: str) -> None:
        # 模型正在通过 request_input 等待澄清答案：本次输入就是回答。
        future = self._request_input_future
        if future is not None and not future.done():
            options = self._request_input_options or []
            if prompt.isdigit() and 1 <= int(prompt) <= len(options):
                future.set_result(options[int(prompt) - 1])
            else:
                future.set_result(prompt)
            return
        if prompt.startswith("/"):
            await self.handle_command(prompt)
            self.invalidate()
            return
        if self.state.busy:
            self.state.status = "当前任务仍在运行，请等待或 Ctrl+C 取消"
            self.invalidate()
            return
        # 压缩锁：压缩进行中拦截新输入，防止压缩标记吞掉摘要期间
        # 新写入的对话（HIGHLIGHTS 1.3.C）。
        if getattr(self.runtime, "compacting", False):
            self.state.status = "正在压缩上下文，请稍候再发送"
            self.invalidate()
            return
        await self.run_prompt(prompt)

    async def _answer_request_input(self, question: str,
                                    options: list[str] | None = None) -> str:
        """request_input 的 TUI 实现：问题与选项进对话流，↑↓ 选择或
        直接输入自定义回答，全程不阻塞事件循环。"""
        self.state.add_system(f"❓ {question}")
        self._request_input_options = list(options) if options else None
        self._request_input_selected = 0
        if self._request_input_options:
            self._request_input_message = self.state.append_tool_activity(
                self._options_text()
            )
        self.state.status = "等待你回答上面的提问"
        self.viewport.scroll_to_bottom()
        self.invalidate()
        loop = asyncio.get_running_loop()
        self._request_input_future = loop.create_future()
        try:
            answer = await self._request_input_future
        except asyncio.CancelledError:
            answer = "（用户取消了本次任务）"
        finally:
            self._request_input_future = None
            self._request_input_options = None
            self._request_input_message = None
        self.state.append_tool_activity(f"✓ 你的回答：{answer}")
        return answer

    def _options_text(self) -> str:
        lines = []
        for index, option in enumerate(self._request_input_options or []):
            marker = "❯" if index == self._request_input_selected else " "
            lines.append(f"{marker} {index + 1}) {option}")
        lines.append("  ↑↓ 选择 · Enter 确认 · 也可直接输入自定义回答")
        return "\n".join(lines)

    def _move_request_selection(self, delta: int) -> None:
        options = self._request_input_options or []
        if not options or self._request_input_future is None:
            return
        count = len(options)
        self._request_input_selected = (
            (self._request_input_selected + delta) % count
        )
        if self._request_input_message is not None:
            self._request_input_message.text = self._options_text()
        self.invalidate()

    async def handle_command(self, prompt: str) -> None:
        context = self._command_context()
        result = await self.command_registry.execute(prompt, context)

        if result.new_session_requested:
            self.state.session_id = "新会话"
            self.state.usage = {"input_tokens": 0, "output_tokens": 0}
            self.state.tool_count = 0
            self.state.messages.clear()
        elif result.session_id is not None:
            self.state.session_id = result.session_id

        if result.panel_action == "toggle":
            self._toggle_panel()

        if result.clear_requested:
            self.state.messages.clear()

        if result.data.get("history") is not None:
            self._backfill_history(result.data["history"])

        # 模型或 /status 执行后刷新界面缓存。
        self.state.runtime_info = self.runtime.describe()
        self.state.status = result.output[0] if result.output else result.status

        # clear 的输出要在清理后再显示；其余命令直接显示。
        # 多行输出合并为一条系统消息，避免每行都带 SYSTEM 标题。
        if result.output:
            self.state.add_system("\n".join(result.output))

        # 命令输出要求可见，恢复跟随底部。
        self.viewport.scroll_to_bottom()

        if result.exit_requested:
            self.application.exit(result="exit")

    def _backfill_history(self, history: list[dict[str, Any]]) -> None:
        """把磁盘会话消息回填到对话流；工具消息只进入活动栏。"""
        self.state.messages.clear()
        for item in history:
            role = item.get("role")
            content = item.get("content") or ""
            if role in ("user", "assistant"):
                if content:
                    self.state.append_message(str(role), str(content))
            elif role == "tool":
                if content:
                    self.state.append_tool_activity(f"✓ 工具结果：{str(content)[:80]}")
            elif role == "system":
                if content:
                    self.state.add_system(str(content))

    async def _compact_session(self, session_id: str) -> str:
        from .cli import compact_session
        return await compact_session(self.runtime, session_id)

    async def run_prompt(self, prompt: str | None = None) -> None:
        # prompt=None 为团队自动续回合：leader 只消费步边界注入的
        # 团队收件箱（交付/消息/报告），对话流不出现用户消息。
        if prompt is not None:
            self.state.append_message("user", prompt)
            self._auto_turns = 0  # 用户接管，自动回应计数重置
        # 不再预建占位消息：assistant 消息在实际开始输出时创建
        # （stream_delta），保证与工具活动行保持真实时序。
        self.viewport.scroll_to_bottom()
        self.state.busy = True
        self.state.status = ("团队成员有新交付，正在回应…"
                             if prompt is None else "AI 正在处理")
        self.invalidate()
        self.current_task = asyncio.create_task(self._ask_runtime(prompt))
        try:
            final_text, session_id, _ = await self.current_task
        except asyncio.CancelledError:
            self.state.finish_stream("")
            self.state.interrupt_activities()
            self.state.add_system("任务已中断，未完成的工具调用已标记为失败。")
            self.state.status = "已中断"
        except Exception as exc:
            self.state.finish_stream("")
            self.state.add_system(f"任务失败：{type(exc).__name__}: {exc}")
            self.state.status = "任务失败"
            self.state.error_count += 1
        else:
            self.state.finish_stream(final_text)
            self.state.session_id = session_id
            self.state.status = "任务完成"
        finally:
            self.state.busy = False
            self.current_task = None
            self.invalidate()
        if prompt is None:
            self._auto_turns += 1
        # 自动续回合：leader 收件箱还有交付/消息，或最终报告未送达。
        # 上限防失控（leader 判断失败时不会无限循环）；用户输入随时
        # 可以接管（Esc 中断本回合后正常输入）。
        needs_more = False
        try:
            needs_more = bool(self.runtime.team_needs_leader())
        except AttributeError:
            pass
        if needs_more and self._auto_turns < self.MAX_AUTO_TURNS:
            await self.run_prompt(None)
        elif needs_more:
            self.state.add_system(
                f"团队事件较多（自动回应已达 {self.MAX_AUTO_TURNS} 轮上限），"
                "发送任意消息即可继续接管。")

    MAX_AUTO_TURNS = 30

    async def _ask_runtime(self, prompt: str | None) -> tuple[str, str, Any]:
        return await self.runtime.ask(
            prompt,
            session_id=None if self.state.session_id == "新会话" else str(self.state.session_id),
            output_format="text",
            text_callback=self._on_text_delta,
            event_callback=self._on_event,
        )

    def _on_text_delta(self, text: str) -> None:
        self.state.stream_delta(text)
        self.invalidate_soon()

    def _on_event(self, event: Any) -> None:
        self.state.record_event(event)
        self.invalidate_soon()

    async def run_async(self) -> str:
        # auto dream（可选开启）：启动时后台补跑，提炼结果下个会话生效。
        if getattr(getattr(self.runtime, "config", None), "dream_enabled", False):
            asyncio.create_task(self._run_dream_background())
        # 团队被动通知：leader 空闲时成员交付/消息立即显示为活动行，
        # 不打断输入、不启动新回合（用户仍是 leader 的唯一唤醒者）。
        if hasattr(self.runtime, "on_team_notice"):
            self.runtime.on_team_notice = self._on_team_notice
        try:
            return await self.application.run_async()
        finally:
            if self.output is None:
                reset_cursor_shape()

    def _on_team_notice(self, text: str) -> None:
        self.state.append_tool_activity(f"👥 {text}")
        self.viewport.scroll_to_bottom()
        self.invalidate()
        # leader 空闲 → 自动唤醒回应交付/消息；busy 时本轮 run_prompt
        # 尾部的续杯检查会接手。上限内才自动（防失控循环）。
        if not self.state.busy and self._auto_turns < self.MAX_AUTO_TURNS:
            asyncio.create_task(self._auto_follow_up())

    async def _auto_follow_up(self) -> None:
        try:
            if self.state.busy or not self.runtime.team_needs_leader():
                return
        except AttributeError:
            return
        await self.run_prompt(None)

    async def _run_dream_background(self) -> None:
        try:
            processed = await self.runtime.run_dream()
            if processed:
                self.state.add_system(
                    f"auto dream 已完成：提炼了 {processed} 个会话的长期记忆"
                )
                self.invalidate()
        except Exception:
            pass  # dream 是旁路功能，失败不影响正常使用


class CommandCompleter(Completer):
    """为 Slash 命令和参数提供候选。"""

    def __init__(self, interface: ContiTui) -> None:
        self.interface = interface

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        context = self.interface._command_context()
        suggestions = self.interface.command_registry.suggest(text, context)
        parts = text[1:].split(maxsplit=1)
        for item in suggestions:
            value = item.value
            if value.startswith("/"):
                # 命令候选自带斜杠：从输入起点整体替换，避免出现 "//"。
                start = -len(text)
            elif not parts or parts[-1] == "":
                start = 0
            else:
                start = -len(parts[-1])
            yield Completion(
                value,
                start_position=start,
                display=value,
                display_meta=item.description,
            )


def show_startup_logo() -> None:
    """在进入全屏前展示独立设计的 ASCII 启动画面。"""
    print("\033[2J\033[H", end="")
    print("\033[1;96m" + STARTUP_LOGO + "\033[0m")
    print("\033[90m  independent runtime · local workspace · explicit safety\033[0m")
    print("\033[90m  正在初始化界面...\033[0m", flush=True)


class BlinkingCursorOutput:
    """pt 的 Windows 输出类不实现 set_cursor_shape（空操作），
    导致光标闪烁/形状设置完全无效。代理一层，补上 DECSCUSR；
    每次重新显示光标（打字、重绘）后重申形状，避免被终端默认值覆盖。"""

    _SEQUENCES = {
        CursorShape.BLINKING_BEAM: "\x1b[5 q",
        CursorShape.BEAM: "\x1b[4 q",
        CursorShape.BLINKING_BLOCK: "\x1b[1 q",
        CursorShape.BLOCK: "\x1b[2 q",
        CursorShape.BLINKING_UNDERLINE: "\x1b[3 q",
        CursorShape.UNDERLINE: "\x1b[6 q",
    }

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def _write(self, data: str) -> None:
        try:
            sys.stdout.write(data)
            sys.stdout.flush()
        except Exception:
            pass

    def set_cursor_shape(self, shape: Any) -> None:
        self._write(self._SEQUENCES.get(shape, ""))

    def reset_cursor_shape(self) -> None:
        self._write("\x1b[0 q")

    def show_cursor(self) -> None:
        self._wrapped.show_cursor()
        self._write(self._SEQUENCES.get(CursorShape.BLINKING_BEAM, ""))


def enable_console_vt() -> None:
    """持久启用虚拟终端处理，让 DECSCUSR 序列在 flush 之外也生效。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if not mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def reset_cursor_shape() -> None:
    try:
        if sys.platform == "win32":
            sys.stdout.write("\x1b[0 q")
            sys.stdout.flush()
    except Exception:
        pass
    time.sleep(0.8)
