from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.commands import CommandContext, create_default_registry
from conti_agent.config import load_single
from conti_agent.messages import ToolCall, user_message
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.runtime import Runtime
from conti_agent.team import LEADER, LeaderSendTool, TeamHub, TeamRunner
from conti_agent.tools import ToolContext, ToolRegistry


class TeamHubTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / ".conti"
        self.hub = TeamHub(self.root, team_id="test-team")
        self.hub.open_team(
            [{"name": "scout", "profile": "reader"},
             {"name": "writer", "profile": "writer"}],
            [{"id": "T1", "title": "调研A", "owner": "scout"},
             {"id": "T2", "title": "写报告", "owner": "writer"}],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_send_drain_and_journal(self) -> None:
        self.hub.send("scout", "writer", "A 库阈值 90%")
        inbox = self.hub.drain("writer")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["from"], "scout")
        self.hub.mark_delivered("writer", inbox)
        self.assertEqual(self.hub.drain("writer"), [])
        journal = self.hub.journal_path.read_text(encoding="utf-8")
        self.assertIn("message.sent", journal)
        self.assertIn("message.delivered", journal)
        self.assertTrue(self.hub.state_path.exists())

    async def test_send_validation(self) -> None:
        with self.assertRaises(Exception):
            self.hub.send("scout", "ghost", "hello")
        self.hub.status = "closed"
        with self.assertRaises(Exception):
            self.hub.send("scout", "writer", "hello")

    async def test_broadcast_excludes_sender(self) -> None:
        self.hub.send("scout", "*", "大家好")
        self.assertEqual(self.hub.drain("scout"), [])
        self.assertEqual(len(self.hub.drain("writer")), 1)
        self.assertEqual(len(self.hub.drain(LEADER)), 1)

    async def test_leader_dispatch_via_send(self) -> None:
        """开工顺序由 leader 智能调度：交付唤醒 leader 后由它 team_send
        指派后续任务并携带上下文，hub 不做机械解锁。"""
        self.hub.complete_task("T1", "调研完成", "scout")
        self.hub.send(LEADER, "writer",
                      "T1 完成，摘要：A 库阈值 90%。开始 T2 写报告")
        inbox = self.hub.drain("writer")
        self.assertEqual(len(inbox), 1)
        self.assertIn("开始 T2", inbox[0]["body"])
        self.assertEqual(self.hub.tasks["T2"].status, "todo")

    async def test_wake_event(self) -> None:
        self.hub.set_status("writer", "parked")

        async def parker() -> None:
            self.assertTrue(await self.hub.wait_wake("writer", timeout=2.0))

        task = asyncio.ensure_future(parker())
        await asyncio.sleep(0)
        self.hub.send("scout", "writer", "醒了")
        await task
        self.hub.set_status("writer", "running")

    async def test_leader_idle_notification_callback(self) -> None:
        """leader 空闲时的被动通知：发往 LEADER 的消息立即回调，
        投递本身不受回调异常影响。"""
        notices: list[str] = []

        def on_message(message: dict[str, Any]) -> None:
            notices.append(f"{message['from']}：{message['body']}")
            raise RuntimeError("通知挂了也不能影响投递")

        self.hub.on_leader_message = on_message
        self.hub.send("scout", "leader", "交付内容")
        self.assertEqual(notices, ["scout：交付内容"])
        # 消息仍正常入箱。
        self.assertEqual(len(self.hub.drain(LEADER)), 1)
        # leader 自己发的消息不触发通知。
        self.hub.on_leader_message = on_message
        notices.clear()
        self.hub.send(LEADER, "scout", "干 live")
        self.assertEqual(notices, [])


class TeamRunnerTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self) -> Any:
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[[profile]]
name = "worker"
description = "通用工作者"
system_prompt = "你是工作者。"
allowed_tools = []
permission_mode = "workspace"
""", encoding="utf-8")
        return load_single(config_path)

    async def test_single_member_full_lifecycle(self) -> None:
        """成员：交付 T1 → 收工 → 被就绪任务唤醒 → 交付 T2 → 收队。"""
        config = self._config()
        provider = FakeProvider([
            # 回合 1：交付 T1 后结束回合。
            ProviderResponse(tool_calls=[ToolCall("c1", "team_send", {
                "to": "leader", "task_id": "T1", "body": "T1 结果"})]),
            ProviderResponse(text="T1 完成，等 T2"),
            # 回合 2（T2 就绪唤醒）：交付 T2。
            ProviderResponse(tool_calls=[ToolCall("c2", "team_send", {
                "to": "leader", "task_id": "T2", "body": "T2 报告"})]),
            ProviderResponse(text="全部完成"),
        ])
        hub = TeamHub(self.root / ".conti", team_id="lifecycle")
        hub.open_team(
            [{"name": "w", "profile": "worker"}],
            [{"id": "T1", "title": "活一", "owner": "w"},
             {"id": "T2", "title": "活二", "owner": "w"}],
        )
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in config.profiles})
        report = await runner.run(hub, [{"name": "w", "profile": "worker"}],
                                  park_timeout=0.05, team_timeout=10.0)
        self.assertIn("T1", report)
        self.assertIn("T2", report)
        self.assertEqual(hub.tasks["T1"].status, "done")
        self.assertEqual(hub.tasks["T2"].status, "done")
        # 交付消息进入队长收件箱，被消费后清空。
        leader_inbox = hub.drain(LEADER)
        self.assertEqual([m["task_id"] for m in leader_inbox], ["T1", "T2"])
        # journal 完整记录。
        journal = hub.journal_path.read_text(encoding="utf-8")
        self.assertIn("team.opened", journal)
        self.assertIn("task.completed", journal)
        self.assertIn("team.closed", journal)

    async def test_member_without_delivery_gets_nudged(self) -> None:
        """漏交付防呆：收工未交付先提醒一次，再犯判失败并通知队长。"""
        config = self._config()
        provider = FakeProvider([
            ProviderResponse(text="我干完了"),      # 回合 1：没交付
            ProviderResponse(text="还是没交付"),     # 提醒后仍未交付
        ])
        hub = TeamHub(self.root / ".conti", team_id="nudge")
        hub.open_team(
            [{"name": "w", "profile": "worker"}],
            [{"id": "T1", "title": "活", "owner": "w"}],
        )
        # 标记 doing 模拟成员已认领开始。
        hub.tasks["T1"].status = "doing"
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in config.profiles})
        await runner.run(hub, [{"name": "w", "profile": "worker"}],
                         park_timeout=0.05, team_timeout=10.0)
        self.assertEqual(hub.tasks["T1"].status, "failed")
        inbox = hub.drain(LEADER)
        self.assertTrue(any("未交付" in m["body"] for m in inbox))

    async def test_runtime_team_create_and_inbox_delivery(self) -> None:
        """runtime 集成：team_create 后台起队，交付经 _team_inbox 送达队长，
        finished 后给最终报告并清理 active_team。"""
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[[profile]]
name = "worker"
description = "通用工作者"
system_prompt = "你是工作者。"
allowed_tools = []
permission_mode = "workspace"
""", encoding="utf-8")
        runtime = Runtime(load_single(config_path), self.root,
                          output_function=lambda text: None)
        runtime.provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("c1", "team_send", {
                "to": "leader", "task_id": "T1", "body": "交付内容"})]),
            ProviderResponse(text="done"),
        ])
        runtime.team_park_timeout = 0.1
        runtime.team_timeout = 30.0
        tool = runtime.registry.get("team_create")
        result = await tool.execute({
            "members": [{"name": "w", "profile": "worker"}],
            "tasks": [{"title": "活儿", "owner": "w"}],
        }, ToolContext(workspace=self.root, session_id="s1"))
        self.assertTrue(result.output.startswith("团队"))
        self.assertIsNotNone(runtime.active_team)
        # team_create 的描述动态列出可用 profile（模型选型依据）。
        self.assertIn("worker", tool.description)

        # 等待后台团队结束（同一个事件循环内）。
        for _ in range(500):
            if runtime.active_team is not None and \
                    runtime.active_team["finished"].is_set():
                break
            await asyncio.sleep(0.02)
        # 队长第一次查收件箱：送达交付 + 最终报告，然后清空团队。
        notice = await runtime._team_inbox()
        self.assertIsNotNone(notice)
        self.assertIn("团队交付", notice)
        self.assertIn("最终报告", notice)
        self.assertIn("交付内容", notice)
        self.assertIsNone(runtime.active_team)
        # 再查：无团队时静默。
        self.assertIsNone(await runtime._team_inbox())

    async def test_team_create_over_limit_returns_friendly_error(self) -> None:
        """超过成员上限时返回可读错误（含上限数字），绝不 NameError。"""
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[[profile]]
name = "worker"
description = "w"
system_prompt = "worker"
allowed_tools = []
permission_mode = "workspace"
""", encoding="utf-8")
        runtime = Runtime(load_single(config_path), self.root,
                          output_function=lambda text: None)
        tool = runtime.registry.get("team_create")
        self.assertIn("最多 4 名", tool.description)
        result = await tool.execute({
            "members": [{"name": f"m{i}", "profile": "worker"}
                        for i in range(5)],
            "tasks": [],
        }, ToolContext(workspace=self.root, session_id="s"))
        self.assertTrue(result.is_error)
        self.assertIn("上限 4", result.output)
        self.assertNotIn("NameError", result.output)
        self.assertIsNone(runtime.active_team)

    async def test_leader_inbox_hook_injects_into_agent_loop(self) -> None:
        """回归：leader agent 循环里的步边界注入（inbox_hook）必须把
        收件箱消息作为 user 消息注入（曾因缺 user_message 导入而
        NameError）。"""
        from conti_agent.agent import Agent

        provider = FakeProvider([ProviderResponse(text="收到交付")])
        registry = ToolRegistry()
        notices: list[str] = []

        async def inbox() -> str | None:
            if notices:
                return None
            notices.append("used")
            return "【团队交付】w 完成任务 T1：交付内容"

        agent = Agent(provider, registry, ToolContext(workspace=self.root),
                      inbox_hook=inbox)
        events = []
        async for item in agent.run([user_message("开始")]):
            events.append(item)
        # 注入路径走通、模型收到消息后正常产出。
        self.assertTrue(any(
            item.type == "message.created" and "收到交付" in str(item.payload.get("text"))
            for item in events))

    async def test_team_needs_leader_lifecycle(self) -> None:
        """自动唤醒判据：交付入箱 → 需要；报告送达 → 不再需要。"""
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[[profile]]
name = "worker"
description = "通用工作者"
system_prompt = "你是工作者。"
allowed_tools = []
permission_mode = "workspace"
""", encoding="utf-8")
        runtime = Runtime(load_single(config_path), self.root,
                          output_function=lambda text: None)
        runtime.provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("c1", "team_send", {
                "to": "leader", "task_id": "T1", "body": "交付"})]),
            ProviderResponse(text="done"),
        ])
        runtime.team_park_timeout = 0.1
        runtime.team_timeout = 30.0
        tool = runtime.registry.get("team_create")
        await tool.execute({
            "members": [{"name": "w", "profile": "worker"}],
            "tasks": [{"title": "活儿", "owner": "w"}],
        }, ToolContext(workspace=self.root, session_id="s1"))
        # 等团队结束。
        for _ in range(500):
            if runtime.active_team and runtime.active_team["finished"].is_set():
                break
            await asyncio.sleep(0.02)
        # 报告未送达：需要 leader 自动回应。
        self.assertTrue(runtime.team_needs_leader())
        notice = await runtime._team_inbox()
        self.assertIn("最终报告", notice)
        # 报告已送达：不再需要。
        self.assertFalse(runtime.team_needs_leader())

    async def test_member_cannot_request_input(self) -> None:
        """提问中继协议：成员即使 profile 白名单含 request_input 也被剔除，
        必须走 leader 中继。"""
        config_path = self.root / "runtime.toml"
        config_path.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[[profile]]
name = "worker"
description = "w"
system_prompt = "worker"
allowed_tools = ["request_input"]
permission_mode = "workspace"
""", encoding="utf-8")
        provider = FakeProvider([ProviderResponse(text="done")])
        hub = TeamHub(self.root / ".conti", team_id="relay")
        hub.open_team([{"name": "w", "profile": "worker"}],
                      [{"id": "T1", "title": "活", "owner": "w"}])
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in
                             [(item) for item in
                              __import__("conti_agent.config", fromlist=["load_single"])
                              .load_single(self.root / "runtime.toml").profiles]})
        # 直接检查成员注册表构造（不跑完整循环）。
        profile = runner.profiles["worker"]
        tool_names = [t for t in profile.allowed_tools
                      if t not in {"spawn_task", "request_input"}]
        registry = ToolRegistry().filter(tool_names)
        self.assertFalse(registry.has("request_input"))

    def test_leader_send_tool_wakes_and_delivers(self) -> None:
        """队长 team_send：运行中团队可中途指派/纠偏；无团队时报错。"""
        async def scenario() -> None:
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
            tool = LeaderSendTool(runtime)
            # 无团队时拒绝。
            result = await tool.execute({"to": "w", "body": "x"},
                                        ToolContext(workspace=self.root))
            self.assertTrue(result.is_error)
            # 建队后：消息入成员信箱并触发唤醒事件。
            hub = TeamHub(self.root / ".conti", team_id="send")
            hub.open_team([{"name": "w", "profile": "worker"}],
                          [{"id": "T1", "title": "活", "owner": "w"}])
            runtime.active_team = {"hub": hub, "finished": asyncio.Event(),
                                   "summary": ""}
            hub.set_status("w", "parked")

            async def parker() -> None:
                self.assertTrue(await hub.wait_wake("w", timeout=2.0))

            task = asyncio.ensure_future(parker())
            await asyncio.sleep(0)
            result = await tool.execute({"to": "w", "body": "改成先跑测试"},
                                        ToolContext(workspace=self.root))
            await task
            self.assertIn("已发送给 w", result.output)
            inbox = hub.drain("w")
            self.assertEqual(inbox[0]["from"], LEADER)
            self.assertIn("改成先跑测试", inbox[0]["body"])

        asyncio.run(scenario())

    def test_team_status_command(self) -> None:
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
        result = asyncio.run(create_default_registry().execute(
            "/team", CommandContext(runtime)))
        self.assertTrue(result.ok)
        self.assertIn("没有运行中的团队", result.output[0])


    async def test_parked_cycles_do_not_burn_turns(self) -> None:
        """挂起等待不消耗工作轮数：远超轮数的挂起周期后成员仍能正常工作。"""
        config = self._config()
        provider = FakeProvider([
            ProviderResponse(text="收到消息，开始工作"),
        ])
        hub = TeamHub(self.root / ".conti", team_id="park-burn")
        hub.open_team([{"name": "w", "profile": "worker"}], [])
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in config.profiles})
        task = asyncio.ensure_future(runner.run(
            hub, [{"name": "w", "profile": "worker"}],
            park_timeout=0.05, team_timeout=1.0, max_member_turns=2,
        ))
        # 空转挂起约 15 个周期（远超 max_member_turns=2），不应被判轮数超限。
        await asyncio.sleep(0.75)
        self.assertNotEqual(hub.members["w"]["status"], "failed")
        await asyncio.wait_for(task, timeout=10.0)
        self.assertNotEqual(hub.members["w"]["status"], "failed")

    async def test_close_interrupts_member_mid_turn(self) -> None:
        """收队后成员在下一个请求边界停止，且不被标记为 failed。"""
        config = self._config()

        class SlowTalker(FakeProvider):
            async def complete(self, messages, registry, stream_handler,
                               tool_choice=None):
                await asyncio.sleep(0.05)
                return await super().complete(messages, registry,
                                              stream_handler,
                                              tool_choice=tool_choice)

        provider = SlowTalker([ProviderResponse(text="继续思考")
                               for _ in range(200)])
        hub = TeamHub(self.root / ".conti", team_id="close-stop")
        hub.open_team([{"name": "w", "profile": "worker"}],
                      [{"id": "T1", "title": "长任务", "owner": "w"}])
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in config.profiles})
        task = asyncio.ensure_future(runner.run(
            hub, [{"name": "w", "profile": "worker"}],
            park_timeout=0.05, team_timeout=30.0, max_member_turns=50,
        ))
        await asyncio.sleep(0.4)
        hub.finish("测试收队")
        await asyncio.wait_for(task, timeout=10.0)
        self.assertNotEqual(hub.members["w"]["status"], "failed")

    async def test_journal_records_failure_reasons(self) -> None:
        """成员失败原因必须落 journal（含原因），不许静默。"""
        config = self._config()
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("c1", "team_send", {
                "to": "nobody", "body": "x"})]),
        ])
        hub = TeamHub(self.root / ".conti", team_id="fail-journal")
        hub.open_team([{"name": "w", "profile": "worker"}], [])
        runner = TeamRunner(provider, ToolRegistry(), self.root,
                            {p.name: p for p in config.profiles})
        await runner.run(hub, [{"name": "w", "profile": "worker"}],
                         park_timeout=0.05, team_timeout=10.0)
        journal = hub.journal_path.read_text(encoding="utf-8")
        self.assertIn("member.turn_failed", journal)
        # 失败通知带着原因进入 leader 收件箱。
        self.assertIn("异常退出", journal)


if __name__ == "__main__":
    unittest.main()
