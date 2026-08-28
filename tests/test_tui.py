from __future__ import annotations

import unittest
from typing import Any

from conti_agent.events import event

try:
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from conti_agent.tui import STARTUP_LOGO, ContiTui, TuiState
except ImportError:  # pragma: no cover - 核心运行时仍可在无 TUI 依赖时使用
    STARTUP_LOGO = None
    ContiTui = None
    TuiState = None
    DummyInput = None
    DummyOutput = None


class FakeSessions:
    def list(self) -> list[dict[str, Any]]:
        return [{"session_id": "abc", "title": "旧任务"}]

    def load(self, session_id: str) -> None:
        self.loaded = session_id


class FakeRuntime:
    def __init__(self) -> None:
        self.sessions = FakeSessions()

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "test-provider",
            "model": "test-model",
            "protocol": "openai-compat",
            "permission_mode": "workspace",
            "workspace": "W",
            "tools": ["a", "b"],
        }


@unittest.skipIf(ContiTui is None, "未安装 prompt-toolkit")
class TuiTestCase(unittest.TestCase):
    def test_startup_logo_is_independent_ascii(self) -> None:
        self.assertIn("CONTI-AGENT TUI", STARTUP_LOGO)
        self.assertIn("____", STARTUP_LOGO)

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


def asyncio_run(awaitable: Any) -> None:
    import asyncio
    asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
