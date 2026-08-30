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
        self.assertEqual(config.providers[0].protocol, "fake")
        path.write_text(path.read_text(encoding="utf-8").replace(
            'protocol = "fake"', 'protocol = "openai-compat"'
        ), encoding="utf-8")
        alias_config = load_single(path)
        self.assertEqual(alias_config.providers[0].protocol, "openai-compat")
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

    def test_local_config_allows_direct_api_key(self) -> None:
        path = self.root / "local-secret.toml"
        path.write_text(r"""
[[provider]]
name = "local"
protocol = "openai-compat"
base_url = "https://example.com"
model = "m"
api_key = "local-only-key"
""", encoding="utf-8")
        config = load_single(path)
        self.assertEqual(config.providers[0].resolve_api_key(), "local-only-key")

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

    def _skill_runtime(self, skills_enabled: bool = True):
        """构造带可选 skill 开关的 Runtime，并预置一个 release skill。"""
        from conti_agent.runtime import Runtime
        directory = self.root / ".conti" / "skills"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "release.md").write_text(
            '---\nname = "release"\ndescription = "发布检查清单"\n'
            'keywords = ["release"]\nversion = 1\n---\n\n1. 运行测试\n',
            encoding="utf-8",
        )
        config_path = self.root / "runtime.toml"
        config_path.write_text(f"""
[[provider]]
name = "fake"
protocol = "fake"
base_url = "local://fake"
model = "fake"
[runtime]
permission_mode = "workspace"
[extensions]
skills = {str(skills_enabled).lower()}
""", encoding="utf-8")
        return Runtime(load_single(config_path), self.root,
                       output_function=lambda text: None)

    def test_skill_catalog_injected_into_system_prompt(self) -> None:
        runtime = self._skill_runtime()
        prompt = runtime._system_prompt()
        self.assertIn("release", prompt)
        self.assertIn("发布检查清单", prompt)
        self.assertIn("load_skill", prompt)
        self.assertIn("load_skill", runtime.registry.names())

    def test_skills_disabled_skips_registration_and_injection(self) -> None:
        runtime = self._skill_runtime(skills_enabled=False)
        self.assertNotIn("load_skill", runtime.registry.names())
        self.assertNotIn("已安装 Skill", runtime._system_prompt())
        self.assertNotIn("release", runtime._system_prompt())

    def test_skill_catalog_truncated_to_budget(self) -> None:
        runtime = self._skill_runtime()
        # 塞入超预算的长描述，验证截断标记。
        directory = self.root / ".conti" / "skills"
        for index in range(10):
            (directory / f"bulk{index}.md").write_text(
                '---\nname = "bulk' + str(index) + '"\ndescription = "长'
                + "描" * 400 + '"\nversion = 1\n---\n\n正文\n',
                encoding="utf-8",
            )
        prompt = runtime._system_prompt()
        self.assertIn("已截断", prompt)

    def test_skills_command_lists_library(self) -> None:
        import asyncio
        from conti_agent.commands import CommandContext, create_default_registry
        runtime = self._skill_runtime()
        registry = create_default_registry()
        result = asyncio.run(
            registry.execute("/skills", CommandContext(runtime)))
        self.assertTrue(result.ok)
        self.assertIn("release：发布检查清单", "\n".join(result.output))


if __name__ == "__main__":
    unittest.main()
