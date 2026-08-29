from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .agent import Agent, AgentRunConfig
from .config import AppConfig, ProviderConfig, load_config
from .commands import create_default_registry
from .context import ContextManager, ResultSpiller, default_summary
from .errors import ConfigurationError, ProviderError
from .events import AgentEvent, event
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

# 上下文超限的识别关键词：命中后静默压缩并重试，不向用户抛错。
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "input tokens",
    "reduce the length",
    "prompt is too long",
    "请求过长",
    "上下文长度",
    "超出上下文",
)

_COMPACT_SYSTEM_PROMPT = (
    "你是会话压缩器。把较早的对话历史压缩成一份摘要，作为后续对话延续上下文的唯一依据。"
    "必须保留：用户的目标与约束、已完成的工作、关键结论与决定、涉及或修改过的文件路径、"
    "当前待办事项、用户的明确偏好。失败过的尝试只保留一句结论。"
    "不要评论、不要提问，用紧凑的中文分节输出（目标/已完成/结论/文件/待办）。"
)


def create_provider(provider: ProviderConfig):
    """把 Provider 配置编译为可注入 transport 的运行时对象。"""
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
        if not config.providers:
            raise ConfigurationError("配置中没有 provider")
        self.provider_configs = {item.name: item for item in config.providers}
        self.provider_config = config.providers[0]
        self.workspace = Workspace(workspace)
        self.root = self.workspace.root / ".conti"
        self.provider = create_provider(self.provider_config)
        self.busy = False
        self.commands = create_default_registry()
        self.registry = create_local_registry(self.workspace)
        self.skill_library = SkillLibrary(self.root / "skills")
        self.registry.register(LoadSkillTool(self.skill_library))
        self.registry.register(TaskNoteTool(self.workspace))
        self.registry.register(RequestInputTool(self._dispatch_input))
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
        self.result_spiller = ResultSpiller(self.root / "spill")
        schema_tokens = sum(len(str(tool.parameters)) // 4 for tool in self.registry.all())
        self.context_manager = ContextManager(
            context_window=self.provider_config.context_window or 128_000,
            max_output_tokens=self.provider_config.max_output_tokens,
            tool_schema_tokens=schema_tokens,
        )
        self._update_context_window(self.provider_config)
        self.input_function = input_function or (lambda question: input(question + "\n> "))
        self.output_function = output_function or (lambda text: print(text, file=sys.stdout))
        # 全屏界面注入的异步提问处理器：request_input 和权限批准都走它，
        # 避免阻塞式 input() 冻结事件循环。
        self.async_input_handler: Callable[..., Any] | None = None

    def _dispatch_input(self, question: str,
                        options: list[str] | None = None) -> Any:
        if self.async_input_handler is not None:
            return self.async_input_handler(question, options)
        if options:
            numbered = "\n".join(f"  {index}) {option}"
                                 for index, option in enumerate(options, 1))
            question = (question + "\n选项：" + numbered
                        + "\n（输入编号或直接输入你的回答）")
        return self.input_function(question)

    async def _approve(self, key: str, arguments: dict[str, Any], reason: str) -> bool:
        """默认交互式批准入口；服务模式可注入显式批准策略。"""
        answer = self._dispatch_input(f"需要批准：{reason}（yes/no）")
        if inspect.isawaitable(answer):
            answer = await answer
        return answer.strip().lower() in {"y", "yes"}

    def register_extra(self, tool) -> None:
        self.registry.register(tool)

    def list_providers(self) -> list[dict[str, Any]]:
        """列出全部 provider；不返回密钥。"""
        return [self.get_provider_info(item.name) for item in self.config.providers]

    def get_provider_info(self, name: str) -> dict[str, Any]:
        config = self.provider_configs.get(name)
        if config is None:
            raise ConfigurationError(f"未知模型：{name}")
        return {
            "name": config.name,
            "protocol": config.protocol,
            "model": config.model,
            "base_url": config.base_url,
            "active": config.name == self.provider_config.name,
            "api_key_ready": bool(config.resolve_api_key()) or config.protocol == "fake",
        }

    def active_provider_name(self) -> str:
        return self.provider_config.name

    def set_active_provider(self, name: str,
                            *, session_id: str | None = None) -> dict[str, Any]:
        """切换 active provider；busy 时禁止，构建成功后才提交。

        返回切换轨迹字典。已有 session 时把 model.switched 事件写入同一账本；
        没有 session（新会话还没发过消息）时创建一个轻量 session 作为磁盘锚点，
        之后的第一条消息会继续写入这个 session。
        """
        if self.busy:
            raise ConfigurationError("当前任务运行中，不能切换模型")
        config = self.provider_configs.get(name)
        if config is None:
            raise ConfigurationError(f"未知模型：{name}。使用 /models 查看可用模型。")
        from_provider = self.provider_config.name
        from_model = self.provider_config.model
        provider = create_provider(config)
        self.provider_config = config
        self.provider = provider
        self.profile_runner.provider = provider
        self._update_context_window(config)
        created_session = False
        if session_id is None:
            session_id, _ = self.sessions.create(
                self.workspace.root,
                title=f"初始模型 {config.name}",
                metadata={"provider": config.name, "model": config.model},
            )
            created_session = True
        else:
            self.sessions.append_model_switch(
                session_id,
                from_provider=from_provider,
                from_model=from_model,
                to_provider=config.name,
                to_model=config.model,
            )
        return {
            "from_provider": from_provider,
            "from_model": from_model,
            "to_provider": config.name,
            "to_model": config.model,
            "session_id": session_id,
            "created_session": created_session,
        }

    def _update_context_window(self, config: ProviderConfig) -> None:
        self.context_manager.context_window = config.context_window or 128_000
        self.context_manager.max_output_tokens = config.max_output_tokens

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

    def load_session_history(self, session_id: str) -> list[dict[str, Any]]:
        """读取一个 session 的完整消息历史，供界面回填显示。"""
        _, messages = self.sessions.load(session_id)
        return messages

    def context_usage_percent(self) -> int:
        """当前上下文用量占窗口的百分比（有精确基线时为精确值）。"""
        window = max(1, self.context_manager.context_window)
        projected = self.context_manager.projected_input_tokens()
        return min(100, round(100 * projected / window))

    def context_usage(self) -> tuple[int, int, int]:
        """(当前上下文 token, 窗口大小, 百分比)。

        注意：这是“下一次请求将携带的上下文”（精确基线 + 增量），
        不是会话的累计消耗——累计值每次请求都会重复计入历史，会远大
        于当前占用。
        """
        window = max(1, self.context_manager.context_window)
        projected = self.context_manager.projected_input_tokens()
        return projected, window, min(100, round(100 * projected / window))

    async def ask(self, prompt: str, *, session_id: str | None = None,
                  output_format: str = "text",
                  text_callback: Callable[[str], None] | None = None,
                  event_callback: Callable[[AgentEvent], None] | None = None) -> tuple[str, str, AsyncIterator[AgentEvent] | list[AgentEvent]]:
        """执行一次任务；返回 final_text、session_id 和事件集合。"""
        self.busy = True
        created = False
        if session_id is None:
            session_id, _ = self.sessions.create(
                self.workspace.root,
                prompt[:60],
                metadata={
                    "provider": self.provider_config.name,
                    "model": self.provider_config.model,
                },
            )
            created = True
        else:
            _, messages = self.sessions.load(session_id)
        if created:
            messages = []
        messages.append(user_message(prompt))
        self.sessions.append_message(session_id, user_message(prompt))
        messages.insert(0, {"role": "system", "content": self._system_prompt()})
        events: list[AgentEvent] = []
        context = ToolContext(workspace=self.workspace.root, session_id=session_id,
                              services={"skill_library": self.skill_library})
        self.result_spiller.reset_round()

        def observe_usage(input_tokens: int, output_tokens: int,
                          covered_count: int) -> None:
            self.context_manager.observe_usage(input_tokens, output_tokens,
                                               covered_count)

        async def pre_request(pending_messages: list[dict[str, Any]], *,
                              force: bool = False, reason: str = "auto") -> None:
            """每次发请求前的统一检查点：投影超限（或强制）就压缩。"""
            pending = pending_messages[self.context_manager.observed_count:]
            if force or self.context_manager.needs_compaction(pending_messages,
                                                              pending):
                events.append(event("context.compacting", reason=reason))
                if await self.compact_messages(pending_messages, session_id,
                                               reason=reason):
                    events.append(event("context.compacted", reason=reason))

        final_text = ""
        for attempt in range(2):
            agent = Agent(
                self.provider, self.registry, context,
                AgentRunConfig(max_tool_iterations=self.config.runtime.max_tool_iterations),
                permission_checker=self.permission_checker,
                auditor=self.auditor,
                session_store=self.sessions,
                session_id=session_id,
                hook_engine=self.hook_engine,
                result_spiller=self.result_spiller,
                usage_observer=observe_usage,
                pre_request_hook=pre_request,
            )
            try:
                async for item in agent.run(messages):
                    if output_format == "jsonl":
                        self.output_function(item.to_json())
                    if item.type == "text.delta" and text_callback and item.payload.get("text"):
                        text_callback(str(item.payload["text"]))
                    if item.type == "usage.recorded":
                        self.context_manager.observe_usage(
                            int(item.payload.get("input_tokens", 0)),
                            int(item.payload.get("output_tokens", 0)),
                        )
                    if event_callback:
                        event_callback(item)
                    events.append(item)
                    if item.type == "message.created" and item.payload.get("text"):
                        final_text = str(item.payload["text"])
                break
            except ProviderError as exc:
                if attempt == 0 and self._is_context_overflow(exc):
                    # 上下文超限：不向用户抛错，静默压缩后重试一次。
                    events.append(event("context.compacting", reason="overflow"))
                    if await self.compact_messages(messages, session_id, reason="overflow"):
                        events.append(event("context.compacted", reason="overflow"))
                    continue
                raise
        try:
            self.sessions.append_message(
                session_id, {"role": "assistant", "content": final_text}
            )
            return final_text, session_id, events
        finally:
            self.busy = False

    @staticmethod
    def _is_context_overflow(exc: ProviderError) -> bool:
        text = str(exc).lower()
        return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)

    async def compact_messages(self, messages: list[dict[str, Any]],
                               session_id: str | None, *,
                               reason: str = "manual") -> str:
        """把较早历史压缩为一条摘要 user 消息，返回摘要文本。

        保留约 10K token 的近期原文；切点回退到 user 边界，保证
        tool_use/tool_result 配对完整。摘要优先由当前模型生成，
        失败时回退为固定规则摘要。
        """
        prefix = [m for m in messages if m.get("role") == "system"]
        body = [m for m in messages if m.get("role") != "system"]
        split = self.context_manager.split_for_compaction(body)
        if split is None:
            return ""
        old, tail = split
        summary_text = await self._summarize_old(old)
        extras = ""
        if session_id:
            extras = (f"[落盘文件目录] {self.result_spiller.directory}\n"
                      f"[会话记录] {self.root / 'sessions' / f'{session_id}.jsonl'}")
        summary_message = {
            "role": "user",
            "content": "[历史摘要]\n" + summary_text + ("\n" + extras if extras else ""),
        }
        messages[:] = [*prefix, summary_message, *tail]
        # 历史被替换，精确基线失效；退化为估算，下次响应后自动恢复。
        self.context_manager.invalidate_baseline()
        if session_id:
            self.sessions.append_compaction(session_id, summary_text, len(old),
                                            summary_message=summary_message)
        return summary_text

    async def _summarize_old(self, old: list[dict[str, Any]]) -> str:
        if not old:
            return "（无可压缩的历史）"
        transcript: list[str] = []
        for message in old:
            content = str(message.get("content") or "")
            calls = message.get("tool_calls")
            line = f"[{message.get('role', '?')}] {content}"
            if calls:
                names = ", ".join(
                    call.get("name", "?") if isinstance(call, dict) else call.name
                    for call in calls
                )
                line += f"（调用工具：{names}）"
            transcript.append(line)
        prompt = (_COMPACT_SYSTEM_PROMPT + "\n\n<较早历史>\n"
                  + "\n".join(transcript) + "\n</较早历史>")
        try:
            response = await self.provider.complete(
                [{"role": "user", "content": prompt}], ToolRegistry(), None
            )
            text = (response.text or "").strip()
            if text:
                return text
        except Exception:
            pass
        return default_summary(old)

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
