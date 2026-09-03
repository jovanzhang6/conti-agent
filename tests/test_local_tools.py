from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from conti_agent.messages import ToolCall
from conti_agent.tools import ToolContext, execute_tool
from conti_agent.tools_local import (
    ProcessRunTool,
    WorkspaceEditTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
    WorkspaceWriteTool,
    create_local_registry,
)
from conti_agent.workspace import Workspace


class LocalToolTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.workspace = Workspace(self.root)
        self.context = ToolContext(workspace=self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    async def call(self, tool, arguments):
        return await execute_tool(
            create_local_registry(self.workspace), ToolCall("t", tool.name, arguments), self.context
        )

    async def test_read_lines_paging(self) -> None:
        """workspace_read 分页：整读保真；部分读取带行号与续读 offset。"""
        lines = [f"line-{i}" for i in range(1, 11)]
        await self.call(WorkspaceWriteTool(self.workspace),
                        {"path": "src/paged.txt", "content": "\n".join(lines) + "\n"})

        # 整读（不指定 offset/limit）：返回原始文本，无行号。
        whole = await self.call(WorkspaceReadTool(self.workspace),
                                {"path": "src/paged.txt"})
        self.assertEqual(whole.output, "\n".join(lines) + "\n")
        self.assertNotIn("     1\t", whole.output)

        # 分页：offset=3 limit=4 → 第 3–6 行，带行号，提示续读。
        page = await self.call(WorkspaceReadTool(self.workspace),
                               {"path": "src/paged.txt", "offset": 3, "limit": 4})
        self.assertIn("     3\tline-3", page.output)
        self.assertIn("     6\tline-6", page.output)
        self.assertNotIn("line-7", page.output)
        self.assertIn("offset=7", page.output)
        self.assertEqual(page.metadata["next_offset"], 7)
        self.assertEqual(page.metadata["total_lines"], 10)

        # 按续读参数读到末尾：不再有续读提示。
        tail = await self.call(WorkspaceReadTool(self.workspace),
                               {"path": "src/paged.txt", "offset": 7, "limit": 4})
        self.assertIn("     7\tline-7", tail.output)
        self.assertIn("line-10", tail.output)
        self.assertIsNone(tail.metadata["next_offset"])

    async def test_read_lines_huge_single_line_advances(self) -> None:
        """单行超过字节上限：返回截断首行，next_offset 指向下一行（防死循环）。"""
        huge_line = "中" * 100_000  # 多字节字符，截断边界落在字符中间
        await self.call(WorkspaceWriteTool(self.workspace),
                        {"path": "src/huge.txt",
                         "content": huge_line + "\n" + "tail\n"})
        page = await self.call(WorkspaceReadTool(self.workspace),
                               {"path": "src/huge.txt", "max_bytes": 1_000})
        self.assertEqual(page.metadata["next_offset"], 2)
        tail = await self.call(WorkspaceReadTool(self.workspace),
                               {"path": "src/huge.txt", "offset": 2,
                                "max_bytes": 1_000})
        self.assertIn("tail", tail.output)

    async def test_read_lines_byte_cap(self) -> None:
        """单行/批量超字节上限：自动收缩并给出续读 offset，不报错。"""
        big_line = "x" * 500
        await self.call(WorkspaceWriteTool(self.workspace),
                        {"path": "src/big.txt",
                         "content": "\n".join(big_line for _ in range(6)) + "\n"})
        page = await self.call(WorkspaceReadTool(self.workspace),
                               {"path": "src/big.txt", "limit": 100,
                                "max_bytes": 800})
        self.assertLessEqual(len(page.output.encode("utf-8")), 800 + 200)
        self.assertIsNotNone(page.metadata["next_offset"])
        self.assertIn("offset=", page.output)

    async def test_read_write_edit(self) -> None:
        written = await self.call(WorkspaceWriteTool(self.workspace),
                                  {"path": "src/a.txt", "content": "hello\n"})
        self.assertEqual(written.metadata["byte_change"], 6)
        read = await self.call(WorkspaceReadTool(self.workspace), {"path": "src/a.txt"})
        self.assertEqual(read.output, "hello\n")
        edited = await self.call(WorkspaceEditTool(self.workspace),
                                 {"path": "src/a.txt", "old": "hello", "new": "world"})
        self.assertEqual(edited.metadata["matches"], 1)
        self.assertEqual(Path(self.root, "src/a.txt").read_text(encoding="utf-8"), "world\n")

    async def test_write_preserves_crlf_and_rejects_ambiguous_edit(self) -> None:
        content = "one\r\ntwo\r\n"
        await self.call(WorkspaceWriteTool(self.workspace), {"path": "crlf.txt", "content": content})
        result = await self.call(WorkspaceEditTool(self.workspace),
                                 {"path": "crlf.txt", "old": "two", "new": "three"})
        self.assertEqual(result.metadata["matches"], 1)
        self.assertEqual(Path(self.root, "crlf.txt").read_bytes(), b"one\r\nthree\r\n")

        ambiguous = await self.call(WorkspaceEditTool(self.workspace),
                                    {"path": "dup.txt", "old": "x", "new": "y"})
        self.assertTrue(ambiguous.is_error)

    async def test_path_traversal_is_rejected(self) -> None:
        result = await self.call(WorkspaceReadTool(self.workspace), {"path": "../outside.txt"})
        self.assertTrue(result.is_error)
        self.assertIn("escapes", result.output)

    async def test_list_and_search_ignore_hidden(self) -> None:
        Path(self.root, "visible.txt").write_text("find me\n", encoding="utf-8")
        Path(self.root, ".hidden.txt").write_text("find me\n", encoding="utf-8")
        Path(self.root, "node_modules").mkdir()
        Path(self.root, "node_modules", "ignore.txt").write_text("find me\n", encoding="utf-8")
        listed = await self.call(WorkspaceListTool(self.workspace), {})
        self.assertIn("visible.txt", listed.output)
        self.assertNotIn(".hidden.txt", listed.output)
        self.assertNotIn("node_modules", listed.output)
        searched = await self.call(WorkspaceSearchTool(self.workspace), {"pattern": "find me"})
        self.assertEqual(searched.metadata["count"], 1)

    async def test_process_timeout(self) -> None:
        if os.name == "nt":
            command = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"]
        else:
            command = ["sleep", "5"]
        result = await self.call(ProcessRunTool(self.workspace),
                                 {"command": command, "timeout_ms": 100})
        self.assertTrue(result.is_error)
        self.assertTrue(result.metadata["timed_out"])

    async def test_process_output_and_env_policy(self) -> None:
        if os.name == "nt":
            command = ["cmd.exe", "/d", "/s", "/c", "echo %SECRET%"]
        else:
            command = ["sh", "-c", "echo $SECRET"]
        os.environ["SECRET"] = "value"
        result = await self.call(ProcessRunTool(self.workspace),
                                 {"command": command, "inherit_env": ["SECRET"]})
        self.assertIn("value", result.output)
        restricted = await self.call(ProcessRunTool(self.workspace),
                                     {"command": command, "inherit_env": []})
        self.assertNotIn("value", restricted.output)


if __name__ == "__main__":
    unittest.main()
