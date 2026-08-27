from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conti_agent.cli import run_chat, run_cli
from conti_agent.collab import CollaborationError, CrewManager
from conti_agent.runtime import Runtime
from conti_agent.service import RuntimeService, ServiceRequestError
from conti_agent.snapshots import SnapshotError, SnapshotManager


class IntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self.config = self.root / "config.toml"
        self.config.write_text(r"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake-model"
""", encoding="utf-8")
        self.outputs: list[str] = []
        self.inputs = iter(["/exit"])

    def tearDown(self) -> None:
        self._temporary.cleanup()

    async def test_cli_ask_sessions_and_config_check(self) -> None:
        code = await run_cli(["--config", str(self.config), "ask", "你好"],
                             workspace=self.root,
                             output_function=self.outputs.append)
        self.assertEqual(code, 0)
        self.assertIn("fake provider ready", self.outputs)
        code = await run_cli(["--config", str(self.config), "sessions"],
                             workspace=self.root, output_function=self.outputs.append)
        self.assertEqual(code, 0)
        self.assertTrue(any("  " in line for line in self.outputs))
        code = await run_cli(["--config", str(self.config), "config-check"],
                             workspace=self.root, output_function=self.outputs.append)
        self.assertEqual(code, 0)

    async def test_cli_jsonl_and_invalid_config(self) -> None:
        code = await run_cli(["--config", str(self.config), "ask", "你好",
                              "--event-format", "jsonl"],
                             workspace=self.root, output_function=self.outputs.append)
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith('{"event":') for line in self.outputs))
        missing = self.root / "missing.toml"
        errors: list[str] = []
        code = await run_cli(["--config", str(missing), "ask", "x"],
                             workspace=self.root, error_function=errors.append)
        self.assertEqual(code, 2)

    async def test_repl_exit_sessions_and_compact(self) -> None:
        runtime = Runtime(load_config := __import__(
            "conti_agent.config", fromlist=["load_single"]
        ).load_single(self.config), self.root, output_function=self.outputs.append)
        session_id, _ = runtime.sessions.create(runtime.workspace.root, "existing")
        inputs = iter(["/sessions", "/compact", "/exit"])
        await run_chat(runtime, session_id, lambda prompt: next(inputs), self.outputs.append)
        self.assertTrue(any("existing" in line for line in self.outputs))
        self.assertTrue(any("已压缩历史" in line for line in self.outputs))

    def test_crew_board_and_mailbox(self) -> None:
        root = self.root / "crew"
        crew = CrewManager(root, "release")
        crew.create_task("t1", "检查发布", "lead")
        crew.send("m1", "lead", "worker", "开始")
        self.assertEqual(crew.get_task("t1").status, "todo")
        crew.update_task("t1", status="done", result="完成")
        drained = crew.drain("worker")
        self.assertEqual(len(drained), 1)
        self.assertEqual(CrewManager(root, "release").get_task("t1").result, "完成")
        with self.assertRaises(CollaborationError):
            crew.get_task("missing")

    async def test_snapshot_requires_git_and_rejects_missing(self) -> None:
        manager = SnapshotManager(self.root)
        with self.assertRaises(SnapshotError):
            await manager.create("not-a-repo")

    async def test_service_validation_and_submission(self) -> None:
        runtime = Runtime(__import__("conti_agent.config", fromlist=["load_single"]).load_single(
            self.config
        ), self.root, output_function=self.outputs.append)
        service = RuntimeService(runtime)
        with self.assertRaises(ServiceRequestError):
            service.validate_submission({"prompt": ""})
        result = await service.submit({"prompt": "服务请求"})
        self.assertEqual(result["result"], "fake provider ready")
        self.assertTrue(result["events"])


if __name__ == "__main__":
    unittest.main()
