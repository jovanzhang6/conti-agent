from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    usage: str
    arguments: tuple[str, ...] = ()
    requires_idle: bool = False

    @property
    def full_name(self) -> str:
        return f"/{self.name}"


@dataclass
class CommandResult:
    output: list[str] = field(default_factory=list)
    status: str = "ok"
    exit_requested: bool = False
    clear_requested: bool = False
    new_session_requested: bool = False
    session_id: str | None = None
    panel_action: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class CommandContext:
    runtime: Any
    session_id: str | None = None
    compact_session: Callable[[str], Awaitable[str]] | None = None
    activity_provider: Callable[[], list[str]] | None = None


@dataclass(frozen=True)
class CommandSuggestion:
    value: str
    description: str = ""


Handler = Callable[[CommandContext, list[str]], Awaitable[CommandResult]]


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._handlers: dict[str, Handler] = {}

    def register(self, spec: CommandSpec, handler: Handler) -> None:
        if spec.name in self._commands:
            raise ValueError(f"重复命令：/{spec.name}")
        self._commands[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> list[CommandSpec]:
        return sorted(self._commands.values(), key=lambda item: item.name)

    def get(self, name: str) -> CommandSpec | None:
        return self._commands.get(name.lstrip("/").lower())

    def is_command(self, prompt: str) -> bool:
        return prompt.lstrip().startswith("/")

    def suggest(self, prompt: str,
                context: CommandContext | None = None) -> list[CommandSuggestion]:
        text = prompt.lstrip()
        if not text.startswith("/"):
            return []
        parts = text[1:].split(" ", 1)
        name = parts[0].lower() if parts else ""
        if not name:
            return [CommandSuggestion(f"/{item.name}", item.description)
                    for item in self.specs()]
        if len(parts) == 1:
            return [CommandSuggestion(f"/{item.name}", item.description)
                    for item in self.specs()
                    if item.name.startswith(name)]
        spec = self.get(name)
        if spec is None or not spec.arguments:
            return []
        suggestions = self._argument_suggestions(spec, spec.arguments[0], context)
        value = parts[1]
        return [item for item in suggestions if item.value.startswith(value)]

    def _argument_suggestions(self, spec: CommandSpec, argument: str,
                              context: CommandContext | None) -> list[CommandSuggestion]:
        if context is None or context.runtime is None:
            return [CommandSuggestion(f"<{argument}>", spec.description)]
        if argument == "name":
            return [CommandSuggestion(item["name"],
                                      f"{item['model']} · {item['protocol']}")
                    for item in context.runtime.list_providers()]
        if argument == "session-id":
            return [CommandSuggestion(item["session_id"], item.get("title", ""))
                    for item in context.runtime.sessions.list()]
        return []

    async def execute(self, prompt: str, context: CommandContext) -> CommandResult:
        if not self.is_command(prompt):
            raise ValueError("不是 Slash 命令")
        try:
            parts = shlex.split(prompt.strip())
        except ValueError:
            parts = prompt.strip().split()
        name = parts[0][1:].lower()
        arguments = parts[1:]
        spec = self.get(name)
        if spec is None:
            return CommandResult(
                [f"未知命令：/{name}。输入 /help 查看可用命令。"], status="error"
            )
        if spec.requires_idle and getattr(context.runtime, "busy", False):
            return CommandResult(
                [f"当前任务运行中，不能使用 {spec.full_name}。"], status="error"
            )
        if len(arguments) < len(spec.arguments):
            argument = spec.arguments[0] if spec.arguments else "参数"
            return CommandResult(
                [f"用法：{spec.usage}。缺少 <{argument}>。"], status="error"
            )
        result = self._handlers[name](context, arguments)
        if hasattr(result, "__await__"):
            result = await result
        return result


def create_default_registry() -> CommandRegistry:
    registry = CommandRegistry()

    def help_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        lines = ["可用命令："]
        lines.extend(f"{item.usage} — {item.description}" for item in context.runtime.commands.specs())
        return CommandResult(lines)

    def models_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        providers = context.runtime.list_providers()
        lines = ["可用模型："]
        for item in providers:
            marker = "●" if item["active"] else "○"
            key_state = "Key ready" if item["api_key_ready"] else "Key missing"
            lines.append(f"{marker} {item['name']} — {item['model']} · {item['protocol']} · {key_state}")
        return CommandResult(lines, data={"providers": providers})

    def model_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        name = arguments[0]
        current = context.runtime.active_provider_name()
        if name == current:
            item = context.runtime.get_provider_info(name)
            return CommandResult([f"当前模型已经是 {name}（{item['model']}）。"])
        try:
            context.runtime.set_active_provider(name)
            item = context.runtime.get_provider_info(name)
        except Exception as exc:
            return CommandResult([f"模型切换失败：{exc}"], status="error")
        return CommandResult(
            [f"模型已切换为 {item['name']}（{item['model']}）。"],
            data={"provider": item},
        )

    def status_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        info = context.runtime.describe()
        return CommandResult([f"{key}: {value}" for key, value in info.items()],
                             data={"runtime": info})

    def activity_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        if context.activity_provider is None:
            return CommandResult(["当前界面没有活动记录。"])
        items = context.activity_provider()
        if not items:
            return CommandResult(["暂无活动记录。"])
        return CommandResult(["当前活动：", *items[-20:]], data={"activity": items})

    def sessions_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        sessions = context.runtime.sessions.list()
        if not sessions:
            return CommandResult(["还没有保存的会话。"])
        lines = ["最近会话："]
        lines.extend(f"{item['session_id']}  {item['title']}" for item in sessions[-20:])
        return CommandResult(lines, data={"sessions": sessions})

    async def resume_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        session_id = arguments[0]
        context.runtime.sessions.load(session_id)
        context.session_id = session_id
        return CommandResult(
            [f"已恢复会话 {session_id}。历史消息回填将在后续提交提供。"],
            session_id=session_id,
        )

    async def compact_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        if not context.session_id:
            return CommandResult(["当前还没有可压缩的磁盘会话。"], status="error")
        if context.compact_session is None:
            return CommandResult(["当前界面不支持压缩会话。"], status="error")
        summary = await context.compact_session(context.session_id)
        return CommandResult([f"历史已压缩。摘要字数：{len(summary)}。"])

    def new_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        context.session_id = None
        return CommandResult(["已开启新会话。"], new_session_requested=True, session_id=None)

    def clear_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        return CommandResult(["屏幕显示已清除。"], clear_requested=True)

    def panel_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        return CommandResult(["状态面板已切换。"], panel_action="toggle")

    def exit_handler(context: CommandContext, arguments: list[str]) -> CommandResult:
        return CommandResult(["再见。"], exit_requested=True)

    commands = (
        (CommandSpec("help", "显示命令帮助", "/help"), help_handler),
        (CommandSpec("models", "列出可用模型", "/models"), models_handler),
        (CommandSpec("model", "切换模型", "/model <name>", ("name",), True), model_handler),
        (CommandSpec("status", "显示运行时状态", "/status"), status_handler),
        (CommandSpec("sessions", "列出保存的会话", "/sessions"), sessions_handler),
        (CommandSpec("resume", "恢复会话", "/resume <session-id>", ("session-id",), True), resume_handler),
        (CommandSpec("compact", "压缩当前历史", "/compact", (), True), compact_handler),
        (CommandSpec("new", "开启新会话", "/new", (), True), new_handler),
        (CommandSpec("activity", "查看完整活动", "/activity"), activity_handler),
        (CommandSpec("panel", "切换状态面板", "/panel"), panel_handler),
        (CommandSpec("clear", "清除屏幕显示", "/clear", (), True), clear_handler),
        (CommandSpec("exit", "退出程序", "/exit", (), True), exit_handler),
    )
    for spec, handler in commands:
        registry.register(spec, handler)
    return registry
