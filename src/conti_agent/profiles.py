from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent import Agent, AgentRunConfig
from .config import ProfileConfig
from .errors import ToolValidationError
from .messages import user_message
from .permissions import PermissionChecker
from .tools import Tool, ToolContext, ToolRegistry, ToolResult


@dataclass
class ProfileRunner:
    provider: Any
    base_registry: ToolRegistry
    workspace: Any
    profiles: list[ProfileConfig]
    permission_mode: str = "workspace"
    max_recursion: int = 2

    def get(self, name: str) -> ProfileConfig:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        raise ToolValidationError(f"未找到 Profile：{name}")

    async def run(self, profile_name: str, task: str, *, parent_context: ToolContext,
                  depth: int = 1) -> str:
        if depth > self.max_recursion:
            raise ToolValidationError("子任务递归深度超过限制")
        profile = self.get(profile_name)
        registry = self.base_registry.filter(
            [name for name in profile.allowed_tools if name != "spawn_task"]
        )
        context = ToolContext(
            workspace=self.workspace,
            session_id=parent_context.session_id,
            task_id=f"{profile_name}:{depth}",
            profile=profile_name,
            services={
                "profile_runner": self,
                "profile_depth": depth,
                "allowed_profiles": [] if not profile.allow_spawn else None,
            },
        )
        checker = PermissionChecker(profile.permission_mode, workspace=self.workspace)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": profile.system_prompt},
            user_message(task),
        ]
        agent = Agent(
            self.provider, registry, context,
            AgentRunConfig(max_tool_iterations=profile.max_tool_iterations),
            permission_checker=checker,
        )
        final_text = ""
        async for item in agent.run(messages):
            if item.type == "message.created" and item.payload.get("text"):
                final_text = str(item.payload["text"])
        return final_text or "子任务结束，但没有返回文本。"


class SpawnTaskTool(Tool):
    """把一个受限 Profile 作为子代理执行，并只返回最终报告。"""

    name = "spawn_task"
    description = "使用指定 Profile 执行一个受限子任务。"
    parameters = {
        "type": "object",
        "properties": {
            "profile": {"type": "string"},
            "task": {"type": "string"},
        },
        "required": ["profile", "task"],
    }
    effects = frozenset({"control"})

    def __init__(self, runner: ProfileRunner) -> None:
        self.runner = runner

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runner = context.services.get("profile_runner", self.runner)
        depth = int(context.services.get("profile_depth", 1))
        profile = runner.get(arguments["profile"])
        if depth >= runner.max_recursion:
            raise ToolValidationError("当前 Profile 不允许继续派生子任务")
        result = await runner.run(
            profile.name, arguments["task"],
            parent_context=context, depth=depth + 1,
        )
        return ToolResult(result, {"profile": profile.name, "depth": depth + 1})
