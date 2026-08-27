from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from conti_agent.config import ProfileConfig, load_single, merge_config
from conti_agent.errors import ToolValidationError
from conti_agent.external import ExternalToolManager
from conti_agent.hooks import HookConfig, HookEngine
from conti_agent.messages import ToolCall, user_message
from conti_agent.profiles import ProfileRunner, SpawnTaskTool
from conti_agent.providers import FakeProvider, ProviderResponse
from conti_agent.skills import SkillLibrary
from conti_agent.tools import Tool, ToolContext, ToolRegistry, ToolResult


class ExtensionsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _register_base_tool(self, registry: ToolRegistry) -> None:
        class ReadTool(Tool):
            name = "workspace_read"
            description = "测试读取工具。"
            parameters = {"type": "object", "properties": {}, "required": []}
            effects = frozenset({"read"})
            async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
                return ToolResult("empty")
        registry.register(ReadTool())

    def write_config(self) -> Path:
        path = self.root / "config.toml"
        path.write_text(r"""
[[provider]]
name = "primary"
protocol = "fake"
base_url = "local://fake"
model = "fake-model"
api_key_env = "CONTI_TEST_KEY"

[runtime]
permission_mode = "workspace"
max_tool_iterations = 7

[[profile]]
name = "reader"
description = "只读调查"
system_prompt = "只读取证据。"
allowed_tools = ["workspace_read"]
permission_mode = "read_only"

[[hook]]
event = "tool.before"
match_tool = "workspace_write"
command = ["python", "-c", "print('{}')"]
""", encoding="utf-8")
        return path

    def test_config_parse_merge_and_secret(self) -> None:
        path = self.write_config()
        config = load_single(path)
        self.assertEqual(config.providers[0].model, "fake-model")
        self.assertEqual(config.runtime.max_tool_iterations, 7)
        self.assertEqual(config.profiles[0].allowed_tools, ["workspace_read"])
        os.environ["CONTI_TEST_KEY"] = "secret"
        self.assertEqual(config.providers[0].resolve_api_key(), "secret")
        local = self.root / "local.toml"
        local.write_text(r"""
[[provider]]
name = "primary"
protocol = "fake"
base_url = "local://override"
model = "override"
""", encoding="utf-8")
        merged = merge_config(config, load_single(local))
        self.assertEqual(merged.providers[0].model, "override")
        self.assertEqual(merged.runtime.max_tool_iterations, 7)

    def test_skill_discovery_and_load(self) -> None:
        directory = self.root / ".conti" / "skills"
        directory.mkdir(parents=True)
        (directory / "release.md").write_text(r"""
---
name = "release"
description = "发布检查清单"
keywords = ["release"]
version = 1
---

1. 运行测试
""", encoding="utf-8")
        library = SkillLibrary(directory)
        skills = library.discover()
        self.assertEqual(skills[0].name, "release")
        self.assertEqual(library.find("release").body.strip(), "1. 运行测试")

    async def test_hook_can_deny(self) -> None:
        hook = HookConfig(
            event="tool.before", match_tool="workspace_write",
            command=[sys.executable, "-c",
                     "import json,sys; json.load(sys.stdin); print(json.dumps({'decision':'deny','message':'blocked'}))"],
        )
        engine = HookEngine([hook])
        outcome = await engine.run("tool.before", "workspace_write", {"path": "a"})
        assert outcome is not None
        self.assertFalse(outcome.allowed)
        self.assertEqual(outcome.message, "blocked")

    async def test_hook_bad_process_denies_by_default(self) -> None:
        hook = HookConfig(event="tool.before", command=[sys.executable, "-c", "raise SystemExit(2)"])
        outcome = await HookEngine([hook]).run("tool.before", "any", {})
        assert outcome is not None
        self.assertFalse(outcome.allowed)

    async def test_profile_spawn_runs_isolated_task(self) -> None:
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("p", "spawn_task",
                                                  {"profile": "reader", "task": "检查"})]),
            ProviderResponse(text="子代理结果"),
            ProviderResponse(text="父代理结果"),
        ])
        base = ToolRegistry()
        self._register_base_tool(base)
        registry = ToolRegistry()
        runner = ProfileRunner(provider, base, self.root, [
            ProfileConfig("reader", "只读", "只读取证据。", ["workspace_read"], "read_only", 2)
        ])
        registry.register(SpawnTaskTool(runner))
        context = ToolContext(workspace=self.root, services={"profile_runner": runner, "profile_depth": 0})
        from conti_agent.tools import execute_tool
        call = ToolCall("p", "spawn_task", {"profile": "reader", "task": "检查"})
        result = await execute_tool(registry, call, context)
        self.assertIn("子代理结果", result.output)

    async def test_profile_unknown_name_fails(self) -> None:
        runner = ProfileRunner(None, ToolRegistry(), self.root, [])
        with self.assertRaises(ToolValidationError):
            runner.get("missing")

    async def test_external_manager_list_and_call(self) -> None:
        class FakeConnector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []
            async def start(self) -> None:
                self.calls.append(("initialize", {}))
            async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((method, params))
                if method == "tools/list":
                    return {"tools": [{"name": "search", "description": "搜索",
                                       "input_schema": {"type": "object"}}]}
                if method == "tools/call":
                    return {"content": "found"}
                return {}
            async def close(self) -> None:
                pass
        connector = FakeConnector()
        manager = ExternalToolManager(connector, "docs")
        await manager.start()
        registry = ToolRegistry()
        await manager.register(registry)
        tool = registry.get("docs.search")
        result = await tool.execute({"q": "x"}, ToolContext(workspace=self.root))
        self.assertEqual(result.output, "found")
        self.assertIn("tools/call", [item[0] for item in connector.calls])


if __name__ == "__main__":
    unittest.main()
