from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .agent import Agent, AgentRunConfig
from .config import AppConfig, load_config
from .context import ContextManager
from .errors import ConfigurationError
from .events import AgentEvent
from .messages import user_message
from .permissions import AuditLogger, PermissionChecker, PermissionMode
from .profiles import ProfileRunner, SpawnTaskTool
from .providers import (
    AnthropicCompatibleProvider,
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderResponse,
)
from .sessions import SessionStore
from .skills import SkillLibrary
from .tools import ToolContext, ToolRegistry
from .tools_local import create_local_registry
from .tools_misc import LoadSkillTool, RequestInputTool, TaskNoteTool
from .workspace import Workspace


InputFunction = Callable[[str], str]


def create_provider(config: AppConfig):
    """把 Provider 配置编译为可注入 transport 的运行时对象。"""
    if not config.providers:
        raise ConfigurationError("配置中没有 provider")
    provider = config.providers[0]
    api_key = provider.resolve_api_key()
    if provider.protocol == "fake":
        return FakeProvider([ProviderResponse(
            text="fake provider ready", usage=None,
        )])
    if not api_key:
        raise ConfigurationError(f"环境变量 {provider.api_key_env} 未设置 API Key")
    if provider.protocol == "openai":
        return OpenAICompatibleProvider(
            base_url=provider.base_url, model=provider.model, api_key=api_key,
            max_output_tokens=provider.max_output_tokens,
        )
    if provider.protocol == "anthropic":
        return AnthropicCompatibleProvider(
            base_url=provider.base_url, model=provider.model, api_key=api_key,
            max_output_tokens=provider.max_output_tokens,
        )
    raise ConfigurationError(f"不支持的协议：{provider.protocol}")


class Runtime:
    """CLI、REPL、HTTP 服务共用的运行时门面。"""

    def __init__(self, config: AppConfig, workspace: Path,
                 *, input_function: InputFunction | None = None,
                 output_function: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.workspace = Workspace(workspace)
        self.root = self.workspace.root / ".conti"
        self.provider = create_provider(config)
        self.registry = create_local_registry(self.workspace)
        self.skill_library = SkillLibrary(self.root / "skills")
        self.registry.register(LoadSkillTool(self.skill_library))
        self.registry.register(TaskNoteTool(self.workspace))
        self.registry.register(RequestInputTool(
            input_function or (lambda question: input(question + "\n> "))
        ))
        self.profile_runner = ProfileRunner(
            self.provider,
            self.registry,
            self.workspace.root,
            config.profiles,
            config.runtime.permission_mode,
        )
        self.registry.register(SpawnTaskTool(self.profile_runner))
        self.permission_checker = PermissionChecker(
            config.runtime.permission_mode,
            workspace=self.workspace,
            approver=self._approve,
        )
        self.auditor = AuditLogger(self.root / "runtime" / "audit.jsonl")
        self.sessions = SessionStore(self.root)
        schema_tokens = sum(len(str(tool.parameters)) // 4 for tool in self.registry.all())
        self.context_manager = ContextManager(
            context_window=config.providers[0].context_window or 128_000,
            max_output_tokens=config.providers[0].max_output_tokens,
            tool_schema_tokens=schema_tokens,
        )
        self.input_function = input_function or (lambda question: input(question + "\n> "))
        self.output_function = output_function or (lambda text: print(text, file=sys.stdout))

    async def _approve(self, key: str, arguments: dict[str, Any], reason: str) -> bool:
        """默认交互式批准入口；服务模式可注入显式批准策略。"""
        answer = self.input_function(f"需要批准：{reason}（yes/no）")
        return answer.strip().lower() in {"y", "yes"}

    def register_extra(self, tool) -> None:
        self.registry.register(tool)

    async def ask(self, prompt: str, *, session_id: str | None = None,
                  output_format: str = "text") -> tuple[str, str, AsyncIterator[AgentEvent] | list[AgentEvent]]:
        """执行一次任务；返回 final_text、session_id 和事件集合。"""
        created = False
        if session_id is None:
            session_id, _ = self.sessions.create(self.workspace.root, prompt[:60])
            created = True
        else:
            _, messages = self.sessions.load(session_id)
        if created:
            messages = []
        messages.append(user_message(prompt))
        self.sessions.append_message(session_id, user_message(prompt))
        if self.context_manager.needs_compaction(messages):
            compacted, summary, count = self.context_manager.compact(
                messages, self._default_summarizer
            )
            messages[:] = compacted
            self.sessions.append_compaction(session_id, summary, count)
        context = ToolContext(workspace=self.workspace.root, session_id=session_id,
                              services={"skill_library": self.skill_library})
        agent = Agent(
            self.provider, self.registry, context,
            AgentRunConfig(max_tool_iterations=self.config.runtime.max_tool_iterations),
            permission_checker=self.permission_checker,
            auditor=self.auditor,
            session_store=self.sessions,
            session_id=session_id,
        )
        events: list[AgentEvent] = []
        final_text = ""
        async for item in agent.run(messages):
            if output_format == "jsonl":
                self.output_function(item.to_json())
            events.append(item)
            if item.type == "message.created" and item.payload.get("text"):
                final_text = str(item.payload["text"])
        self.sessions.append_message(session_id, {"role": "assistant", "content": final_text})
        return final_text, session_id, events

    def _default_summarizer(self, old: list[dict[str, Any]], instruction: str) -> str:
        user_goals = [item.get("content", "") for item in old if item.get("role") == "user"]
        return "\n".join([
            "压缩说明：" + instruction,
            "早期用户目标：",
            *[f"- {goal}" for goal in user_goals[-8:]],
        ])
