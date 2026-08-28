from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout import (
    Dimension,
    FormattedTextControl,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
)
from prompt_toolkit.data_structures import Point
from prompt_toolkit.widgets import Frame, TextArea
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
        self.cursor_row = 0
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
        # ScrollablePane 通过 cursor position 跟随最新消息；
        # show_cursor=False 只是不绘制光标，不影响滚动定位。
        self.cursor_row = max(0, sum(
            fragment[1].count("\n") for fragment in fragments
        ))
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


class ContiTui:
    """独立的 prompt_toolkit 全屏界面，不依赖任何原项目视觉。"""

    def __init__(self, runtime: Any, *, output: Any = None, input: Any = None) -> None:
        self.runtime = runtime
        self.output = output
        self.input = input
        self.state = TuiState(runtime.describe())
        self.sidebar_visible = False
        self.current_task: asyncio.Task | None = None
        self.command_registry = runtime.commands
        self.command_completer = CommandCompleter(self)
        self.conversation_control = FormattedTextControl(
            self.state.render_conversation,
            show_cursor=False,
            get_cursor_position=self._conversation_cursor,
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

        return TextArea(
            multiline=False,
            wrap_lines=True,
            completer=self.command_completer,
            complete_while_typing=True,
            focus_on_click=True,
        )

    def _conversation(self) -> Any:
        control = Window(
            self.conversation_control,
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=self._conversation_scroll,
    )
        return Frame(
            ScrollablePane(control, display_arrows=True),
            title="对话流",
        )

    def _conversation_scroll(self, window: Any) -> int:
        """对话 pane 始终跟随底部，避免最新回复落在视口外。"""
        info = window.render_info
        return max(0, int(info.content_height) - int(info.window_height))

    def _conversation_cursor(self) -> Any:
        return Point(0, max(0, getattr(self.state, "cursor_row", 0) - 1))

    def _sidebar(self) -> Any:
        control = Window(
            self.sidebar_control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        return Frame(
            ScrollablePane(control, display_arrows=False),
            title="运行状态",
            width=34,
        )

    def _build_layout(self) -> Any:
        conversation = self._conversation()
        input_frame = Frame(self.input_control, title="任务输入 — Enter 发送")
        left = HSplit([
                conversation,
                input_frame,
            ], height=Dimension(weight=1))
        children: list[Any] = [left]
        if self.sidebar_visible:
            children.append(self._sidebar())
        return VSplit(children)

    def _root_layout(self) -> Any:
        header = Window(
            FormattedTextControl(self._header_fragments, show_cursor=False),
            height=1,
            style="class:header",
        )
        model_status = Window(
            FormattedTextControl(self._model_status_fragments, show_cursor=False),
            height=1,
            style="class:footer",
        )
        shortcuts = Window(
            FormattedTextControl(self._shortcut_fragments, show_cursor=False),
            height=1,
            style="class:footer",
        )
        return HSplit([
            header,
            self.layout,
            model_status,
            shortcuts,
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
            ("class:status-key", f" {info.get('permission_mode', '-')} "),
            ("class:status-sep", "│"),
            ("class:status-key", f" {len(info.get('tools', []))} tools "),
            ("class:status-sep", "│"),
            (style, f" {self.state.status} "),
        ]

    def _shortcut_fragments(self) -> list[tuple[str, str]]:
        style = "class:status-busy" if self.state.busy else "class:status-idle"
        return [
            ("class:status-key", " Enter 发送 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+C 取消 "),
            ("class:status-sep", "│"),
            ("class:status-key", " Ctrl+Q 退出 "),
            ("class:status-sep", "│"),
            (style, f" {self.state.status} "),
        ]

    def _status_fragments(self) -> list[tuple[str, str]]:
        return self._model_status_fragments()

    def _build_application(self) -> Application:
        header = Window(
            FormattedTextControl(self._header_fragments, show_cursor=False),
            height=1,
            style="class:header",
        )
        model_status = Window(
            FormattedTextControl(self._model_status_fragments, show_cursor=False),
            height=1,
            style="class:footer",
        )
        shortcuts = Window(
            FormattedTextControl(self._shortcut_fragments, show_cursor=False),
            height=1,
            style="class:footer",
        )
        root = HSplit([header, self.layout, model_status, shortcuts])
        layout = Layout(root, focused_element=self.input_control)
        return Application(
            layout=layout,
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=False,
            style=self._style(),
            output=self.output,
            input=self.input,
        )

    def _style(self) -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({
            "header": "bg:#101820 #dbe7ee",
            "logo": "bg:#0ea5e9 #04121d bold",
            "header-key": "#89ddff bold",
            "header-sep": "#445767",
            "status-idle": "#7ee787 bold",
            "status-busy": "#ffbd2e bold",
            "user-heading": "#7dd3fc bold",
            "assistant-heading": "#c792ea bold",
            "system-heading": "#7ee787 bold",
            "streaming": "#ffbd2e",
            "muted": "#748496",
            "sidebar-heading": "#0ea5e9 bold",
            "key": "#8fa7b7",
            "activity": "#a5b6c3",
            "footer": "bg:#101820 #dbe7ee",
            "status-key": "#89ddff bold",
            "status-sep": "#445767",
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

        # 模型或 /status 执行后刷新界面缓存。
        self.state.runtime_info = self.runtime.describe()
        self.state.status = result.output[0] if result.output else result.status

        # clear 的输出要在清理后再显示；其余命令直接显示。
        for line in result.output:
            self.state.add_system(line)

        if result.exit_requested:
            self.application.exit(result="exit")

    async def _compact_session(self, session_id: str) -> str:
        from .cli import compact_session
        return await compact_session(self.runtime, session_id)

    async def run_prompt(self, prompt: str) -> None:
        self.state.append_message("user", prompt)
        self.state.append_message("assistant", streaming=True)
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
