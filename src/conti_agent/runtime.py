from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .agent import Agent, AgentRunConfig
from .config import AppConfig, ProviderConfig, load_config
from .commands import create_default_registry
from .context import (
    ContextManager,
    ResultSpiller,
    default_summary,
    estimate_message_tokens,
)
from .errors import ConfigurationError, ProviderError, ToolValidationError
from .events import AgentEvent, event
from .external import ExternalToolManager, StdioExternalConnector
from .git_snapshot import GitCheckpoint
from .hooks import HookEngine
from .memory import (
    MemoryStore,
    merge_findings,
    parse_memory_findings,
    session_to_units,
    split_section,
)
from .messages import user_message
from .permissions import (
    AuditLogger,
    PermissionChecker,
    PermissionMode,
    normalize_mode,
    parse_approval,
)
from .profiles import ProfileRunner, SpawnTaskTool
from .providers import (
    AnthropicCompatibleProvider,
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderResponse,
)
from .sessions import SessionStore
from .skills import SkillLibrary
from .team import LEADER, LeaderSendTool, TeamHub, TeamRunner
from .tools import Tool, ToolContext, ToolResult
from .tools_local import create_local_registry
from .tools_misc import LoadSkillTool, MemoryWriteTool, RequestInputTool
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

# 压缩指令：作为摘要请求的最后一条 user 消息（前面重放真实 system +
# 被压缩旧消息原文，与主请求构成公共前缀命中 KV cache——见 HIGHLIGHTS 1.3.A）。
_COMPACTION_INSTRUCTION = (
    "请把本次请求携带的全部较早历史压缩为一份摘要，作为后续对话延续上下文的唯一依据。\n"
    "要求：\n"
    "1. 输出整体包裹在 <compacted-summary> 与 </compacted-summary> 标签中；\n"
    "2. 标签内按顺序使用以下七个固定 Markdown 小节：\n"
    "## 目标与约束\n"
    "## 关键决策\n"
    "## 文件与代码\n"
    "## 错误与修复\n"
    "## 当前进度\n"
    "## 下一步\n"
    "## 关键上下文\n"
    "3. 保留精确信息：文件路径、函数与标识符名、命令、错误信息原文、数值；\n"
    "4. 若较早历史中已包含带 <compacted-summary> 标签的旧摘要，将其视为前次摘要"
    "做增量合并：对话内容与旧摘要冲突时以对话为准，旧摘要中仍相关的信息不得丢失；\n"
    "5. 失败过的尝试只保留一句结论；不要评论、不要提问、不要提及压缩行为本身。"
)

# 压缩顺带提炼（HIGHLIGHTS 2.3 第二层）：与压缩共用同一次模型调用。
# 记忆索引只追加在末尾 user 消息里，不影响与主请求的公共前缀。
_MEMORY_SECTION_HEADING = "值得长期记住的事"


def _memory_addon(index_text: str) -> str:
    lines = [
        "6. 若较早历史中存在值得长期记住的长期偏好或项目事实（不是一次性的"
        "任务细节），可在七个固定小节之后追加一节 \"## " + _MEMORY_SECTION_HEADING
        + "\"，最多 3 行，每行以 [new]、[matches:Pxx] 或 [supersedes:Pxx] 开头"
        "（new=新记忆，matches=与已有记忆同义，supersedes=覆盖已有记忆）；"
        "没有可提炼的就写\"无\"。",
    ]
    if index_text:
        lines.append(f"<当前记忆索引>\n{index_text}\n</当前记忆索引>")
    return "\n".join(lines)


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


class TeamCreateTool(Tool):
    """队长侧：组建团队并后台运行（进度经收件箱自动送达对话）。"""

    name = "team_create"
    parameters = {
        "type": "object",
        "properties": {
            "members": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "profile": {"type": "string"},
                    },
                    "required": ["name", "profile"],
                },
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "owner": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "owner"],
                },
            },
        },
        "required": ["members", "tasks"],
    }
    effects = frozenset({"control"})

    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        catalog = "；".join(
            f"{profile.name}（{profile.description or '无描述'}）"
            for profile in runtime.config.profiles
        )
        self.description = (
            "组建 agent 团队并行协作（异步运行：进度、交付与最终报告会自动"
            "送达你的对话，无需轮询；成员最多 4 名，超出请分批建队）。"
            "可用 profile："
            + (catalog or "（尚未配置任何 profile）")
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self.runtime
        if runtime.active_team is not None:
            return ToolResult("已有团队在运行，先用 team_close 收队。", is_error=True)
        hub = TeamHub(runtime.root)
        try:
            hub.open_team(arguments["members"], arguments.get("tasks") or [])
        except ToolValidationError as exc:
            return ToolResult(str(exc), is_error=True)
        runtime.active_team = {"hub": hub, "finished": asyncio.Event(), "summary": ""}
        runtime._notify_team_notice()
        # 建队即动效：明确告知启动了几个子 agent、都是谁。
        if runtime.on_team_notice:
            names = "、".join(str(m["name"]) for m in arguments["members"])
            try:
                runtime.on_team_notice(
                    f"已启动 {len(arguments['members'])} 个子 agent：{names}")
            except Exception:
                pass

        async def background() -> None:
            try:
                summary = await runtime.team_runner.run(
                    hub, arguments["members"],
                    provider=runtime.provider,
                    park_timeout=runtime.team_park_timeout,
                    team_timeout=runtime.team_timeout,
                )
                runtime.active_team["summary"] = summary
            except Exception as exc:
                if runtime.active_team is not None:
                    runtime.active_team["summary"] = f"团队异常终止：{exc}"
                    runtime.active_team["finished"].set()
            else:
                if runtime.active_team is not None:
                    runtime.active_team["finished"].set()

        asyncio.create_task(background())
        return ToolResult(
            f"团队 {hub.team_id} 已建立并开始运行（{hub.board_digest()}）。"
            "交付与进度会自动送达；全部完成后你会收到最终报告。"
        )


class TeamCloseTool(Tool):
    """队长侧：主动收队。"""

    name = "team_close"
    description = "收队：终止团队所有成员并给出最终报告。"
    parameters = {"type": "object", "properties": {}}
    effects = frozenset({"control"})

    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        team = self.runtime.active_team
        if team is None:
            return ToolResult("当前没有运行中的团队。")
        team["hub"].finish("leader 主动收队")
        report = team["summary"] or team["hub"].final_report()
        self.runtime.active_team = None
        return ToolResult("团队已收队。\n" + report)


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
        # 压缩进行中标志：手动 /compact 的拒绝依据，TUI 拦截新输入的依据。
        self.compacting = False
        self.commands = create_default_registry()
        self.registry = create_local_registry(self.workspace)
        self.skill_library = SkillLibrary(self.root / "skills")
        # skills_enabled 真实生效：关闭时不注册 load_skill、不注入目录。
        if config.skills_enabled:
            self.registry.register(LoadSkillTool(self.skill_library))
        self.memory = MemoryStore(self.root)
        self.registry.register(MemoryWriteTool(self.memory))
        self.registry.register(RequestInputTool(self._dispatch_input))
        self.profile_runner = ProfileRunner(
            self.provider,
            self.registry,
            self.workspace.root,
            config.profiles,
            config.runtime.permission_mode,
        )
        self.registry.register(SpawnTaskTool(self.profile_runner))
        # Agent Team（HIGHLIGHTS 亮点 5）：队长侧工具 + 后台团队状态。
        self.team_runner = TeamRunner(
            self.provider, self.registry, self.workspace.root,
            {profile.name: profile for profile in config.profiles},
        )
        self.active_team: dict[str, Any] | None = None
        # 团队成员挂起/全队超时（测试与高级用户可调小）。
        self.team_park_timeout = 120.0
        self.team_timeout = 1_800.0
        # leader 空闲时的被动通知（TUI 注入显示，不启动新回合）。
        self.on_team_notice: Callable[[str], None] | None = None
        if config.collaboration_enabled:
            self.registry.register(TeamCreateTool(self))
            self.registry.register(TeamCloseTool(self))
            self.registry.register(LeaderSendTool(self))
        self.permission_checker = PermissionChecker(
            config.runtime.permission_mode,
            workspace=self.workspace,
            approver=self._approve,
        )
        self.hook_engine = HookEngine(config.hooks, config.hooks_enabled)
        self.external_managers: list[ExternalToolManager] = []
        self.auditor = AuditLogger(self.root / "runtime" / "audit.jsonl")
        self.checkpoint = GitCheckpoint(self.workspace.root)
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

    async def _approve(self, key: str, arguments: dict[str, Any], reason: str) -> str:
        """三选项审批入口（允许一次/本会话都允许/拒绝）；服务模式可注入。"""
        answer = self._dispatch_input(
            f"需要批准：{reason}", ["允许一次", "本次会话都允许", "拒绝"]
        )
        if inspect.isawaitable(answer):
            answer = await answer
        return parse_approval(str(answer))

    def get_permission_mode(self) -> str:
        return self.config.runtime.permission_mode

    def set_permission_mode(self, mode: str) -> str:
        """切换权限档位（接受历史别名）；切换立即生效。"""
        normalized = normalize_mode(mode)
        self.config.runtime.permission_mode = normalized
        self.permission_checker.mode = PermissionMode(normalized)
        return normalized

    async def undo_last(self) -> str:
        """回滚到最近的 git 检查点。"""
        return await self.checkpoint.undo()

    # ---------- Agent Team：队长收件箱与状态（HIGHLIGHTS 亮点 5） ----------

    def _notify_team_notice(self) -> None:
        """给当前团队挂接被动通知回调（建队时调用）。"""
        hub = self.active_team["hub"] if self.active_team else None
        if hub is None:
            return

        def on_message(message: dict[str, Any]) -> None:
            if self.on_team_notice is None:
                return
            label = ("交付" if message.get("task_id")
                     else "消息" if message.get("type") == "chat" else "通知")
            body = str(message.get("body", ""))[:160]
            try:
                self.on_team_notice(f"{message['from']} {label}：{body}")
            except Exception:
                pass

        # 成员状态动效：建队/交付/挂起/退出即时显示在活动行。
        def on_member_status(member: str, label: str) -> None:
            if self.on_team_notice is None:
                return
            try:
                self.on_team_notice(f"{member} {label}（{len(self.active_team['hub'].members) - 1} 个子 agent）"
                                    if label == "运行中" else f"{member} {label}")
            except Exception:
                pass

        hub.on_leader_message = on_message
        hub.on_member_status = on_member_status

    async def _team_inbox(self) -> str | None:
        """队长的步边界注入：交付/消息/最终报告自动送达对话。"""
        team = self.active_team
        if team is None:
            return None
        hub: TeamHub = team["hub"]
        lines: list[str] = []
        inbox = hub.drain(LEADER)
        if inbox:
            hub.mark_delivered(LEADER, inbox)
            for message in inbox:
                if message.get("task_id"):
                    lines.append(f"【团队交付】{message['from']} 完成任务 "
                                 f"{message['task_id']}：{message['body']}")
                elif message.get("type") == "system":
                    lines.append(f"【团队】{message['body']}")
                else:
                    lines.append(f"【团队】{message['from']} → 你：{message['body']}")
        if team["finished"].is_set():
            lines.append("【团队】已收队，最终报告：\n"
                         + (team["summary"] or hub.final_report()))
            team["report_delivered"] = True
            self.active_team = None
        elif lines:
            lines.append(hub.board_digest())
        return "\n".join(lines) if lines else None

    def team_status(self) -> str:
        if self.active_team is None:
            return "当前没有运行中的团队。"
        return self.active_team["hub"].board_digest()

    def team_needs_leader(self) -> bool:
        """自动续回合的判据：leader 收件箱有未消费内容，或团队结束
        报告还没送达过。"""
        team = self.active_team
        if team is None:
            return False
        hub: TeamHub = team["hub"]
        if hub.drain(LEADER):
            return True
        return team["finished"].is_set() and not team.get("report_delivered")

    # ---------- auto dream（HIGHLIGHTS 2.3 第一层） ----------

    _DREAM_SYSTEM_PROMPT = (
        "你是记忆提炼器。从对话片段中提炼用户的长期偏好与项目事实"
        "（编码风格、常用命令、工具习惯、项目约定、踩坑教训），"
        "忽略一次性的任务细节；只依据明确或强烈暗示的用户表态，宁缺毋滥。"
    )

    _DREAM_BATCH_HEADER = (
        '下面是若干对话单元（用户消息 + 相邻助手回复摘要）。提炼其中体现的'
        '长期偏好/项目事实，输出 JSON 数组（可为空数组 []），每项形如 '
        '{"action": "new", "section": "用户偏好|项目事实|踩过的坑", '
        '"statement": "一句话"}。若提供了当前记忆索引，与已有记忆同义时用 '
        '{"action": "matches", "target": "Pxx"}，用户改主意时用 '
        '{"action": "supersedes", "target": "Pxx"}。只输出 JSON。'
    )

    async def run_dream(self, *, max_sessions: int = 20,
                        batch_token_budget: int = 12_000) -> int:
        """离线批量提炼：启动时补跑而非守护进程定时。

        双层游标：全局 last_dream_at（哪天跑过）+ 每会话已消费消息数
        （消费到哪条）。按 mtime 选出活跃会话后只提炼各会话新增的
        消息，持续追加中的会话不会重跑全量；提取结果由合并纯函数
        按内容归一化去重。跑完全部成功才写回游标，崩溃重跑不丢不重。
        返回处理的会话数。
        """
        state_path = self.root / "memory" / "dream_state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        cutoff = (datetime.now() - timedelta(days=7)).timestamp()
        if state.get("last_dream_at"):
            try:
                cutoff = datetime.fromisoformat(
                    str(state["last_dream_at"])).timestamp()
            except ValueError:
                pass
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return 0
        files = sorted(
            (p for p in sessions_dir.glob("*.jsonl") if p.stat().st_mtime > cutoff),
            key=lambda p: p.stat().st_mtime,
        )[-max_sessions:]
        cursors: dict[str, int] = dict(state.get("sessions") or {})
        index_text = self.memory.index_text()
        processed = 0
        for path in files:
            try:
                _, messages = self.sessions.load(path.stem)
            except Exception:
                continue
            consumed = int(cursors.get(path.stem, 0))
            new_messages = messages[min(consumed, len(messages)):]
            user_tokens = sum(
                estimate_message_tokens([message]) for message in new_messages
                if message.get("role") == "user"
            )
            if not new_messages or user_tokens < 500:
                cursors[path.stem] = len(messages)  # 无新内容：推进游标
                processed += 1
                continue
            units = session_to_units(new_messages)
            for batch in self._pack_units(units, batch_token_budget):
                body = "\n\n".join(
                    f"<对话单元 {number}>\n用户：{unit['user']}\n"
                    f"助手（摘要）：{unit['assistant'] or '（无）'}\n"
                    f"</对话单元 {number}>"
                    for number, unit in enumerate(batch, 1)
                )
                request = [
                    {"role": "system", "content": self._DREAM_SYSTEM_PROMPT},
                    {"role": "user", "content": self._DREAM_BATCH_HEADER
                     + (f"\n\n<当前记忆索引>\n{index_text}\n</当前记忆索引>"
                        if index_text else "")
                     + "\n\n" + body},
                ]
                try:
                    response = await self.provider.complete(
                        request, self.registry, None, tool_choice="none"
                    )
                    findings = parse_memory_findings((response.text or "").strip())
                except Exception:
                    continue  # 单批失败不影响其他批次
                if findings:
                    entries, _ = merge_findings(
                        self.memory.load(), findings, source="dream"
                    )
                    self.memory.save(entries)
            # 游标推进到本轮实际见过的消息数；中途异常向上抛，
            # 游标不写回，下次从上次消费处继续。
            cursors[path.stem] = len(messages)
            processed += 1
        # 全部成功才写回游标。
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state["last_dream_at"] = datetime.now().isoformat()
        state["sessions"] = dict(sorted(cursors.items())[-200:])
        state_path.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
        return processed

    @staticmethod
    def _pack_units(units: list[dict[str, str]],
                    token_budget: int) -> list[list[dict[str, str]]]:
        """对话单元按估算 token 装填分批（离线批处理，粗估即可）。"""
        batches: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        size = 0
        for unit in units:
            tokens = (len(unit["user"]) + len(unit["assistant"])) // 3 + 8
            if current and size + tokens > token_budget:
                batches.append(current)
                current, size = [], 0
            current.append(unit)
            size += tokens
        if current:
            batches.append(current)
        return batches

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
        self.team_runner.provider = provider
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

    async def ask(self, prompt: str | None, *, session_id: str | None = None,
                  output_format: str = "text",
                  text_callback: Callable[[str], None] | None = None,
                  event_callback: Callable[[AgentEvent], None] | None = None) -> tuple[str, str, AsyncIterator[AgentEvent] | list[AgentEvent]]:
        """执行一次任务；返回 final_text、session_id 和事件集合。

        prompt=None 为团队自动续回合：不追加用户消息，leader 只消费
        步边界注入的团队收件箱（交付/消息/报告）。
        """
        self.busy = True
        created = False
        if session_id is None:
            if prompt is None:
                raise ConfigurationError("新会话需要一条用户消息")
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
        if prompt is not None:
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
        # 团队自动续回合的工具调用密集（逐成员 team_send 分派、处理
        # 交付），迭代预算放大到不低于 32，避免调度中途触顶失败。
        tool_limit = self.config.runtime.max_tool_iterations
        if prompt is None:
            tool_limit = max(tool_limit, 32)
        for attempt in range(2):
            agent = Agent(
                self.provider, self.registry, context,
                AgentRunConfig(max_tool_iterations=tool_limit),
                permission_checker=self.permission_checker,
                auditor=self.auditor,
                session_store=self.sessions,
                session_id=session_id,
                hook_engine=self.hook_engine,
                result_spiller=self.result_spiller,
                usage_observer=observe_usage,
                pre_request_hook=pre_request,
                checkpoint=self.checkpoint,
                inbox_hook=self._team_inbox,
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
        tool_use/tool_result 配对完整。摘要请求重放真实 system 与
        被压缩旧消息原文（公共前缀命中 KV cache），失败时回退为
        固定规则摘要。compacting 期间拒绝重入。
        """
        if self.compacting:
            return ""
        self.compacting = True
        try:
            prefix = [m for m in messages if m.get("role") == "system"]
            body = [m for m in messages if m.get("role") != "system"]
            split = self.context_manager.split_for_compaction(body)
            if split is None:
                return ""
            old, tail = split
            summary_text = await self._summarize_old(old)
            # 压缩顺带提炼：把"值得长期记住的事"小节并入长期记忆。
            try:
                section = split_section(summary_text, _MEMORY_SECTION_HEADING)
                if section and section.strip() != "无":
                    findings = parse_memory_findings(section)
                    if findings:
                        entries, _ = merge_findings(
                            self.memory.load(), findings, source="compaction"
                        )
                        self.memory.save(entries)
            except Exception:
                pass  # 记忆提炼失败不影响压缩本身
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
        finally:
            self.compacting = False

    async def _summarize_old(self, old: list[dict[str, Any]]) -> str:
        if not old:
            return "（无可压缩的历史）"
        # 前缀复用（HIGHLIGHTS 1.3.A）：重放真实 system prompt + 被压缩旧
        # 消息原文（逐字节一致，不做任何序列化改写），压缩指令作为末尾
        # user 消息——与最近一次主请求构成公共前缀命中服务商 KV cache。
        # tool schema 原样传入但 tool_choice="none" 禁止摘要模型调工具。
        summary_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            *old,
            {"role": "user",
             "content": _COMPACTION_INSTRUCTION
             + _memory_addon(self.memory.index_text())},
        ]
        try:
            response = await self.provider.complete(
                summary_messages, self.registry, None, tool_choice="none"
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

    def _skill_catalog(self) -> str:
        """已安装 Skill 的名称+描述清单（HIGHLIGHTS 4.2）。

        注入 system prompt 让模型知道有哪些技能可用；带截断预算，
        且内容稳定以保护前缀 KV cache。
        """
        if not self.config.skills_enabled:
            return ""
        try:
            skills = self.skill_library.discover()
        except Exception:
            return ""
        if not skills:
            return ""
        lines = [f"- {skill.name}：{skill.description}" for skill in skills]
        catalog = "\n".join(lines)
        if len(catalog) > self._SKILL_CATALOG_BUDGET:
            catalog = catalog[:self._SKILL_CATALOG_BUDGET] + "\n…（已截断）"
        return ("已安装 Skill（用户提到相关任务时，用 load_skill 按名称加载"
                "完整正文）：\n" + catalog)

    _SKILL_CATALOG_BUDGET = 2_000

    def _system_prompt(self) -> str:
        """为真实模型生成稳定的行为边界和当前工作区信息。"""
        instruction_path = self.root / "memory" / "instructions.md"
        custom = ""
        if instruction_path.exists():
            try:
                custom = instruction_path.read_text(encoding="utf-8").strip()
            except OSError:
                custom = ""
        skill_catalog = self._skill_catalog()
        memory_text = self.memory.inject_text()
        return "\n\n".join([
            "你是 conti-agent，一个谨慎的本地编程助手。"
            "回答保持简洁、可执行；修改文件或执行命令前必须说明将做什么。",
            f"当前工作区：{self.workspace.root}",
            f"权限模式：{self.config.runtime.permission_mode}",
            f"可用工具：{', '.join(self.registry.names())}",
            "需要用户澄清时调用 request_input；"
            "用户明确要求记住某事时调用 memory_write。",
            *([skill_catalog] if skill_catalog else []),
            *([memory_text] if memory_text else []),
            *([f"用户附加指令：\n{custom}"] if custom else []),
        ])
