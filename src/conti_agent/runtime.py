from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .agent import Agent, AgentRunConfig
from .config import AppConfig, load_config
from .context import ContextManager
from .errors import ConfigurationError
from .events import AgentEvent
from .external import ExternalToolManager, StdioExternalConnector
from .hooks import HookEngine
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
    if provider.protocol in {"openai", "openai-compat"}:
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
        self.provider_config = config.providers[0]
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
        self.hook_engine = HookEngine(config.hooks, config.hooks_enabled)
        self.external_managers: list[ExternalToolManager] = []
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

    def describe(self) -> dict[str, object]:
        """返回终端界面需要显示的运行时状态，不包含密钥。"""
        return {
            "provider": self.provider_config.name,
            "protocol": self.provider_config.protocol,
            "model": self.provider_config.model,
            "base_url": self.provider_config.base_url,
            "permission_mode": self.config.runtime.permission_mode,
            "workspace": str(self.workspace.root),
            "tools": self.registry.names(),
            "skills_enabled": self.config.skills_enabled,
            "hooks_enabled": self.config.hooks_enabled,
            "profiles_enabled": self.config.profiles_enabled,
            "external_tools_enabled": self.config.external_tools_enabled,
        }

    async def ask(self, prompt: str, *, session_id: str | None = None,
                  output_format: str = "text",
                  text_callback: Callable[[str], None] | None = None) -> tuple[str, str, AsyncIterator[AgentEvent] | list[AgentEvent]]:
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
        messages.insert(0, {"role": "system", "content": self._system_prompt()})
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
            hook_engine=self.hook_engine,
        )
        events: list[AgentEvent] = []
        final_text = ""
        async for item in agent.run(messages):
            if output_format == "jsonl":
                self.output_function(item.to_json())
            if item.type == "text.delta" and text_callback and item.payload.get("text"):
                text_callback(str(item.payload["text"]))
            events.append(item)
            if item.type == "message.created" and item.payload.get("text"):
                final_text = str(item.payload["text"])
        self.sessions.append_message(session_id, {"role": "assistant", "content": final_text})
        return final_text, session_id, events

    async def start_external_tools(self, warn=None) -> None:
        """启动配置中的外部工具服务；单个失败不拖垮主 Runtime。"""
        if not self.config.external_tools_enabled:
            return
        for item in self.config.external_servers:
            manager = ExternalToolManager(
                StdioExternalConnector(item.command, item.env),
                item.name,
            )
            try:
                await manager.start()
                await manager.register(self.registry)
                self.external_managers.append(manager)
            except Exception as exc:
                if warn:
                    warn(f"外部工具服务 {item.name} 启动失败：{exc}")

    async def close_external_tools(self) -> None:
        """停止所有外部工具子进程，避免测试和 CLI 退出时泄漏句柄。"""
        for manager in self.external_managers:
            await manager.close()
        self.external_managers.clear()

    def _default_summarizer(self, old: list[dict[str, Any]], instruction: str) -> str:
        user_goals = [item.get("content", "") for item in old if item.get("role") == "user"]
        return "\n".join([
            "压缩说明：" + instruction,
            "早期用户目标：",
            *[f"- {goal}" for goal in user_goals[-8:]],
        ])

    def _system_prompt(self) -> str:
        """为真实模型生成稳定的行为边界和当前工作区信息。"""
        instruction_path = self.root / "memory" / "instructions.md"
        custom = ""
        if instruction_path.exists():
            try:
                custom = instruction_path.read_text(encoding="utf-8").strip()
            except OSError:
                custom = ""
        return "\n\n".join([
            "你是 conti-agent，一个谨慎的本地编程助手。"
            "回答保持简洁、可执行；修改文件或执行命令前必须说明将做什么。",
            f"当前工作区：{self.workspace.root}",
            f"权限模式：{self.config.runtime.permission_mode}",
            f"可用工具：{', '.join(self.registry.names())}",
            "需要用户澄清时调用 request_input；需要持久化任务信息时调用 task_note。",
            *([f"用户附加指令：\n{custom}"] if custom else []),
        ])
