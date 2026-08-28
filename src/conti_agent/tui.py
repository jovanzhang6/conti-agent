from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
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
  ____  ____  _   _  _____ _    ___
 / ___|/ ___|| \ | ||_   _| |  / _ \
| |    | |   |  \| |  | |  | | | | | |
| |___ | |___| |\  |  | |  | |_| |_| |
 \____| \____|_| \_|  |_|   \___/\___/
"""


@dataclass
class ChatMessage:
    role: str
    text: str
    timestamp: float = field(default_factory=time.time)
    streaming: bool = False


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
        self.activity: list[str] = []
        self.status = "准备就绪"
        self.session_id = "新会话"
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.busy = False
        self.tool_count = 0
        self.error_count = 0
        self.pending_activities: dict[str, tuple[str, dict[str, Any]]] = {}
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

    def append_activity(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.activity.append(f"{stamp} {text}")
        self.activity = self.activity[-100:]

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
            self.append_activity(format_tool_started(tool_name, arguments))
        elif event_type == "tool.completed":
            call_id = str(payload.get("tool_call_id", ""))
            tool_name, arguments = self.pending_activities.pop(
                call_id, (str(payload.get("tool_name", "")), {})
            )
            elapsed = payload.get("metadata", {}).get("elapsed")
            state = "失败" if payload.get("is_error") else "完成"
            if state == "失败":
                summary = format_tool_completed(
                    tool_name, arguments, is_error=True, elapsed=elapsed
                )
            else:
                summary = format_tool_completed(
                    tool_name, arguments, is_error=False, elapsed=elapsed
                )
            self.append_activity(summary)
        elif event_type == "usage.recorded":
            self.usage["input_tokens"] += int(payload.get("input_tokens", 0))
            self.usage["output_tokens"] += int(payload.get("output_tokens", 0))
        elif event_type == "run.retry":
            self.append_activity(f"Provider 重试 {payload.get('attempt')}")
        elif event_type == "run.failed":
            self.error_count += 1
            self.status = "运行失败"
            self.append_activity(f"失败：{payload.get('error')}")

    def render_conversation(self) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        if not self.messages:
            return [("class:muted", "还没有对话。\n输入你的第一个任务。")]
        for index, message in enumerate(self.messages):
            if index:
                fragments.append(("", "\n\n"))
            icon, style = {
                "user": ("▶ ", "class:user-heading"),
                "assistant": ("◆ ", "class:assistant-heading"),
                "system": ("ℹ ", "class:system-heading"),
            }.get(message.role, ("• ", "class:muted"))
            fragments.append((style, f"{icon}{message.role.upper()}"))
            if message.streaming:
                fragments.append(("class:streaming", "  ● STREAMING"))
            fragments.append(("", "\n"))
            fragments.append(("", message.text or ("..." if message.streaming else "")))
            if message.streaming:
                fragments.append(("class:streaming", "▌"))
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
        fragments.append(("class:key", "tokens    "))
        fragments.append(("", (
            f"{self.usage['input_tokens']} in / "
            f"{self.usage['output_tokens']} out\n"
        )))
        fragments.append(("class:key", "errors    "))
        fragments.append(("", f"{self.error_count}\n"))

        fragments.append(("", "\n"))
        heading("活动")
        if not self.activity:
            fragments.append(("class:muted", "暂无工具活动。\n"))
        for item in reversed(self.activity[-14:]):
            fragments.append(("class:activity", item + "\n"))
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
        offset, content = state
        if content <= 1 or content <= self.viewport.window_height:
            return []
        fragments: list[tuple[str, str]] = [("class:scrollbar-arrow", "▲\n")]
        track = max(1, self.viewport.window_height - 2)
        thumb = max(1, min(
            track,
            round(track * self.viewport.window_height / content),
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


class ContiTui:
    """独立的 prompt_toolkit 全屏界面，不依赖任何原项目视觉。"""

    def __init__(self, runtime: Any, *, output: Any = None, input: Any = None) -> None:
        self.runtime = runtime
        self.output = output
        self.input = input
        self.state = TuiState(runtime.describe())
        self.viewport = ViewportState()
        self.sidebar_visible = True
        self.current_task: asyncio.Task | None = None
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

    def _command_context(self) -> CommandContext:
        session_id = None if self.state.session_id == "新会话" else self.state.session_id
        return CommandContext(
            self.runtime,
            session_id=str(session_id) if session_id is not None else None,
            compact_session=self._compact_session,
            activity_provider=lambda: list(self.state.activity),
        )

    def _make_input(self) -> Any:
        kb = KeyBindings()
        self.key_bindings = kb

        @kb.add("enter")
        async def _send(event: Any) -> None:
            prompt = self.input_control.text.strip()
            if not prompt:
                return
            self.input_control.text = ""
            await self.handle_prompt(prompt)

        @kb.add("c-c")
        async def _cancel(event: Any) -> None:
            if self.current_task and not self.current_task.done():
                self.state.busy = False
                self.state.status = "正在取消当前任务"
                self.current_task.cancel()
                self.state.add_system("已发送取消请求。")
            else:
                self.state.status = "没有正在运行的任务"
            self.application.invalidate()

        @kb.add("c-q")
        async def _quit(event: Any) -> None:
            if self.current_task and not self.current_task.done():
                self.current_task.cancel()
            self.application.exit(result="exit")

        @kb.add("c-b")
        async def _toggle_panel(event: Any) -> None:
            self._toggle_panel()

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

        return TextArea(
            multiline=False,
            wrap_lines=True,
            completer=self.command_completer,
            complete_while_typing=True,
            focus_on_click=True,
        )

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

    def _input_area(self) -> Any:
        return HSplit([
            Window(height=1, char="─", style="class:separator"),
            VSplit(
                [Window(width=1, char=" "), self.input_control],
                style="class:input-area",
            ),
        ])

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
        info = self.state.runtime_info
        style = "class:status-busy" if self.state.busy else "class:status-idle"
        return [
            ("class:status-key", f" {info.get('model', '-')} "),
            ("class:status-sep", "│"),
            ("class:status-key", f" {len(info.get('tools', []))} tools "),
            ("class:status-sep", "│"),
            (style, f" {self.state.status} "),
        ]

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
        ])
        hint = Window(
            FormattedTextControl(self._too_small_fragments, show_cursor=False),
            style="class:muted",
        )
        return HSplit([
            ConditionalContainer(header, filter=~too_small),
            ConditionalContainer(self.layout, filter=~too_small),
            ConditionalContainer(footer, filter=~too_small),
            ConditionalContainer(hint, filter=too_small),
        ])

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
            "input-area": "bg:#0b1218",
            "sidebar-heading": "#0ea5e9 bold",
            "key": "#8fa7b7",
            "activity": "#a5b6c3",
            "footer": "bg:#101820 #dbe7ee",
            "status-key": "#89ddff",
            "status-sep": "#33414d",
            "scrollbar-arrow": "#33414d",
            "scrollbar-thumb": "#0ea5e9",
            "scrollbar-track": "#1c2733",
        })

    def invalidate(self) -> None:
        self.application.invalidate()

    async def handle_prompt(self, prompt: str) -> None:
        if prompt.startswith("/"):
            await self.handle_command(prompt)
            self.invalidate()
            return
        if self.state.busy:
            self.state.status = "当前任务仍在运行，请等待或 Ctrl+C 取消"
            self.invalidate()
            return
        await self.run_prompt(prompt)

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
            self.state.activity.clear()

        if result.data.get("history") is not None:
            self._backfill_history(result.data["history"])

        # 模型或 /status 执行后刷新界面缓存。
        self.state.runtime_info = self.runtime.describe()
        self.state.status = result.output[0] if result.output else result.status

        # clear 的输出要在清理后再显示；其余命令直接显示。
        for line in result.output:
            self.state.add_system(line)

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
                    self.state.append_activity(f"工具结果：{str(content)[:80]}")
            elif role == "system":
                if content:
                    self.state.add_system(str(content))

    async def _compact_session(self, session_id: str) -> str:
        from .cli import compact_session
        return await compact_session(self.runtime, session_id)

    async def run_prompt(self, prompt: str) -> None:
        self.state.append_message("user", prompt)
        self.state.append_message("assistant", streaming=True)
        # 用户主动发送消息时回到跟随模式，确保看到最新回复。
        self.viewport.scroll_to_bottom()
        self.state.busy = True
        self.state.status = "AI 正在处理"
        self.invalidate()
        self.current_task = asyncio.create_task(self._ask_runtime(prompt))
        try:
            final_text, session_id, _ = await self.current_task
        except asyncio.CancelledError:
            self.state.finish_stream("")
            self.state.add_system("任务已取消。")
            self.state.status = "已取消"
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

    async def _ask_runtime(self, prompt: str) -> tuple[str, str, Any]:
        return await self.runtime.ask(
            prompt,
            session_id=None if self.state.session_id == "新会话" else str(self.state.session_id),
            output_format="text",
            text_callback=self._on_text_delta,
            event_callback=self._on_event,
        )

    def _on_text_delta(self, text: str) -> None:
        self.state.stream_delta(text)
        self.invalidate()

    def _on_event(self, event: Any) -> None:
        self.state.record_event(event)
        self.invalidate()

    async def run_async(self) -> str:
        return await self.application.run_async()


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
        if len(parts) == 1:
            start = -len(parts[0])
        elif parts[1] == "":
            start = 0
        else:
            start = -len(parts[1])
        for item in suggestions:
            yield Completion(
                item.value,
                start_position=start,
                display=item.value,
                display_meta=item.description,
            )


def show_startup_logo() -> None:
    """在进入全屏前展示独立设计的 ASCII 启动画面。"""
    print("\033[2J\033[H", end="")
    print("\033[1;96m" + STARTUP_LOGO + "\033[0m")
    print("\033[90m  independent runtime · local workspace · explicit safety\033[0m")
    print("\033[90m  正在初始化界面...\033[0m", flush=True)
    time.sleep(0.8)
