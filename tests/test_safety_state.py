from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.agent import Agent
from conti_agent.context import ContextManager, estimate_message_tokens
from conti_agent.messages import ToolCall, user_message
from conti_agent.permissions import (
    AuditLogger,
    DangerousCommandDetector,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.sessions import SessionError, SessionStore
from conti_agent.tools import Tool, ToolContext, ToolRegistry, ToolResult
from conti_agent.workspace import Workspace


class WriteTool(Tool):
    name = "workspace_write"
    description = "测试用写入工具。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    effects = frozenset({"write"})

    def __init__(self) -> None:
        self.called = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        self.called = True
        Path(context.workspace, arguments["path"]).write_text(
            arguments["content"], encoding="utf-8"
        )
        return ToolResult("已写入")


class SafetyStateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.workspace = Workspace(self.root)
        self.context = ToolContext(workspace=self.root, session_id="s1")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    async def test_permission_modes_and_dangerous_commands(self) -> None:
        checker = PermissionChecker("read_only", workspace=self.workspace)
        tool = WriteTool()
        decision = await checker.check(tool, {"path": "a.txt", "content": "x"}, self.context)
        self.assertFalse(decision.allowed)
        self.assertEqual(checker.mode, PermissionMode.READ_ONLY)
        self.assertIsNotNone(DangerousCommandDetector().inspect({"command": ["rm", "-rf", "."]}))
        self.assertIsNone(DangerousCommandDetector().inspect({"command": ["python", "--version"]}))

    async def test_rule_precedence_and_audit(self) -> None:
        project = self.root / ".conti" / "permissions.toml"
        local = self.root / ".conti" / "permissions.local.toml"
        project.parent.mkdir(parents=True)
        project.write_text(r"""
[[rule]]
tool = "workspace_write"
decision = "allow"
pattern = '\.allowed$'
""", encoding="utf-8")
        local.write_text(r"""
[[rule]]
tool = "workspace_write"
decision = "deny"
pattern = '\.allowed$'
""", encoding="utf-8")
        audit_path = self.root / ".conti" / "runtime" / "audit.jsonl"
        checker = PermissionChecker(
            "workspace", workspace=self.workspace,
            rules=RuleEngine([project, local]),
        )
        tool = WriteTool()
        denied = await checker.check(tool, {"path": "a.allowed", "content": ""}, self.context)
        self.assertFalse(denied.allowed)
        AuditLogger(audit_path).record(
            "denied", tool, {"path": "a.allowed", "content": "secret"}, denied, self.context
        )
        self.assertIn('"event": "denied"', audit_path.read_text(encoding="utf-8"))
        self.assertNotIn("secret", audit_path.read_text(encoding="utf-8"))

    async def test_approved_mode_uses_approver_once(self) -> None:
        calls: list[str] = []
        async def approve(key: str, arguments: dict[str, Any], reason: str) -> bool:
            calls.append(key)
            return True
        checker = PermissionChecker("approved", workspace=self.workspace, approver=approve)
        tool = WriteTool()
        first = await checker.check(tool, {"path": "a.txt", "content": "x"}, self.context)
        second = await checker.check(tool, {"path": "b.txt", "content": "x"}, self.context)
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertEqual(len(calls), 1)

    async def test_agent_denial_does_not_execute_tool(self) -> None:
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("one", "workspace_write",
                                                  {"path": "a.txt", "content": "x"})]),
            ProviderResponse(text="完成"),
        ])
        registry = ToolRegistry()
        tool = WriteTool()
        registry.register(tool)
        checker = PermissionChecker("read_only", workspace=self.workspace)
        audit_path = self.root / "audit.jsonl"
        agent = Agent(provider, registry, self.context,
                      permission_checker=checker,
                      auditor=AuditLogger(audit_path))
        events = [event async for event in agent.run([user_message("写入")])]
        self.assertFalse(tool.called)
        self.assertTrue(any(item.type == "tool.approved" and
                            item.payload["decision"] == "denied" for item in events))
        self.assertIn('"event": "denied"', audit_path.read_text(encoding="utf-8"))

    async def test_agent_hook_denies_after_permission(self) -> None:
        class DenyHookEngine:
            async def run(self, event: str, tool_name: str, payload: dict[str, Any]):
                from conti_agent.hooks import HookOutcome
                if event == "tool.before" and tool_name == "workspace_write":
                    return HookOutcome(False, "策略拒绝写入")
                return HookOutcome(True, "通过")
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("one", "workspace_write",
                                                  {"path": "hooked.txt", "content": "x"})]),
            ProviderResponse(text="完成"),
        ])
        registry = ToolRegistry()
        tool = WriteTool()
        registry.register(tool)
        agent = Agent(
            provider, registry, self.context,
            permission_checker=PermissionChecker("workspace", workspace=self.workspace),
            hook_engine=DenyHookEngine(),
        )
        async for _ in agent.run([user_message("写入")]):
            pass
        self.assertFalse(tool.called)
        self.assertFalse((self.root / "hooked.txt").exists())

    def test_runtime_adds_system_prompt(self) -> None:
        from conti_agent.config import load_single
        from conti_agent.runtime import Runtime
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
""", encoding="utf-8")
        runtime = Runtime(load_single(config_path), self.root,
                          output_function=lambda text: None)
        messages = [user_message("你好")]
        messages.insert(0, {"role": "system", "content": runtime._system_prompt()})
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("当前工作区", messages[0]["content"])
        self.assertIn("workspace", messages[0]["content"])

    async def test_tool_call_assistant_message_is_persisted(self) -> None:
        from conti_agent.sessions import SessionStore
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("one", "workspace_write",
                                                  {"path": "session.txt", "content": "x"})]),
            ProviderResponse(text="done"),
        ])
        registry = ToolRegistry()
        tool = WriteTool()
        registry.register(tool)
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root)
        agent = Agent(provider, registry, self.context, session_store=store,
                      session_id=session_id)
        async for _ in agent.run([user_message("调用")]):
            pass
        _, messages = store.load(session_id)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertEqual(messages[0]["tool_calls"][0].name, "workspace_write")
        self.assertEqual(messages[1]["role"], "tool")
        self.assertTrue(tool.called)

    def test_session_append_and_resume(self) -> None:
        store = SessionStore(self.root / ".conti")
        session_id, messages = store.create(self.root, "测试会话")
        store.append_message(session_id, user_message("你好"))
        store.append_message(session_id, {"role": "assistant", "content": "问候",
                                          "tool_calls": [{"id": "x", "name": "t",
                                                          "arguments": {"a": 1}}]})
        metadata, resumed = store.load(session_id)
        self.assertEqual(metadata["title"], "测试会话")
        self.assertEqual(resumed[0]["content"], "你好")
        self.assertEqual(resumed[1]["tool_calls"][0].name, "t")
        self.assertEqual(store.list()[0]["session_id"], session_id)

    def test_session_metadata_and_model_switch(self) -> None:
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root, "模型轨迹",
                                     metadata={"provider": "p1", "model": "m1"})
        store.append_message(session_id, user_message("继续"))
        store.append_model_switch(session_id, from_provider="p1", from_model="m1",
                                  to_provider="p2", to_model="m2")
        metadata, resumed = store.load(session_id)
        self.assertEqual(metadata["provider"], "p1")
        self.assertEqual(metadata["model"], "m1")
        # model.switched 是事件记录，不产生对话消息。
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["content"], "继续")

    def test_legacy_session_without_provider_metadata_loads(self) -> None:
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root, "旧格式")
        path = store.directory / f"{session_id}.jsonl"
        path.write_text(
            path.read_text(encoding="utf-8")
            + '{"schema_version": 1, "kind": "message.appended", "timestamp": 1,'
              ' "message": {"role": "user", "content": "旧消息"}}\n',
            encoding="utf-8",
        )
        metadata, resumed = store.load(session_id)
        self.assertNotIn("provider", metadata)
        self.assertEqual(resumed[0]["content"], "旧消息")

    def test_session_corruption_is_rejected(self) -> None:
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root)
        path = store.directory / f"{session_id}.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
        with self.assertRaises(SessionError):
            store.load(session_id)

    def test_context_budget_and_compaction(self) -> None:
        manager = ContextManager(context_window=100, max_output_tokens=10)
        messages = [
            {"role": "system", "content": "durable"},
            *[{"role": "user", "content": f"message-{index} " + ("y" * 200)}
              for index in range(30)],
            {"role": "user", "content": "latest"},
        ]
        self.assertTrue(manager.needs_compaction(messages))
        def summarize(old: list[dict[str, Any]], instruction: str) -> str:
            return f"{len(old)} turns summarized"
        compacted, summary, count = manager.compact(messages, summarize, keep_recent=2)
        self.assertEqual(summary, "29 turns summarized")
        self.assertEqual(count, 29)
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("[历史摘要]", compacted[1]["content"])
        planned = manager.plan(messages, keep_recent=2)
        self.assertLessEqual(planned.estimated_tokens, manager.budget)
        self.assertEqual(planned.messages[-1]["content"], "latest")


if __name__ == "__main__":
    unittest.main()
