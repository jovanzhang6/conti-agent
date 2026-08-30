from __future__ import annotations

import asyncio
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
        """规则 last-match-wins：高优先级文件的窄规则覆盖低优先级宽规则。"""
        project = self.root / ".conti" / "permissions.toml"
        local = self.root / ".conti" / "permissions.local.toml"
        project.parent.mkdir(parents=True)
        project.write_text(r"""
[[rule]]
tool = "workspace_write"
decision = "allow"
pattern = 'a.allowed'
""", encoding="utf-8")
        local.write_text(r"""
[[rule]]
tool = "workspace_write"
decision = "deny"
pattern = 'a.allowed'
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

    async def test_read_only_write_asks_and_always_caches(self) -> None:
        """只读档写操作走审批；always 答复按工具缓存本会话有效。"""
        calls: list[str] = []
        async def approve(key: str, arguments: dict[str, Any], reason: str) -> str:
            calls.append(key)
            return "always"
        checker = PermissionChecker("read_only", workspace=self.workspace,
                                    approver=approve)
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

    async def test_agent_interrupt_fills_tool_results(self) -> None:
        """中断时未完成的 tool_call 必须补合成 tool_result（协议配对完整，
        主上下文与会话账本一致）。"""

        class BlockingTool(Tool):
            name = "blocking"
            description = "永不完成的工具。"
            parameters = {"type": "object", "properties": {}}
            effects = frozenset({"read"})

            async def execute(self, arguments: dict[str, Any],
                              context: ToolContext) -> ToolResult:
                await asyncio.Event().wait()
                return ToolResult("never")

        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("t1", "blocking", {})]),
            ProviderResponse(text="done"),
        ])
        registry = ToolRegistry()
        registry.register(BlockingTool())
        store = SessionStore(self.root / ".conti")
        session_id, _ = store.create(self.root, "中断测试")
        messages = [user_message("跑")]
        agent = Agent(provider, registry, self.context,
                      session_store=store, session_id=session_id)

        events: list[Any] = []

        async def consume() -> None:
            async for item in agent.run(messages):
                events.append(item)

        task = asyncio.ensure_future(consume())
        for _ in range(200):
            if any(item.type == "tool.requested" for item in events):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(any(item.type == "tool.requested" for item in events))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 中断后合成 tool_result 追加到上下文与账本，配对完整。
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn("中断", messages[-1]["content"])
        _, resumed = store.load(session_id)
        self.assertEqual(resumed[-1]["role"], "tool")
        self.assertIn("中断", resumed[-1]["content"])

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

    async def test_compact_messages_replays_prefix_for_cache(self) -> None:
        """摘要请求重放真实 system + 被压缩旧消息原文 + 末尾压缩指令，
        tool schema 原样传入且 tool_choice=none（HIGHLIGHTS 1.3.A/B）。"""
        from conti_agent.config import load_single
        from conti_agent.providers import ProviderResponse
        from conti_agent.runtime import Runtime

        class ScriptedProvider:
            def __init__(self) -> None:
                self.calls: list[list[dict[str, Any]]] = []
                self.tool_choices: list[str | None] = []
                self.registries: list[Any] = []

            async def complete(self, messages, registry, stream_handler=None,
                               tool_choice=None):
                self.calls.append([dict(message) for message in messages])
                self.tool_choices.append(tool_choice)
                self.registries.append(registry)
                return ProviderResponse(
                    text="<compacted-summary>\n## 目标与约束\n压缩目标A\n</compacted-summary>",
                    usage=None,
                )

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
        system_prompt = runtime._system_prompt()
        long = "y" * 400
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "目标A " + long},
            {"role": "assistant", "content": "结论B " + long},
            {"role": "user", "content": "u2 " + long},
            {"role": "assistant", "content": "a2"},
        ]
        summary = await runtime.compact_messages(messages, None, reason="manual")
        # 前缀一致性：system 与旧消息逐字节重放，压缩指令是末尾 user 消息。
        sent = provider.calls[0]
        self.assertEqual(sent[0], {"role": "system", "content": system_prompt})
        self.assertEqual(sent[1], {"role": "user", "content": "目标A " + long})
        self.assertEqual(sent[2], {"role": "assistant", "content": "结论B " + long})
        self.assertEqual(sent[-1]["role"], "user")
        self.assertIn("压缩为一份摘要", sent[-1]["content"])
        # 结构化模板七节 + <compacted-summary> 标签 + 增量合并规则。
        for section in ("## 目标与约束", "## 关键决策", "## 文件与代码",
                        "## 错误与修复", "## 当前进度", "## 下一步", "## 关键上下文"):
            self.assertIn(section, sent[-1]["content"])
        self.assertIn("<compacted-summary>", sent[-1]["content"])
        self.assertIn("增量合并", sent[-1]["content"])
        # tool schema 原样传入，tool_choice="none"。
        self.assertIs(provider.registries[0], runtime.registry)
        self.assertEqual(provider.tool_choices, ["none"])
        # 对话重组：system 保留，摘要 user 消息紧随其后，近期原文保留。
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("<compacted-summary>", messages[1]["content"])
        self.assertIn("压缩目标A", summary)
        self.assertIn("u2 " + long, messages[-2]["content"])

    async def test_compact_lock_rejects_reentry(self) -> None:
        """compacting 期间 compact_messages 直接返回空，不重入。"""
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
        runtime.compacting = True
        messages = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        self.assertEqual(await runtime.compact_messages(messages, None), "")
        # 锁不吞对话：消息列表原样未动。
        self.assertEqual(len(messages), 3)
        # 正常路径结束后锁必须释放（try/finally）。
        runtime.compacting = False
        provider_calls: list[list[dict[str, Any]]] = []

        class TinyProvider:
            async def complete(self, messages, registry, stream_handler=None,
                               tool_choice=None):
                provider_calls.append(list(messages))
                from conti_agent.providers import ProviderResponse
                return ProviderResponse(text="摘要", usage=None)

        runtime.provider = TinyProvider()
        await runtime.compact_messages(messages, None)
        self.assertFalse(runtime.compacting)
        self.assertTrue(provider_calls)


if __name__ == "__main__":
    unittest.main()
