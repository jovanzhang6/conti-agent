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

    def test_token_estimation_is_cjk_aware(self) -> None:
        from conti_agent.context import estimate_tokens
        # 宽字符（中日韩）按约 1 token/字，其余按 4 字符/token。
        self.assertEqual(estimate_tokens("中文内容"), 4)
        self.assertEqual(estimate_tokens("abcdefgh"), 2)
        self.assertEqual(estimate_tokens("中文ab"), 3)
        self.assertEqual(estimate_tokens(""), 0)

    def test_compaction_trigger_and_usage_projection(self) -> None:
        manager = ContextManager(context_window=200_000, max_output_tokens=8_192)
        # 触发点 = 窗口 − max_output（答案预留）− 10% 窗口（估算安全垫）。
        self.assertEqual(manager.compaction_trigger, 171_808)
        self.assertTrue(
            manager.needs_compaction([{"role": "user", "content": "测" * 172_000}])
        )
        self.assertFalse(manager.needs_compaction([{"role": "user", "content": "hi"}]))
        # 精确基数（上次 usage）+ 待发送增量估算（含消息键值与条目开销）。
        manager.observe_usage(100_000, 5_000)
        self.assertEqual(
            manager.projected_input_tokens([{"role": "user", "content": "测" * 100}]),
            105_108,
        )

    def test_usage_baseline_schema_and_invalidation(self) -> None:
        manager = ContextManager(context_window=32_000, max_output_tokens=2_048,
                                 tool_schema_tokens=500)
        self.assertEqual(manager.compaction_trigger, 32_000 - 2_048 - 3_200)
        body = [{"role": "user", "content": "测" * 26_600}]
        # 无精确基线时，全量估算必须把工具 schema 也计入。
        self.assertTrue(manager.needs_compaction(body))
        manager.tool_schema_tokens = 0
        self.assertFalse(manager.needs_compaction(body))
        # 精确基线覆盖数：投影只估算覆盖数之后的新增消息。
        manager.tool_schema_tokens = 500
        manager.observe_usage(10_000, 1_000, observed_count=2)
        self.assertEqual(
            manager.projected_input_tokens([{"role": "user", "content": "x" * 40}]),
            11_018,
        )
        # 压缩后基线失效。
        manager.invalidate_baseline()
        self.assertIsNone(manager.last_input_tokens)
        self.assertEqual(manager.observed_count, 0)

    def test_split_for_compaction_preserves_tool_pairing(self) -> None:
        manager = ContextManager(context_window=200_000)
        long = "y" * 400
        messages = [
            {"role": "user", "content": "u1 " + long},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "a1", "name": "t", "arguments": {}}]},
            {"role": "tool", "content": "r1", "tool_call_id": "a1"},
            {"role": "assistant", "content": "a1 " + long},
            {"role": "user", "content": "u2 " + long},
            {"role": "assistant", "content": "a2"},
        ]
        split = manager.split_for_compaction(messages, keep_tokens=180)
        self.assertIsNotNone(split)
        old, tail = split
        # 保留部分必须以 user 开头，tool 与 assistant 配对不被切断。
        self.assertEqual(tail[0]["role"], "user")
        for index, message in enumerate(tail):
            if message["role"] == "tool":
                self.assertEqual(tail[index - 1]["role"], "assistant")
        self.assertTrue(old)
        self.assertTrue(tail)

    def test_compact_assembles_summary_user_message(self) -> None:
        manager = ContextManager(context_window=200_000)
        long = "y" * 400
        messages = [
            {"role": "user", "content": "u1 " + long},
            {"role": "assistant", "content": "a1 " + long},
            {"role": "user", "content": "u2 " + long},
            {"role": "assistant", "content": "a2"},
        ]
        compacted, count = manager.compact(messages, "目标 X；结论 Y",
                                           keep_tokens=180)
        self.assertEqual(count, 2)
        self.assertEqual(compacted[0]["role"], "user")
        self.assertIn("[历史摘要]", compacted[0]["content"])
        self.assertIn("目标 X", compacted[0]["content"])
        self.assertEqual(compacted[1]["content"], "u2 " + long)
        self.assertEqual(compacted[-1]["content"], "a2")

    def test_default_summary_fallback_lists_user_goals(self) -> None:
        from conti_agent.context import default_summary
        summary = default_summary([{"role": "user", "content": "目标A"}], "保留要点")
        self.assertIn("目标A", summary)
        self.assertIn("保留要点", summary)

    def test_result_spiller_spills_large_output(self) -> None:
        from conti_agent.context import ResultSpiller
        spiller = ResultSpiller(self.root / ".conti" / "spill",
                                single_limit=100, round_limit=10_000,
                                preview_chars=50)
        small = spiller.process("search", "x" * 50)
        self.assertEqual(small, "x" * 50)
        big = spiller.process("read", "y" * 500)
        self.assertIn("已落盘", big)
        self.assertIn("完整内容", big)
        self.assertIn("y" * 50, big)
        self.assertNotIn("y" * 100, big)
        spilled_path = Path(spiller.spilled[0])
        self.assertTrue(spilled_path.exists())
        self.assertEqual(spilled_path.read_text(encoding="utf-8"), "y" * 500)

    def test_ledger_compaction_roundtrip_with_summary_message(self) -> None:
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root, "压缩回放")
        store.append_message(session_id, user_message("旧消息"))
        summary_message = {"role": "user", "content": "[历史摘要]\n摘要内容"}
        store.append_compaction(session_id, "摘要内容", 1,
                                summary_message=summary_message)
        store.append_message(session_id, user_message("新消息"))
        _, resumed = store.load(session_id)
        self.assertEqual(resumed[0], summary_message)
        self.assertEqual(resumed[1]["content"], "新消息")

    async def test_runtime_compacts_silently_on_context_overflow(self) -> None:
        from conti_agent.config import load_single
        from conti_agent.errors import ProviderError
        from conti_agent.providers import ProviderResponse
        from conti_agent.runtime import Runtime

        class OverflowOnceProvider:
            def __init__(self) -> None:
                self.calls = 0

            async def complete(self, messages, registry, stream):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError(
                        "This model's maximum context length is 128000 tokens"
                    )
                return ProviderResponse(text="done", usage=None)

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
        runtime.provider = OverflowOnceProvider()
        final, _, _ = await runtime.ask("你好")
        # 第一次请求超限：静默压缩后重试，错误不抛给用户。
        self.assertEqual(final, "done")
        self.assertEqual(runtime.provider.calls, 2)

    async def test_compact_messages_uses_model_summary(self) -> None:
        from conti_agent.config import load_single
        from conti_agent.providers import ProviderResponse
        from conti_agent.runtime import Runtime

        class ScriptedProvider:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def complete(self, messages, registry, stream):
                self.prompts.append(messages[-1]["content"])
                return ProviderResponse(text="模型摘要：目标A；结论B", usage=None)

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
        provider = ScriptedProvider()
        runtime.provider = provider
        long = "y" * 400
        messages = [
            {"role": "user", "content": "目标A " + long},
            {"role": "assistant", "content": "结论B " + long},
            {"role": "user", "content": "u2 " + long},
            {"role": "assistant", "content": "a2"},
        ]
        await runtime.compact_messages(messages, None, reason="manual")
        # 摘要请求发给了模型，压缩结果以 user 摘要消息打头。
        self.assertTrue(any("压缩器" in p for p in provider.prompts))
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("模型摘要：目标A；结论B", messages[0]["content"])
        self.assertIn("u2 " + long, messages[-2]["content"])


if __name__ == "__main__":
    unittest.main()
