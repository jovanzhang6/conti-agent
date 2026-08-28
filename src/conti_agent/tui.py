from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
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


STARTUP_LOGO = r"""
  ____ _____ ____  _____ ____  ___
 / ___|_   _|  _ \| ____|  _ \ / _ \
| |     | | | |_) |  _| | |_) | | | |
| |___  | | |  _ <| |___|  _ <| |_| |
 \____| |_| |_| \_\_____|_| \_\\___/

CONTI-AGENT TUI
Independent Terminal Agent Runtime
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
            self.append_activity(
                f"工具请求 {payload.get('tool_name')}"
            )
        elif event_type == "tool.completed":
            state = "失败" if payload.get("is_error") else "完成"
            self.append_activity(
                f"工具{state} {payload.get('tool_name')}"
            )
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
        self.current_task: asyncio.Task | None = None
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
        self.layout = self._build_layout()
        self.application = self._build_application()

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

        return TextArea(
            multiline=False,
            wrap_lines=True,
            focus_on_click=True,
        )

    def _conversation(self) -> Any:
        control = Window(
            self.conversation_control,
            wrap_lines=True,
            always_hide_cursor=True,
    )
        return Frame(
            ScrollablePane(control, display_arrows=True),
            title="对话流",
        )

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
        sidebar = self._sidebar()
        input_frame = Frame(self.input_control, title="任务输入 — Enter 发送")
        root = VSplit([
            HSplit([
                conversation,
                input_frame,
            ], height=Dimension(weight=1)),
            sidebar,
        ])
        return root

    def _header_fragments(self) -> list[tuple[str, str]]:
        info = self.state.runtime_info
        status_style = "class:status-busy" if self.state.busy else "class:status-idle"
        return [
            ("class:logo", " CONTI-AGENT "),
            ("class:header-key", f" {info.get('model', '-')} "),
            ("class:header-sep", " │ "),
            ("class:header-key", f"{info.get('permission_mode', '-')} "),
            ("class:header-sep", " │ "),
            ("class:header-key", f" {len(info.get('tools', []))} tools "),
            ("class:header-sep", " │ "),
            (status_style, f" {self.state.status} "),
        ]

    def _status_fragments(self) -> list[tuple[str, str]]:
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

    def _build_application(self) -> Application:
        header = Window(
            FormattedTextControl(self._header_fragments, show_cursor=False),
            height=1,
            style="class:header",
        )
        status = Window(
            FormattedTextControl(self._status_fragments, show_cursor=False),
            height=1,
            style="class:footer",
        )
        root = HSplit([header, self.layout, status])
        return Application(
            layout=Layout(root, focused_element=self.input_control),
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
        command, *arguments = prompt.split(maxsplit=1)
        lowered = command.lower()
        if lowered in {"/exit", "/quit"}:
            self.application.exit(result="exit")
            return
        if lowered == "/help":
            self.state.add_system(
                "\n".join([
                    "命令：",
                    "/new — 开启新会话",
                    "/status — 显示运行时状态",
                    "/sessions — 列出会话",
                    "/resume <id> — 恢复会话",
                    "/compact — 压缩当前会话",
                    "/clear — 清除屏幕显示（不清除磁盘会话）",
                    "/exit — 退出",
                ])
            )
            self.state.status = "帮助已显示"
            return
        if lowered == "/clear":
            self.state.messages.clear()
            self.state.activity.clear()
            self.state.add_system("屏幕显示已清除。")
            self.state.status = "显示已清除"
            return
        if lowered == "/new":
            self.state.session_id = "新会话"
            self.state.usage = {"input_tokens": 0, "output_tokens": 0}
            self.state.tool_count = 0
            self.state.messages.clear()
            self.state.add_system("已开启新会话。")
            self.state.status = "新会话"
            return
        if lowered == "/status":
            self.state.add_system("\n".join(
                f"{key}: {value}" for key, value in self.state.runtime_info.items()
            ))
            self.state.status = "状态已刷新"
            return
        if lowered == "/sessions":
            sessions = self.runtime.sessions.list()
            if not sessions:
                self.state.add_system("还没有保存的会话。")
            else:
                lines = [f"{item['session_id']}  {item['title']}" for item in sessions[-20:]]
                self.state.add_system("最近会话：\n" + "\n".join(lines))
            self.state.status = f"共 {len(sessions)} 个会话"
            return
        if lowered == "/resume":
            session_id = arguments[0].strip() if arguments else ""
            if not session_id:
                self.state.add_system("用法：/resume <session-id>")
                return
            self.runtime.sessions.load(session_id)
            self.state.session_id = session_id
            self.state.add_system(f"已恢复会话 {session_id}")
            self.state.status = "已恢复"
            return
        if lowered == "/compact":
            if self.state.session_id in {"", "新会话"}:
                self.state.add_system("当前还没有可压缩的磁盘会话。")
                return
            from .cli import compact_session
            summary = await compact_session(self.runtime, str(self.state.session_id))
            self.state.add_system(f"历史已压缩。摘要字数：{len(summary)}")
            self.state.status = "历史已压缩"
            return
        self.state.add_system(f"未知命令：{command}")

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


def show_startup_logo() -> None:
    """在进入全屏前展示独立设计的 ASCII 启动画面。"""
    print("\033[2J\033[H", end="")
    print("\033[1;96m" + STARTUP_LOGO + "\033[0m")
    print("\033[90m  independent runtime · local workspace · explicit safety\033[0m")
    print("\033[90m  正在初始化界面...\033[0m", flush=True)
    time.sleep(0.8)
