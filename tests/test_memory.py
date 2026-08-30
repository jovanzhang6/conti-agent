from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.config import load_single
from conti_agent.memory import (
    MemoryEntry,
    MemoryStore,
    merge_findings,
    parse_memory_findings,
    session_to_units,
    split_section,
)
from conti_agent.messages import user_message
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.runtime import Runtime
from conti_agent.tools import Tool, ToolContext
from conti_agent.tools_misc import MemoryWriteTool


def _findings(entries: list[MemoryEntry]) -> dict[str, MemoryEntry]:
    return {entry.id: entry for entry in entries}


class MemoryMergeTestCase(unittest.TestCase):
    def test_merge_new_matches_supersedes(self) -> None:
        entries, notes = merge_findings([], [
            {"action": "new", "section": "用户偏好", "statement": "偏好 4 空格缩进"},
        ], source="dream", today="2026-08-30")
        self.assertEqual(len(entries), 1)
        first_id = entries[0].id
        self.assertTrue(first_id.startswith("P"))

        entries, notes = merge_findings(entries, [
            {"action": "matches", "target": first_id, "section": "用户偏好",
             "statement": "缩进统一用 4 空格"},
        ], source="dream", today="2026-08-31")
        merged = _findings(entries)[first_id]
        self.assertEqual(merged.count, 2)
        self.assertEqual(merged.last_seen, "2026-08-31")
        # matches 命中后同义文本不再新增条目。
        self.assertEqual(len(entries), 1)

        entries, notes = merge_findings(entries, [
            {"action": "supersedes", "target": first_id, "section": "用户偏好",
             "statement": "改为 2 空格缩进"},
        ], source="manual", today="2026-09-01")
        merged = _findings(entries)[first_id]
        self.assertEqual(merged.statement, "改为 2 空格缩进")
        self.assertEqual(merged.count, 1)
        self.assertEqual(len(entries), 1)  # 偏好反转不并存

    def test_merge_hallucinated_target_degrades_to_new(self) -> None:
        entries, _ = merge_findings([], [
            {"action": "matches", "target": "P99", "section": "项目事实",
             "statement": "构建命令是 bun run build"},
        ], source="dream", today="2026-08-30")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "P01")

    def test_merge_duplicate_statements_merge_count(self) -> None:
        entries, _ = merge_findings([], [
            {"action": "new", "section": "用户偏好", "statement": "用户喜欢简洁回复"},
            {"action": "new", "section": "用户偏好", "statement": "用户喜欢简洁回复。"},
        ], source="dream", today="2026-08-30")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].count, 2)

    def test_merge_is_idempotent_for_same_input(self) -> None:
        findings = [{"action": "new", "section": "项目事实",
                     "statement": "测试命令 python -m pytest"}]
        entries, _ = merge_findings([], findings, source="dream",
                                    today="2026-08-30")
        again, _ = merge_findings(entries, findings, source="dream",
                                  today="2026-08-30")
        self.assertEqual(len(again), len(entries))
        self.assertEqual(again[0].count, entries[0].count + 1)

    def test_parse_memory_findings_lines_and_json(self) -> None:
        lines = parse_memory_findings(
            "- [new] 偏好 4 空格\n- [matches:P02] 常用 bun\n- 无\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["action"], "new")
        self.assertEqual(lines[1]["target"], "P02")
        parsed = parse_memory_findings(
            '[{"action": "new", "section": "项目事实", "statement": "X"}]')
        self.assertEqual(parsed[0]["statement"], "X")

    def test_split_section(self) -> None:
        text = "## 关键上下文\nabc\n## 值得长期记住的事\n- [new] A\n- [new] B\n"
        self.assertEqual(split_section(text, "值得长期记住的事"),
                         "- [new] A\n- [new] B")


class MemoryStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / ".conti"
        self.store = MemoryStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_and_manual_reassign(self) -> None:
        entries, _ = merge_findings([], [
            {"action": "new", "section": "用户偏好", "statement": "偏好 4 空格"},
        ], source="dream", today="2026-08-30")
        self.store.save(entries)
        text = self.store.path.read_text(encoding="utf-8")
        self.assertIn("#P01", text)
        self.assertIn("## 用户偏好", text)
        # 人工编辑：删掉 id（手写行）→ 加载时补号，不丢内容。
        broken = text.replace("- #P01 (x1, 2026-08-30, dream) ",
                              "- (x1, 2026-08-30, dream) ")
        self.store.path.write_text(broken, encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "P01")
        self.assertEqual(loaded[0].source, "dream")  # 原始 source 保留
        self.assertIn("偏好 4 空格", loaded[0].statement)

    def test_inject_text_budget(self) -> None:
        entries = [MemoryEntry(f"P{n:02d}", "用户偏好", "描" * 200, source="dream",
                               last_seen="2026-08-30") for n in range(1, 30)]
        self.store.save(entries)
        text = self.store.inject_text(budget=800)
        self.assertIn("已截断", text)
        self.assertLessEqual(len(text), 900)
        # 空记忆不注入。
        empty = MemoryStore(self.root.parent / "other")
        self.assertEqual(empty.inject_text(), "")

    def test_session_to_units(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "答" * 500},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "tool", "content": "x" * 50_000},
            {"role": "user", "content": "[历史摘要]\n压缩"},
            {"role": "user", "content": "贴" * 3_000},
        ]
        units = session_to_units(messages)
        self.assertEqual(len(units), 2)
        # 用户消息与它得到的助手回复配对；工具结果不进入单元。
        self.assertEqual(units[0]["user"], "第一问")
        self.assertEqual(units[0]["assistant"], "答" * 400)
        self.assertNotIn("x" * 100, units[1]["user"])
        self.assertIn("省略", units[1]["user"])
        self.assertEqual(units[1]["assistant"], "")


class MemoryIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runtime(self, provider) -> Runtime:
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
        runtime.provider = provider
        return runtime

    async def test_memory_write_tool(self) -> None:
        runtime = self._runtime(FakeProvider([]))
        tool = MemoryWriteTool(runtime.memory)
        result = await tool.execute(
            {"statement": "记住：部署用 bun", "section": "项目事实"},
            ToolContext(workspace=self.root, session_id="s"),
        )
        self.assertIn("已写入长期记忆", result.output)
        entries = runtime.memory.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].statement, "记住：部署用 bun")
        # 写入后注入下个会话的 system prompt。
        self.assertIn("bun", runtime._system_prompt())

    async def test_compaction_piggybacks_memory_extraction(self) -> None:
        summary = ("<compacted-summary>\n## 目标与约束\nx\n"
                   "## 值得长期记住的事\n- [new] 用户偏好 bun 而非 npm\n"
                   "</compacted-summary>")
        runtime = self._runtime(FakeProvider([ProviderResponse(text=summary,
                                                              usage=None)]))
        long = "y" * 400
        messages = [
            {"role": "user", "content": "u1 " + long},
            {"role": "assistant", "content": "a1 " + long},
            {"role": "user", "content": "u2 " + long},
        ]
        await runtime.compact_messages(messages, None, reason="auto")
        entries = runtime.memory.load()
        self.assertEqual(len(entries), 1)
        self.assertIn("bun 而非 npm", entries[0].statement)
        self.assertEqual(entries[0].source, "compaction")

    async def test_run_dream_uses_cursor_and_merges(self) -> None:
        runtime = self._runtime(FakeProvider([
            ProviderResponse(
                text='[{"action": "new", "section": "用户偏好", '
                     '"statement": "用户偏好 CJK 感知估算"}]',
                usage=None),
        ]))
        store = runtime.sessions
        session_id, _ = store.create(self.root, "dream 会话")
        store.append_message(session_id, user_message("测" * 2_100))
        store.append_message(session_id, user_message("再聊" * 700))
        processed = await runtime.run_dream()
        self.assertEqual(processed, 1)
        entries = runtime.memory.load()
        self.assertEqual(len(entries), 1)
        self.assertIn("CJK", entries[0].statement)
        # 游标推进：立刻重跑不会再次提炼（mtime 未变且游标已推进）。
        processed = await runtime.run_dream()
        self.assertEqual(processed, 0)
        self.assertEqual(len(runtime.memory.load()), 1)


if __name__ == "__main__":
    unittest.main()
