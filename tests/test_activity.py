from __future__ import annotations

import unittest

from conti_agent.activity import format_tool_completed, format_tool_started


class ActivityTestCase(unittest.TestCase):
    def test_read_and_write_are_human_readable(self) -> None:
        self.assertEqual(
            format_tool_started("workspace_read", {"path": "src/main.py"}),
            "开始读取文件 src/main.py",
        )
        self.assertEqual(
            format_tool_completed("workspace_write", {"path": "README.md"}, elapsed=0.2),
            "写入文件 README.md已完成，0.20 秒",
        )

    def test_errors_are_explicit(self) -> None:
        result = format_tool_completed(
            "process_run", {"command": ["python", "--version"]},
            is_error=True, elapsed=0.5,
        )
        self.assertEqual(result, "执行命令 python --version失败，0.50 秒")


if __name__ == "__main__":
    unittest.main()
