from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.commands import CommandContext, create_default_registry
from conti_agent.config import load_single
from conti_agent.messages import ToolCall
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.runtime import Runtime
from conti_agent.team import LEADER, TeamHub, TeamRunner
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
             {"id": "T2", "title": "写报告", "owner": "writer",
              "depends_on": ["T1"]}],
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

    async def test_dependency_unlock_notifies_owner(self) -> None:
        self.assertEqual(self.hub.tasks["T2"].status, "waiting")
        self.hub.complete_task("T1", "调研完成", "scout")
        self.assertEqual(self.hub.tasks["T2"].status, "todo")
        inbox = self.hub.drain("writer")
        self.assertTrue(any("T1 已完成" in m["body"] for m in inbox))

    async def test_wake_event(self) -> None:
        self.hub.set_status("writer", "parked")

        async def parker() -> None:
            self.assertTrue(await self.hub.wait_wake("writer", timeout=2.0))

        task = asyncio.ensure_future(parker())
        await asyncio.sleep(0)
        self.hub.send("scout", "writer", "醒了")
        await task
        self.hub.set_status("writer", "running")


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


if __name__ == "__main__":
    unittest.main()
