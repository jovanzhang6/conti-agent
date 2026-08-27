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
