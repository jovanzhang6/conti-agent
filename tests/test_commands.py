from __future__ import annotations

import asyncio
import unittest

from conti_agent.commands import CommandContext, create_default_registry


class FakeRuntime:
    def __init__(self) -> None:
        self.busy = False
        self.commands = create_default_registry()
        self.provider_names = ["one", "two"]
        self.active_name = "one"
        self.switch_session_ids: list[str | None] = []
        self.history: dict[str, list[dict]] = {
            "abc": [
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
                {"role": "tool", "content": "工具输出"},
            ],
        }

    def describe(self):
        return {"provider": self.active_name, "permission_mode": "workspace"}

    def list_providers(self):
        return [
            {"name": name, "model": f"model-{name}", "protocol": "fake",
             "active": name == self.active_name, "api_key_ready": True}
            for name in self.provider_names
        ]

    def get_provider_info(self, name):
        if name not in self.provider_names:
            raise ValueError("unknown")
        return {"name": name, "model": f"model-{name}", "active": name == self.active_name}

    def active_provider_name(self):
        return self.active_name

    def set_active_provider(self, name, *, session_id=None):
        if self.busy:
            raise RuntimeError("busy")
        if name not in self.provider_names:
            raise ValueError("unknown")
        self.active_name = name
        created = session_id is None
        self.switch_session_ids.append(session_id)
        return {
            "from_provider": "old",
            "from_model": "old-m",
            "to_provider": name,
            "to_model": f"model-{name}",
            "session_id": session_id or "new-anchor",
            "created_session": created,
        }

    def load_session_history(self, session_id):
        if session_id not in self.history:
            raise KeyError(f"会话不存在：{session_id}")
        return self.history[session_id]

    class sessions:
        @staticmethod
        def list():
            return [{"session_id": "abc", "title": "old"}]


class CommandTestCase(unittest.TestCase):
    def test_suggestions_include_commands_and_models(self) -> None:
        registry = create_default_registry()
        context = CommandContext(FakeRuntime())
        commands = [item.value for item in registry.suggest("/", context)]
        self.assertIn("/models", commands)
        self.assertIn("/model", commands)
        models = [item.value for item in registry.suggest("/model ", context)]
        self.assertEqual(models, ["one", "two"])

    def test_models_and_model_switch(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        context = CommandContext(runtime)
        result = asyncio.run(registry.execute("/models", context))
        self.assertTrue(result.ok)
        self.assertIn("model-one", "\n".join(result.output))
        result = asyncio.run(registry.execute("/model two", context))
        self.assertTrue(result.ok)
        self.assertEqual(runtime.active_name, "two")

    def test_model_switch_passes_session_and_keeps_history(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        context = CommandContext(runtime, session_id="sess-1")
        result = asyncio.run(registry.execute("/model two", context))
        self.assertTrue(result.ok)
        self.assertEqual(runtime.switch_session_ids[-1], "sess-1")
        self.assertEqual(result.session_id, "sess-1")
        self.assertIn("历史继续保留", "\n".join(result.output))

    def test_model_switch_without_session_creates_anchor(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        context = CommandContext(runtime)
        result = asyncio.run(registry.execute("/model two", context))
        self.assertTrue(result.ok)
        self.assertIsNone(runtime.switch_session_ids[-1])
        self.assertEqual(result.session_id, "new-anchor")
        self.assertIn("发送第一条消息后开始保存对话", "\n".join(result.output))

    def test_model_switch_is_rejected_when_busy(self) -> None:
        runtime = FakeRuntime()
        runtime.busy = True
        registry = create_default_registry()
        result = asyncio.run(registry.execute("/model two", CommandContext(runtime)))
        self.assertFalse(result.ok)
        self.assertIn("运行中", result.output[0])

    def test_unknown_command_and_missing_argument(self) -> None:
        registry = create_default_registry()
        context = CommandContext(FakeRuntime())
        result = asyncio.run(registry.execute("/missing", context))
        self.assertFalse(result.ok)
        result = asyncio.run(registry.execute("/model", context))
        self.assertFalse(result.ok)
        self.assertIn("缺少", result.output[0])

    def test_status_includes_session(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        result = asyncio.run(registry.execute("/status", CommandContext(runtime)))
        self.assertTrue(any("新会话" in line for line in result.output))
        result = asyncio.run(
            registry.execute("/status", CommandContext(runtime, session_id="s9"))
        )
        self.assertIn("session: s9", result.output)

    def test_resume_backfills_history(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        context = CommandContext(runtime)
        result = asyncio.run(registry.execute("/resume abc", context))
        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "abc")
        self.assertEqual(context.session_id, "abc")
        self.assertEqual(len(result.data["history"]), 3)
        self.assertIn("历史已回填", "\n".join(result.output))

    def test_resume_unknown_session_reports_error(self) -> None:
        registry = create_default_registry()
        runtime = FakeRuntime()
        result = asyncio.run(registry.execute("/resume missing", CommandContext(runtime)))
        self.assertFalse(result.ok)
        self.assertIn("恢复会话失败", result.output[0])


if __name__ == "__main__":
    unittest.main()
