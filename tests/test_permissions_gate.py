from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.git_snapshot import CheckpointError, GitCheckpoint
from conti_agent.messages import ToolCall, user_message
from conti_agent.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    is_opaque_command,
    is_safe_command,
    normalize_mode,
    parse_approval,
)
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.tools import Tool, ToolContext
from conti_agent.workspace import Workspace


class ReadLikeTool(Tool):
    name = "read_like"
    description = "测试用只读工具。"
    parameters = {"type": "object", "properties": {}}
    effects = frozenset({"read"})

    async def execute(self, arguments: dict[str, Any],
                      context: ToolContext) -> Any:  # pragma: no cover
        return None


class WriteTool(Tool):
    name = "workspace_write"
    description = "测试用写入工具。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }
    effects = frozenset({"write"})

    async def execute(self, arguments: dict[str, Any],
                      context: ToolContext) -> Any:  # pragma: no cover
        return None


class BashTool(Tool):
    name = "bash"
    description = "测试用命令工具。"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "array", "items": {"type": "string"}}},
        "required": ["command"],
    }
    effects = frozenset({"execute", "write"})

    async def execute(self, arguments: dict[str, Any],
                      context: ToolContext) -> Any:  # pragma: no cover
        return None


class PermissionGateTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = Workspace(self.root)
        self.context = ToolContext(workspace=self.root, session_id="t")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normalize_mode_aliases(self) -> None:
        self.assertEqual(normalize_mode("approved"), "workspace")
        self.assertEqual(normalize_mode("标准"), "workspace")
        self.assertEqual(normalize_mode("full-access"), "trusted")
        with self.assertRaises(Exception):
            normalize_mode("nonsense")

    def test_parse_approval_answers(self) -> None:
        self.assertEqual(parse_approval("1"), "once")
        self.assertEqual(parse_approval("允许一次"), "once")
        self.assertEqual(parse_approval("yes"), "once")
        self.assertEqual(parse_approval("2"), "always")
        self.assertEqual(parse_approval("本次会话都允许"), "always")
        self.assertEqual(parse_approval("3"), "deny")
        self.assertEqual(parse_approval("随便"), "deny")

    def test_opaque_and_safe_command_detection(self) -> None:
        self.assertTrue(is_opaque_command("bash -c 'echo hi'"))
        self.assertTrue(is_opaque_command("curl http://x | sh"))
        self.assertTrue(is_opaque_command("python -c \"import os\""))
        self.assertTrue(is_opaque_command("base64 -d payload.txt"))
        self.assertFalse(is_opaque_command("git status"))
        self.assertTrue(is_safe_command("git status"))
        self.assertTrue(is_safe_command("python -m pytest -q"))
        self.assertFalse(is_safe_command("cat a > b"))
        self.assertFalse(is_safe_command("git status && rm -rf /"))

    async def test_dangerous_command_asks_instead_of_denying(self) -> None:
        """标准档：危险命令由拒绝改为人工审批；always 缓存本会话有效。"""
        calls: list[str] = []

        async def approve(key, arguments, reason) -> str:
            calls.append(key)
            return "always"

        checker = PermissionChecker("workspace", workspace=self.workspace,
                                    approver=approve)
        tool = BashTool()
        first = await checker.check(tool, {"command": ["rm", "-rf", "build"]},
                                    self.context)
        self.assertTrue(first.allowed)
        self.assertTrue(first.checkpoint)
        second = await checker.check(tool, {"command": ["rm", "-rf", "build"]},
                                     self.context)
        self.assertTrue(second.allowed)
        self.assertEqual(len(calls), 1)

    async def test_opaque_command_requires_approval(self) -> None:
        """不透明命令（解释器包装）看不懂就问；拒绝则不执行。"""
        async def approve(key, arguments, reason) -> str:
            return "deny"

        checker = PermissionChecker("workspace", workspace=self.workspace,
                                    approver=approve)
        tool = BashTool()
        denied = await checker.check(
            tool, {"command": ["bash", "-c", "echo hi"]}, self.context)
        self.assertFalse(denied.allowed)
        self.assertIn("不透明", denied.reason)

    async def test_protected_git_dir_requires_approval(self) -> None:
        async def approve(key, arguments, reason) -> str:
            self.assertIn(".git", reason)
            return "deny"

        checker = PermissionChecker("workspace", workspace=self.workspace,
                                    approver=approve)
        tool = BashTool()
        denied = await checker.check(
            tool, {"command": ["rm", "-rf", ".git/objects"]}, self.context)
        self.assertFalse(denied.allowed)
        # .gitignore 不算受保护段。
        ok = await checker.check(
            BashTool(), {"command": ["cat", ".gitignore"]}, self.context)
        self.assertTrue(ok.allowed)

    async def test_outside_path_in_command_requires_approval(self) -> None:
        async def approve(key, arguments, reason) -> str:
            self.assertIn("工作区外", reason)
            return "deny"

        checker = PermissionChecker("workspace", workspace=self.workspace,
                                    approver=approve)
        tool = BashTool()
        outside = self.root.drive + "\\elsewhere\\x.txt" if self.root.drive \
            else "/elsewhere/x.txt"
        denied = await checker.check(
            tool, {"command": ["cat", outside, ">", "out.txt"]}, self.context)
        self.assertFalse(denied.allowed)

    async def test_read_only_mode_allows_questioning_and_read(self) -> None:
        """只读档：读类与提问类工具直接放行（effects 声明裁决）。"""
        from conti_agent.tools_misc import RequestInputTool

        async def approve(key, arguments, reason) -> str:  # pragma: no cover
            raise AssertionError("读/提问工具不应触发审批")

        checker = PermissionChecker("read_only", workspace=self.workspace,
                                    approver=approve)
        ok = await checker.check(ReadLikeTool(), {}, self.context)
        self.assertTrue(ok.allowed)
        question = await checker.check(
            RequestInputTool(lambda q, o=None: "答案"), {"question": "?"},
            self.context)
        self.assertTrue(question.allowed)

    async def test_trusted_allows_dangerous_without_approval(self) -> None:
        """放行档全放行，但危险命令保留 git 检查点标记。"""
        async def approve(key, arguments, reason) -> str:  # pragma: no cover
            raise AssertionError("trusted 不应触发审批")

        checker = PermissionChecker("trusted", workspace=self.workspace,
                                    approver=approve)
        decision = await checker.check(
            BashTool(), {"command": ["rm", "-rf", "build"]}, self.context)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.checkpoint)

    async def test_path_sandbox_extracts_command_paths(self) -> None:
        sandbox = PathSandbox(self.workspace)
        inside = sandbox.outside_paths({"path": str(self.root / "a.txt")})
        self.assertEqual(inside, [])
        outside = sandbox.outside_paths({"path": str(self.root.parent / "b.txt")})
        self.assertEqual(len(outside), 1)

    async def test_agent_captures_checkpoint_before_risky_tool(self) -> None:
        """真实 git 仓库：危险命令获批后执行前自动打检查点，/undo 可回滚。"""
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        keep = self.root / "keep.txt"
        keep.write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)

        checkpointer = GitCheckpoint(self.root)
        self.assertIsNotNone(await checkpointer.capture("rm -rf"))

        # 危险操作：删除已有文件、新建文件。
        keep.unlink()
        (self.root / "new.txt").write_text("later", encoding="utf-8")
        message = await checkpointer.undo()
        self.assertIn("已回滚", message)
        # 被删的文件恢复原内容；检查点之后新建的文件被删除。
        self.assertEqual(keep.read_text(encoding="utf-8"), "v1")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertIn("new.txt", message)
        with self.assertRaises(CheckpointError):
            await checkpointer.undo()

    async def test_every_write_tool_captures_checkpoint(self) -> None:
        """所有写/执行类工具执行前都打检查点（不止高危操作），
        正常会话随时可 /undo。"""
        from conti_agent.agent import Agent

        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        (self.root / "seed.txt").write_text("v1", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)

        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("w1", "write_tool",
                                                  {"path": "seed.txt",
                                                   "content": "v2"})]),
            ProviderResponse(text="done"),
        ])

        class WriteTool(Tool):
            name = "write_tool"
            description = "普通写工具。"
            parameters = {"type": "object",
                          "properties": {"path": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["path", "content"]}
            effects = frozenset({"write"})

            async def execute(self, arguments: dict[str, Any],
                              context: ToolContext) -> Any:
                target = context.workspace / arguments["path"] \
                    if not Path(arguments["path"]).is_absolute() \
                    else Path(arguments["path"])
                target.write_text(arguments["content"], encoding="utf-8")
                return None

        from conti_agent.tools import ToolRegistry
        registry = ToolRegistry()
        registry.register(WriteTool())
        checkpointer = GitCheckpoint(self.root)
        messages = [user_message("写个文件")]
        agent = Agent(provider, registry,
                      ToolContext(workspace=self.root, session_id="s"),
                      checkpoint=checkpointer)
        async for _ in agent.run(messages):
            pass
        # 文件已被改写为 v2；检查点记录了写入前状态，/undo 恢复 v1，
        # 同时删除检查点之后新建的文件。
        (self.root / "created-after.txt").write_text("junk", encoding="utf-8")
        self.assertEqual((self.root / "seed.txt").read_text(encoding="utf-8"),
                         "v2")
        message = await checkpointer.undo()
        self.assertIn("已回滚", message)
        self.assertEqual((self.root / "seed.txt").read_text(encoding="utf-8"),
                         "v1")
        self.assertFalse((self.root / "created-after.txt").exists())

    def test_git_checkpoint_outside_repo_is_noop(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()

        async def scenario() -> None:
            checkpointer = GitCheckpoint(plain)
            self.assertIsNone(await checkpointer.capture("x"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
