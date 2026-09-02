from __future__ import annotations

import asyncio
import unittest
from typing import Any

from conti_agent.events import event

try:
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from conti_agent.tui import (
        STARTUP_LOGO,
        ContiTui,
        TuiState,
        ViewportState,
    )
except ImportError:  # pragma: no cover - 核心运行时仍可在无 TUI 依赖时使用
    STARTUP_LOGO = None
    ContiTui = None
    TuiState = None
    ViewportState = None
    DummyInput = None
    DummyOutput = None


class FakeSessions:
    def list(self) -> list[dict[str, Any]]:
        return [{"session_id": "abc", "title": "旧任务"}]

    def load(self, session_id: str) -> None:
        self.loaded = session_id


class FakeRuntime:
    def __init__(self) -> None:
        from conti_agent.commands import create_default_registry
        self.commands = create_default_registry()
        self.sessions = FakeSessions()
        self.busy = False
        self.switch_session_ids: list[str | None] = []
        self.history = [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "tool", "content": "工具输出"},
        ]

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "test-provider",
            "model": "test-model",
            "protocol": "openai-compat",
            "permission_mode": self.permission_mode,
            "workspace": "W",
            "tools": ["a", "b"],
        }

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {"name": "active", "model": "m1", "protocol": "fake",
             "active": True, "api_key_ready": True},
            {"name": "other", "model": "m2", "protocol": "fake",
             "active": False, "api_key_ready": True},
        ]

    def active_provider_name(self) -> str:
        return "active"

    def context_usage(self) -> tuple[int, int, int]:
        return 50_000, 1_000_000, 5

    permission_mode = "workspace"

    def get_permission_mode(self) -> str:
        return self.permission_mode

    def set_permission_mode(self, mode: str) -> str:
        from conti_agent.permissions import normalize_mode
        self.permission_mode = normalize_mode(mode)
        return self.permission_mode

    def get_provider_info(self, name: str) -> dict[str, Any]:
        if name not in ("active", "other"):
            raise ValueError("unknown")
        return {"name": name, "model": f"model-{name}", "active": name == "active"}

    def set_active_provider(self, name: str, *, session_id: str | None = None) -> dict:
        if name not in ("active", "other"):
            raise ValueError("unknown")
        created = session_id is None
        self.switch_session_ids.append(session_id)
        return {
            "from_provider": "active",
            "from_model": "m1",
            "to_provider": name,
            "to_model": "m2",
            "session_id": session_id or "anchor-1",
            "created_session": created,
        }

    def load_session_history(self, session_id: str) -> list[dict[str, Any]]:
        return self.history


class FakeRenderInfo:
    def __init__(self, content_height: int, window_height: int) -> None:
        self.content_height = content_height
        self.window_height = window_height


class FakeWindow:
    def __init__(self, content_height: int, window_height: int) -> None:
        self.render_info = FakeRenderInfo(content_height, window_height)


@unittest.skipIf(ContiTui is None, "未安装 prompt-toolkit")
class TuiTestCase(unittest.TestCase):
    def test_startup_logo_is_independent_ascii(self) -> None:
        self.assertIn("____", STARTUP_LOGO)
        # 启动图只保留 CONTI 字样，不附加 AGENT/TUI 标题。
        self.assertNotIn("AGENT", STARTUP_LOGO.upper())
        self.assertNotIn("TUI", STARTUP_LOGO.upper())

    def test_stream_delta_and_finish(self) -> None:
        state = TuiState(FakeRuntime().describe())
        state.stream_delta("你")
        state.stream_delta("好")
        state.finish_stream("你好")
        self.assertEqual(state.messages[-1].role, "assistant")
        self.assertEqual(state.messages[-1].text, "你好")
        self.assertFalse(state.messages[-1].streaming)

    def test_event_counters_and_activity(self) -> None:
        state = TuiState(FakeRuntime().describe())
        state.record_event(event("tool.requested", tool_name="workspace_read",
                                 tool_call_id="c1"))
        state.record_event(event("tool.completed", tool_name="workspace_read",
                                 tool_call_id="c1", is_error=False))
        state.record_event(event("usage.recorded", input_tokens=3,
                                 output_tokens=5))
        self.assertEqual(state.tool_count, 1)
        self.assertEqual(state.usage, {"input_tokens": 3, "output_tokens": 5})
        # 工具活动进入主对话流：请求行被完成行原位更新。
        activity = [m for m in state.messages if m.role == "activity"]
        self.assertEqual(len(activity), 1)
        self.assertTrue(activity[0].text.startswith("✓"))
        self.assertEqual([m.text for m in state.messages[-1:]],
                         [activity[0].text])

    def test_commands_and_renderers(self) -> None:
        interface = ContiTui(
            FakeRuntime(), output=DummyOutput(), input=DummyInput()
        )
        asyncio_run(interface.handle_command("/help"))
        self.assertTrue(any("命令：" in item.text for item in interface.state.messages))
        asyncio_run(interface.handle_command("/sessions"))
        self.assertTrue(any("旧任务" in item.text for item in interface.state.messages))
        self.assertTrue(interface.state.render_conversation())
        self.assertTrue(interface.state.render_sidebar())
        self.assertEqual(type(interface.application).__name__, "Application")

    def test_viewport_starts_following_bottom(self) -> None:
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        self.assertTrue(interface.viewport.follow_bottom)
        self.assertEqual(interface.viewport.scroll_offset, 0)

    def test_viewport_manual_scroll_transitions(self) -> None:
        viewport = ViewportState()
        viewport.content_height = 100
        viewport.window_height = 10
        viewport.page_lines = 10
        viewport.scroll_offset = 99
        viewport.scroll_up(5)
        self.assertFalse(viewport.follow_bottom)
        self.assertEqual(viewport.scroll_offset, 94)
        viewport.scroll_down(3)
        self.assertEqual(viewport.scroll_offset, 97)
        self.assertFalse(viewport.follow_bottom)
        # 滚到底部后 follow_bottom 由渲染反馈（最后一行可见）恢复。
        viewport.scroll_down(100)
        self.assertEqual(viewport.scroll_offset, 99)
        self.assertFalse(viewport.follow_bottom)
        viewport.scroll_to_top()
        self.assertFalse(viewport.follow_bottom)
        self.assertEqual(viewport.scroll_offset, 0)
        viewport.scroll_to_bottom()
        self.assertTrue(viewport.follow_bottom)

    def test_scrollbar_sync_restores_follow_at_bottom(self) -> None:
        from conti_agent.tui import ScrollbarControl

        viewport = ViewportState()
        viewport.follow_bottom = False
        viewport.scroll_offset = 20

        class Info:
            vertical_scroll = 90
            content_height = 100
            window_height = 10
            displayed_lines = list(range(90, 100))
            bottom_visible = True

        class Body:
            render_info = Info()

        control = ScrollbarControl(viewport, Body())
        control._sync_from_body()
        self.assertTrue(viewport.follow_bottom)
        self.assertEqual(viewport.scroll_offset, 90)
        self.assertEqual(viewport.content_height, 100)

    def test_conversation_scroll_follows_and_clamps(self) -> None:
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        viewport = interface.viewport
        viewport.content_height = 100
        viewport.window_height = 10
        self.assertEqual(interface._conversation_scroll(FakeWindow(100, 10)), 90)
        viewport.scroll_to_top()
        self.assertEqual(interface._conversation_scroll(FakeWindow(100, 10)), 0)
        viewport.scroll_offset = 500
        self.assertEqual(interface._conversation_scroll(FakeWindow(100, 10)), 90)
        # 内容不足一屏时没有可滚动空间。
        viewport.content_height = 8
        viewport.window_height = 10
        self.assertEqual(interface._conversation_scroll(FakeWindow(8, 10)), 0)

    def test_model_switch_keeps_messages_and_history(self) -> None:
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        interface.state.append_message("user", "我的代号是 blue-lantern")
        interface.state.append_message("assistant", "收到")
        # 新会话第一次切换：创建轻量 session 锚点。
        asyncio_run(interface.handle_command("/model other"))
        texts = [item.text for item in interface.state.messages]
        roles = [item.role for item in interface.state.messages]
        self.assertIn("我的代号是 blue-lantern", texts)
        self.assertIn("user", roles)
        self.assertTrue(any("发送第一条消息后开始保存对话" in item for item in texts))
        self.assertEqual(interface.state.session_id, "anchor-1")
        # 已有 session 时再次切换：历史保留提示 + 同一 session。
        asyncio_run(interface.handle_command("/model active"))
        asyncio_run(interface.handle_command("/model other"))
        texts = [item.text for item in interface.state.messages]
        self.assertTrue(any("历史继续保留" in item for item in texts))
        self.assertIn("我的代号是 blue-lantern", texts)
        self.assertEqual(interface.state.session_id, "anchor-1")
        self.assertEqual(interface.runtime.switch_session_ids[-1], "anchor-1")

    def test_resume_backfills_history_into_conversation(self) -> None:
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        asyncio_run(interface.handle_command("/resume abc"))
        texts = [item.text for item in interface.state.messages]
        roles = [item.role for item in interface.state.messages]
        self.assertIn("旧问题", texts)
        self.assertIn("旧回答", texts)
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)
        self.assertTrue(any("历史已回填" in item for item in texts))
        # 工具消息进入主对话流的活动行。
        self.assertTrue(any(m.role == "activity" and "工具结果" in m.text
                            for m in interface.state.messages))
        self.assertEqual(interface.state.session_id, "abc")


    def test_markdown_rendering(self) -> None:
        from conti_agent.tui import _markdown_fragments

        fragments = _markdown_fragments(
            "## 标题\n\n这是 **加粗** 和 `代码`。\n\n- 列表项\n"
            "```python\nprint(1)\n```\n"
        )
        text = "".join(item[1] for item in fragments)
        self.assertIn("标题", text)
        self.assertIn("加粗", text)
        self.assertIn("print(1)", text)
        self.assertNotIn("##", text)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        styles = [style for style, _ in fragments]
        self.assertIn("class:md-heading", styles)
        self.assertIn("class:md-bold", styles)
        self.assertIn("class:md-code", styles)
        self.assertIn("class:md-codeblock", styles)
        self.assertIn("class:md-bullet", styles)

    def test_conversation_uses_markdown_cache(self) -> None:
        state = TuiState(FakeRuntime().describe())
        message = state.append_message("assistant", "## 标题")
        first = state.render_conversation()
        message.text = "## 标题\n\n正文"
        second = state.render_conversation()
        self.assertIn("正文", "".join(item[1] for item in second))
        # 相同内容命中缓存，片段对象复用。
        message.text = message.text
        third = state.render_conversation()
        self.assertEqual(
            [item for item in second if item[1] == "正文"],
            [item for item in third if item[1] == "正文"],
        )


    def test_slash_completer_boundaries(self) -> None:
        """只按 / 不崩溃；候选替换位置连斜杠一起算，不产生双斜杠。"""
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        completer = interface.command_completer

        class FakeDocument:
            def __init__(self, text: str) -> None:
                self.text_before_cursor = text

        def applied_text(text: str) -> list[str]:
            results = []
            for completion in completer.get_completions(FakeDocument(text), None):
                start = completion.start_position
                results.append(text[:len(text) + start] + completion.text)
            return results

        self.assertTrue(any(item == "/models" for item in applied_text("/")))
        self.assertTrue(any(item == "/models" for item in applied_text("/mod")))
        self.assertIn("/model active", applied_text("/model act"))
        # 无候选时不产出、不报错。
        self.assertEqual(applied_text("/nomatch"), [])


    def test_scrollbar_fragments_render_in_both_modes(self) -> None:
        """滚动条渲染路径必须有输出且不抛错（内容超出一屏时的真实场景）。"""
        from conti_agent.tui import ScrollbarControl

        class Info:
            vertical_scroll = 40
            content_height = 100
            window_height = 20
            displayed_lines = list(range(40, 60))
            bottom_visible = False

        class Body:
            render_info = Info()

        viewport = ViewportState()
        control = ScrollbarControl(viewport, Body())

        # 跟随模式：有滑块，且滑块钉在轨道末端（▼ 之前一格是 █）。
        viewport.follow_bottom = True
        fragments = control._fragments()
        text = "".join(item[1] for item in fragments)
        self.assertIn("█", text)
        self.assertTrue(text.replace("\n", "").endswith("█▼"))

        # 手动阅读模式：同样必须能渲染。
        viewport.follow_bottom = False
        fragments = control._fragments()
        self.assertIn("█", "".join(item[1] for item in fragments))

        # 内容不足一屏：整列留空，不报错。
        Info.content_height = 10
        self.assertEqual(control._fragments(), [])


    def test_request_input_flow_does_not_block(self) -> None:
        """模型调用 request_input 时：问题进对话流、用户输入作为回答返回，
        不阻塞事件循环、不误发新任务（历史冻结 bug 的回归测试）。"""

        async def scenario() -> None:
            interface = ContiTui(FakeRuntime(), output=DummyOutput(),
                                 input=DummyInput())
            handler = interface.runtime.async_input_handler
            self.assertTrue(callable(handler))
            pending = asyncio.ensure_future(handler("要读取哪两个文件？"))
            await asyncio.sleep(0)
            # 问题进入对话流，界面等待回答。
            self.assertTrue(any("要读取哪两个文件？" in m.text
                                for m in interface.state.messages))
            # 用户在输入框输入的内容成为回答，而不是新任务。
            interface.input_control.text = "读 a.py 和 b.py"
            await interface.handle_prompt("读 a.py 和 b.py")
            self.assertEqual(await pending, "读 a.py 和 b.py")
            self.assertIsNone(interface._request_input_future)
            self.assertFalse(any(m.role == "assistant" and m.text == "读 a.py 和 b.py"
                                 for m in interface.state.messages))

        asyncio_run(scenario())

    def test_request_input_options_selection(self) -> None:
        async def scenario() -> None:
            interface = ContiTui(FakeRuntime(), output=DummyOutput(),
                                 input=DummyInput())
            pending = asyncio.ensure_future(
                interface.runtime.async_input_handler(
                    "下一步做什么？", ["继续阅读", "跑测试"]))
            await asyncio.sleep(0)
            # 选项渲染，默认选中第一项。
            self.assertIn("1) 继续阅读", interface._request_input_message.text)
            interface._move_request_selection(1)
            self.assertIn("❯ 2) 跑测试", interface._request_input_message.text)
            # 空输入提交 = 确认选中项。
            interface._submit_input()
            self.assertEqual(await pending, "跑测试")
            # 数字回答 = 选择对应选项。
            pending2 = asyncio.ensure_future(
                interface.runtime.async_input_handler("再来一次？", ["A", "B"]))
            await asyncio.sleep(0)
            await interface.handle_prompt("1")
            self.assertEqual(await pending2, "A")

        asyncio_run(scenario())

    def test_prompt_intercepted_while_compacting(self) -> None:
        """压缩进行中：新输入被拦截并提示，不误发新任务（HIGHLIGHTS 1.3.C）。"""

        async def scenario() -> None:
            interface = ContiTui(FakeRuntime(), output=DummyOutput(),
                                 input=DummyInput())
            interface.runtime.compacting = True
            before = len(interface.state.messages)
            await interface.handle_prompt("帮我写个脚本")
            self.assertIn("正在压缩上下文", interface.state.status)
            # 没有新消息进入对话流，也没有任务启动。
            self.assertEqual(len(interface.state.messages), before)

        asyncio_run(scenario())

    def test_request_input_tool_supports_async_handler(self) -> None:
        import asyncio

        from conti_agent.tools import ToolContext
        from conti_agent.tools_misc import RequestInputTool

        async def scenario() -> None:
            received: list[tuple[str, list[str] | None]] = []

            async def handler(question: str, options: list[str] | None = None) -> str:
                received.append((question, options))
                return "回答：2"

            tool = RequestInputTool(handler)
            result = await tool.execute(
                {"question": "下一步做什么？", "options": ["继续阅读", "跑测试"]},
                ToolContext(workspace=".", session_id="s"),
            )
            self.assertEqual(result.output, "回答：2")
            self.assertEqual(received[0][1], ["继续阅读", "跑测试"])

        asyncio_run(scenario())


    def test_streaming_keeps_true_chronology(self) -> None:
        """工具活动行与模型文本必须保持真实时序：
        iter1 文本 → 工具活动 → iter2 文本各自定位正确。"""

        async def scenario() -> None:
            interface = ContiTui(FakeRuntime(), output=DummyOutput(),
                                 input=DummyInput())
            state = interface.state
            state.messages.clear()
            state.append_message("user", "读取两个文件，做一下测试")
            interface._on_text_delta("我先看一下目录。")
            state.record_event(event("message.created", text="我先看一下目录。"))
            state.record_event(event("tool.requested", tool_name="workspace_list",
                                     tool_call_id="c1"))
            state.record_event(event("tool.completed", tool_name="workspace_list",
                                     tool_call_id="c1", is_error=False))
            state.record_event(event("tool.requested", tool_name="workspace_read",
                                     tool_call_id="c2"))
            state.record_event(event("tool.completed", tool_name="workspace_read",
                                     tool_call_id="c2", is_error=False))
            interface._on_text_delta("读取完成，总结如下。")
            state.record_event(event("message.created", text="读取完成，总结如下。"))
            self.assertEqual([m.role for m in state.messages],
                             ["user", "assistant", "activity", "activity",
                              "assistant"])
            self.assertEqual(state.messages[1].text, "我先看一下目录。")
            self.assertFalse(state.messages[1].streaming)
            self.assertEqual(state.messages[4].text, "读取完成，总结如下。")

        asyncio_run(scenario())


    def test_layout_mounts_completions_menu(self) -> None:
        """补全候选必须挂在 FloatContainer 的 CompletionsMenu 上才会显示。"""
        from prompt_toolkit.layout import CompletionsMenu, FloatContainer

        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        root = interface.application.layout.container
        self.assertIsInstance(root, FloatContainer)
        menus = [f.content for f in root.floats
                 if isinstance(f.content, CompletionsMenu)]
        self.assertTrue(menus)

    def test_command_output_grouped_into_one_system_message(self) -> None:
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        before = len([m for m in interface.state.messages if m.role == "system"])
        asyncio_run(interface.handle_command("/status"))
        system_messages = [m for m in interface.state.messages if m.role == "system"]
        # /status 的多行输出合并为一条消息，不再一行一个 SYSTEM 标题。
        self.assertEqual(len(system_messages), before + 1)
        latest = system_messages[-1].text
        self.assertIn("模型：", latest)
        self.assertIn("会话：", latest)


    def test_layout_fills_height_and_activity_expands(self) -> None:
        """footer 必须贴底（不能按权重分走半屏），活动详情可展开。"""
        import asyncio
        from unittest import mock

        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.layout.mouse_handlers import MouseHandlers
        from prompt_toolkit.layout.screen import Screen, WritePosition

        async def scenario() -> None:
            class _StubLayout:
                current_control = None
                current_window = None

            class _StubApp:
                render_counter = 0
                layout = _StubLayout()

            stub = _StubApp()
            interface = ContiTui(FakeRuntime(), output=DummyOutput(),
                                 input=DummyInput())
            state = interface.state
            state.messages.clear()
            state.append_message("user", "读取文件")
            state.record_event(event("tool.requested", tool_name="workspace_read",
                                     tool_call_id="c1",
                                     arguments={"path": "a.py"}))
            state.record_event(event("tool.completed", tool_name="workspace_read",
                                     tool_call_id="c1", is_error=False,
                                     output="文件内容片段", arguments={"path": "a.py"}))

            def render() -> list[str]:
                stub.render_counter += 1
                interface.output.size = Size(rows=24, columns=100)
                root = interface._root_layout()
                screen = Screen()
                root.write_to_screen(screen, MouseHandlers(),
                                     WritePosition(0, 0, 100, 24), "", True, None)
                return ["".join(screen.data_buffer[y][x].char
                                for x in range(100)).rstrip() for y in range(24)]

            with mock.patch("prompt_toolkit.layout.controls.get_app",
                            return_value=stub):
                lines = render()
            # footer 贴在最后一行（不能按权重分走半屏留下大片空白）。
            self.assertIn("Ctrl+Q 退出", lines[23])
            self.assertIn("点击权限档切换", lines[23])

            # 收起状态：不含详情。
            self.assertFalse(any("文件内容片段" in line for line in lines))
            # Ctrl+O 展开后：参数与结果预览可见。
            state.activity_expanded = True
            with mock.patch("prompt_toolkit.layout.controls.get_app",
                            return_value=stub):
                lines = render()
            joined = "\n".join(lines)
            self.assertIn('"path": "a.py"', joined)
            self.assertIn("文件内容片段", joined)
            self.assertIn("参数：", joined)

        asyncio_run(scenario())

    def test_compaction_shows_start_then_done(self) -> None:
        state = TuiState(FakeRuntime().describe())
        state.record_event(event("context.compacting", reason="auto"))
        self.assertTrue(any("正在压缩" in m.text
                            for m in state.messages if m.role == "activity"))
        state.record_event(event("context.compacted", reason="auto"))
        activity = [m for m in state.messages if m.role == "activity"]
        # 开始行被原位更新为完成，不产生第二条。
        self.assertEqual(len(activity), 1)
        self.assertIn("已压缩早期历史为摘要", activity[0].text)


    def test_permission_chip_click_cycles_modes(self) -> None:
        """状态栏权限芯片：渲染模式名，点击循环切换并立即生效。"""
        interface = ContiTui(FakeRuntime(), output=DummyOutput(), input=DummyInput())
        fragments = interface._model_status_fragments()
        chip = next(item for item in fragments
                    if len(item) >= 2 and "权限:" in item[1])
        self.assertIn("标准", chip[1])
        self.assertTrue(callable(chip[2]))
        # 点击一次：workspace -> trusted。
        chip[2](None)
        self.assertEqual(interface.runtime.get_permission_mode(), "trusted")
        self.assertEqual(interface.state.runtime_info["permission_mode"],
                         "trusted")
        self.assertTrue(any("权限档位已切换" in m.text
                            for m in interface.state.messages))
        # 点击到 trusted 之后回到 read_only。
        chip = next(item for item in interface._model_status_fragments()
                    if len(item) >= 2 and "权限:" in item[1])
        self.assertIn("放行", chip[1])
        chip[2](None)
        self.assertEqual(interface.runtime.get_permission_mode(), "read_only")


def asyncio_run(awaitable: Any) -> None:
    import asyncio
    asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
