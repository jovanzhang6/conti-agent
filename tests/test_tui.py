from __future__ import annotations

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
            "permission_mode": "workspace",
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
        state.record_event(event("tool.requested", tool_name="workspace_read"))
        state.record_event(event("tool.completed", tool_name="workspace_read",
                                 is_error=False))
        state.record_event(event("usage.recorded", input_tokens=3,
                                 output_tokens=5))
        self.assertEqual(state.tool_count, 1)
        self.assertEqual(state.usage, {"input_tokens": 3, "output_tokens": 5})
        self.assertEqual(len(state.activity), 2)

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
        # 工具消息只进入活动栏。
        self.assertTrue(any("工具输出" in item for item in interface.state.activity))
        self.assertEqual(interface.state.session_id, "abc")


def asyncio_run(awaitable: Any) -> None:
    import asyncio
    asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
