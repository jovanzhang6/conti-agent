from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent import Agent, AgentRunConfig
from .errors import ToolValidationError
from .messages import user_message
from .permissions import PermissionChecker
from .tools import Tool, ToolContext, ToolRegistry, ToolResult

# 队长在 hub 中的收件箱别名；team_send(to="leader") 会归一化到它。
LEADER = "__leader__"

DEFAULT_MAX_MEMBERS = 4
DEFAULT_MAX_MESSAGES = 200
DEFAULT_PARK_TIMEOUT = 120.0
DEFAULT_TEAM_TIMEOUT = 1_800.0

TEAM_PROTOCOL = (
    "你是团队的一员。协作规则：\n"
    "1. 给队友发消息用 team_send(to=对方名字)；交付任务用 "
    "team_send(to=\"leader\", task_id=任务ID, body=交付内容)；"
    "向全队广播用 to=\"*\"。\n"
    "2. 完成任务必须交付（带 task_id），任务板会自动更新并唤醒依赖者。\n"
    "3. 收件箱消息会在你每次调用工具之间送达，不会打断你正在执行的步骤。\n"
    "4. 没有可做的事就直接结束回合（挂起）；有新消息或依赖就绪时你会被唤醒，"
    "不要空转，不要重复汇报。\n"
    "5. 你没有向用户提问的通道，遇到阻塞就在交付/消息里说明。"
)


@dataclass
class TeamTask:
    id: str
    title: str
    owner: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "todo"  # todo / waiting / doing / done / failed
    result: str = ""


class TeamHub:
    """持久化黑板 + 信箱协调中心（HIGHLIGHTS 亮点 5）。

    写入纪律：先追加 journal（事实源，审计与对账），再原子重写
    state.json（人类可读快照，写失败仅降级）。收件箱为内存对象，
    投递即消费（先注入对话再从收件箱删除，对话是消费凭证）。
    """

    def __init__(self, conti_root: Path, team_id: str | None = None) -> None:
        self.team_id = team_id or time.strftime("%Y%m%d-%H%M%S")
        self.directory = Path(conti_root) / "team" / self.team_id
        self.journal_path = self.directory / "journal.jsonl"
        self.state_path = self.directory / "state.json"
        self.status = "open"
        self.members: dict[str, dict[str, str]] = {LEADER: {"profile": "leader",
                                                            "status": "running"}}
        self.tasks: dict[str, TeamTask] = {}
        self.mailbox: dict[str, list[dict[str, Any]]] = {LEADER: []}
        self.wake: dict[str, asyncio.Event] = {LEADER: asyncio.Event()}
        self.seq = 0
        self.message_count = 0
        # leader 空闲时的被动通知钩子（TUI 注入）：发往 LEADER 的每条
        # 消息立即回调显示；不启动新回合（唤醒权单向，用户是唯一唤醒者）。
        self.on_leader_message: Callable[[dict[str, Any]], None] | None = None

    # ---------- 持久化 ----------

    def _journal(self, kind: str, **fields: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), "kind": kind, **fields},
                                    ensure_ascii=False) + "\n")

    def _save_state(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        state = {
            "team_id": self.team_id,
            "status": self.status,
            "seq": self.seq,
            "members": self.members,
            "tasks": {key: asdict(task) for key, task in self.tasks.items()},
            "mailbox": self.mailbox,
        }
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(self.state_path)

    # ---------- 建队 ----------

    def open_team(self, members: list[dict[str, Any]],
                  tasks: list[dict[str, Any]]) -> None:
        if len(members) > DEFAULT_MAX_MEMBERS:
            raise ToolValidationError(f"团队成员过多（上限 {DEFAULT_MAX_MEMBERS}）")
        for member in members:
            name = str(member["name"]).strip()
            if name in {LEADER, "*"} or not name:
                raise ToolValidationError(f"非法成员名：{name}")
            self.members[name] = {"profile": str(member["profile"]),
                                  "status": "running"}
            self.mailbox[name] = []
            self.wake[name] = asyncio.Event()
        seen: set[str] = set()
        for index, item in enumerate(tasks, 1):
            task_id = str(item.get("id") or f"T{index}")
            owner = str(item["owner"])
            if owner not in self.members:
                raise ToolValidationError(f"任务 {task_id} 的负责人不存在：{owner}")
            deps = [str(dep) for dep in item.get("depends_on") or []]
            unknown = [dep for dep in deps if dep not in {str(t.get("id") or f"T{i}")
                                                          for i, t in enumerate(tasks, 1)}]
            if unknown:
                raise ToolValidationError(f"任务 {task_id} 依赖不存在的任务：{unknown}")
            status = "todo" if not deps else "waiting"
            self.tasks[task_id] = TeamTask(task_id, str(item["title"]), owner,
                                           deps, status)
            seen.add(task_id)
        self._journal("team.opened", members=list(self.members),
                      tasks={key: asdict(value) for key, value in self.tasks.items()})
        self._save_state()

    # ---------- 信箱 ----------

    def send(self, sender: str, receiver: str, body: str, *,
             type: str = "chat", task_id: str | None = None) -> dict[str, Any]:
        if self.status != "open":
            raise ToolValidationError("团队已收队，不能再发消息")
        if receiver == "leader":
            receiver = LEADER
        if receiver != "*" and receiver not in self.mailbox:
            raise ToolValidationError(f"收件人不存在：{receiver}")
        if self.message_count >= DEFAULT_MAX_MESSAGES:
            raise ToolValidationError("团队消息总量超限，请收敛协作")
        receivers = list(self.mailbox) if receiver == "*" else [receiver]
        if receiver == "*" and sender in receivers:
            receivers.remove(sender)
        sent = []
        for name in receivers:
            self.seq += 1
            self.message_count += 1
            message = {"id": f"M{self.seq}", "seq": self.seq, "from": sender,
                       "to": name, "type": type, "body": str(body),
                       "task_id": task_id, "ts": time.time()}
            self.mailbox[name].append(message)
            self._journal("message.sent", id=message["id"], **{
                key: message[key] for key in ("from", "to", "type", "task_id")
            })
            sent.append(message)
            self._wake(name)
            if name == LEADER and sender != LEADER and self.on_leader_message:
                try:
                    self.on_leader_message(message)
                except Exception:
                    pass  # 通知失败不影响投递本身
        self._save_state()
        return sent[0] if sent else {}

    def drain(self, receiver: str) -> list[dict[str, Any]]:
        return list(self.mailbox.get(receiver, []))

    def mark_delivered(self, receiver: str, messages: list[dict[str, Any]]) -> None:
        delivered_ids = {message["id"] for message in messages}
        box = self.mailbox.get(receiver, [])
        self.mailbox[receiver] = [m for m in box if m["id"] not in delivered_ids]
        for message in messages:
            self._journal("message.delivered", id=message["id"], to=receiver)
        self._save_state()

    def _wake(self, name: str) -> None:
        event = self.wake.get(name)
        if event is not None:
            event.set()

    async def wait_wake(self, name: str, timeout: float) -> bool:
        event = self.wake.get(name)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            event.clear()

    # ---------- 任务板 ----------

    def set_status(self, member: str, status: str) -> None:
        if member in self.members:
            self.members[member]["status"] = status

    def ready_tasks(self, agent_name: str) -> list[TeamTask]:
        return [task for task in self.tasks.values()
                if task.owner == agent_name and task.status == "todo"
                and all(self.tasks[dep].status == "done"
                        for dep in task.depends_on if dep in self.tasks)]

    def my_doing(self, agent_name: str) -> list[TeamTask]:
        return [task for task in self.tasks.values()
                if task.owner == agent_name and task.status == "doing"]

    def complete_task(self, task_id: str, result: str, by: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            raise ToolValidationError(f"任务不存在：{task_id}")
        task.status = "done"
        task.result = result
        self._journal("task.completed", task_id=task_id, by=by)
        # 依赖就绪：解锁 waiting 任务并通知负责人。
        for other in self.tasks.values():
            if other.status == "waiting" and task_id in other.depends_on \
                    and all(self.tasks[dep].status == "done"
                            for dep in other.depends_on if dep in self.tasks):
                other.status = "todo"
                self.send(LEADER, other.owner,
                          f"依赖 {task_id} 已完成，任务 {other.id}"
                          f"「{other.title}」可以开始", type="system")
        self._save_state()

    def fail_task(self, task_id: str, reason: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        task.status = "failed"
        task.result = reason
        self._journal("task.failed", task_id=task_id, reason=reason)
        self._save_state()

    def board_digest(self) -> str:
        if not self.tasks:
            return "任务板为空"
        parts = []
        for task in self.tasks.values():
            mark = {"done": "✓", "failed": "✗", "doing": "⏳", "waiting": "○"}.get(
                task.status, "○")
            owner = f"→{task.owner}" if task.status in {"doing", "waiting"} else ""
            parts.append(f"{task.id}{mark}{owner}")
        return "任务板：" + "｜".join(parts)

    def finish(self, reason: str) -> None:
        self.status = "closed"
        self._journal("team.closed", reason=reason)
        self._save_state()
        for event in self.wake.values():
            event.set()

    def final_report(self) -> str:
        lines = []
        for task in self.tasks.values():
            mark = "✓" if task.status == "done" else "✗"
            lines.append(f"{mark} {task.id} {task.title}（{task.owner}）："
                         f"{task.result or task.status}")
        return "\n".join(lines) or "（团队无任务）"


class TeamSendTool(Tool):
    """团队通信的唯一出口：发消息 / 广播 / 交付任务。"""

    name = "team_send"
    description = (
        "团队通信工具。给队友发消息 to=成员名；向全队广播 to=\"*\"；"
        "交付任务 to=\"leader\" 并带 task_id（交付内容写在 body）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "成员名 / leader / *"},
            "body": {"type": "string", "description": "消息内容"},
            "task_id": {"type": "string", "description": "交付任务时填任务 ID"},
        },
        "required": ["to", "body"],
    }
    # 通信不是资源副作用：空 effects 让任何档位的成员都能发言，
    # 权限收敛只作用于成员的工作工具（由 profile 白名单控制）。
    effects = frozenset()

    def __init__(self, hub: TeamHub, sender: str) -> None:
        self.hub = hub
        self.sender = sender

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        task_id = arguments.get("task_id")
        message = self.hub.send(
            self.sender, str(arguments["to"]), str(arguments["body"]),
            type="delivery" if task_id else "chat", task_id=task_id,
        )
        if task_id and message.get("to") == LEADER:
            self.hub.complete_task(str(task_id), str(arguments["body"]), self.sender)
        return ToolResult(f"已发送给 {message.get('to', arguments['to'])}")


class LeaderSendTool(Tool):
    """队长的中途指派/纠偏通道：给某个成员或全队发消息。

    与成员版同为空 effects（通信不是资源副作用）；成员若在挂起中
    会被 hub 唤醒，运行中则在下一个步边界看到。
    """

    name = "team_send"
    description = (
        "给团队成员发消息：新指派、纠偏、补充上下文。to=成员名；"
        "to=\"*\" 广播全员。成员挂起中会被立即唤醒，运行中在下一个"
        "工具间隙看到。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "成员名，或 * 广播"},
            "body": {"type": "string", "description": "消息内容（新任务写清要求）"},
        },
        "required": ["to", "body"],
    }
    effects = frozenset()

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        team = self.runtime.active_team
        if team is None:
            return ToolResult("当前没有运行中的团队。", is_error=True)
        message = team["hub"].send(LEADER, str(arguments["to"]),
                                   str(arguments["body"]), type="chat")
        return ToolResult(f"已发送给 {message.get('to', arguments['to'])}，"
                          "对方会在下一个工具间隙或被唤醒时看到。")


class TeamRunner:
    """成员运行循环：激活 → 干活 → 挂起 → 唤醒。"""

    def __init__(self, provider: Any, base_registry: ToolRegistry,
                 workspace: Path, profiles: dict[str, Any]) -> None:
        self.provider = provider
        self.base_registry = base_registry
        self.workspace = Path(workspace)
        self.profiles = profiles  # name -> ProfileConfig

    async def run(self, hub: TeamHub, members: list[dict[str, Any]], *,
                  provider: Any = None,
                  session_store=None, session_id: str | None = None,
                  emit: Callable[[Any], None] | None = None,
                  max_member_turns: int = 8,
                  park_timeout: float = DEFAULT_PARK_TIMEOUT,
                  team_timeout: float = DEFAULT_TEAM_TIMEOUT) -> str:
        # provider 每次起队时传入（模型切换后 Runtime.provider 是新对象）。
        if provider is not None:
            self.provider = provider
        member_tasks = [
            asyncio.create_task(self._member_loop(
                hub, member, session_store=session_store, session_id=session_id,
                emit=emit, max_member_turns=max_member_turns,
                park_timeout=park_timeout,
            ))
            for member in members
        ]
        done, pending = await asyncio.wait(member_tasks, timeout=team_timeout)
        for task in pending:
            task.cancel()
        if pending:
            for task_id, task in hub.tasks.items():
                if task.status in {"todo", "waiting", "doing"}:
                    hub.fail_task(task_id, "团队超时")
        hub.finish("团队运行结束" if not pending else "团队超时收队")
        return hub.final_report()

    async def _member_loop(self, hub: TeamHub, member: dict[str, Any], *,
                           session_store=None, session_id: str | None = None,
                           emit: Callable[[Any], None] | None = None,
                           max_member_turns: int = 8,
                           park_timeout: float = DEFAULT_PARK_TIMEOUT) -> None:
        name = str(member["name"])
        profile = self.profiles.get(str(member["profile"]))
        if profile is None:
            hub.send(LEADER, LEADER, f"成员 {name} 的 profile 不存在", type="system")
            hub.set_status(name, "failed")
            return
        tool_names = [t for t in profile.allowed_tools if t != "spawn_task"]
        registry = self.base_registry.filter(tool_names)
        registry.register(TeamSendTool(hub, name))
        context = ToolContext(workspace=self.workspace, session_id=session_id,
                              task_id=f"team:{name}", profile=str(member["profile"]),
                              services={})
        checker = PermissionChecker(profile.permission_mode,
                                    workspace=self.workspace)
        # 无审批入口：子代理的审批请求确定性拒绝（fail-closed）。
        assigned = [task for task in hub.tasks.values() if task.owner == name]
        brief = "\n".join(
            f"- {task.id}「{task.title}」"
            + ("（等依赖就绪的通知再开始）" if task.status == "waiting" else "")
            for task in assigned
        ) or "（暂无任务，等待 team_send 通知）"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": profile.system_prompt + "\n\n" + TEAM_PROTOCOL},
            user_message(f"你已加入团队，名字是 {name}。任务板：{hub.board_digest()}\n"
                         f"你的任务：\n{brief}\n开始工作。"),
        ]
        turns = 0
        nudged: set[str] = set()
        while hub.status == "open":
            turns += 1
            if turns > max_member_turns:
                for task in hub.my_doing(name):
                    hub.fail_task(task.id, f"{name} 轮数超限")
                    hub.send(LEADER, LEADER, f"任务 {task.id} 因 {name} 轮数超限失败",
                             type="system")
                hub.set_status(name, "failed")
                return
            agent = Agent(
                self.provider, registry, context,
                AgentRunConfig(max_tool_iterations=profile.max_tool_iterations),
                permission_checker=checker,
                session_store=session_store, session_id=session_id,
            )
            try:
                async for item in agent.run(messages):
                    if emit is not None:
                        emit(item)
            except Exception as exc:
                # 机制级异常：自动重试一次（全新对话 + 摘要），再失败交队长。
                try:
                    retry_messages = [
                        messages[0],
                        user_message(f"你上一轮因异常中断：{exc}。"
                                     f"任务板：{hub.board_digest()}。继续你的任务。"),
                    ]
                    retry_agent = Agent(
                        self.provider, registry, context,
                        AgentRunConfig(max_tool_iterations=profile.max_tool_iterations),
                        permission_checker=checker,
                        session_store=session_store, session_id=session_id,
                    )
                    async for item in retry_agent.run(retry_messages):
                        if emit is not None:
                            emit(item)
                    messages = retry_messages
                except Exception:
                    for task in hub.my_doing(name):
                        hub.fail_task(task.id, f"{name} 异常：{exc}")
                        hub.send(LEADER, LEADER,
                                 f"成员 {name} 异常退出：{exc}", type="system")
                    hub.set_status(name, "failed")
                    return
            # 回合结束：查收件箱（步边界之外的收尾投递）。
            inbox = hub.drain(name)
            if inbox:
                hub.mark_delivered(name, inbox)
                notice = "\n".join(
                    f"【团队消息】来自 {m['from']}：{m['body']}"
                    + (f"（任务 {m['task_id']}）" if m.get("task_id") else "")
                    for m in inbox
                )
                messages.append(user_message(notice))
                continue
            # 漏交付防呆：名下有 doing 任务却收工 → 提醒一次，再犯判失败。
            doing = hub.my_doing(name)
            if doing:
                stuck = [task for task in doing if task.id not in nudged]
                if stuck:
                    for task in stuck:
                        nudged.add(task.id)
                    messages.append(user_message(
                        f"【系统提醒】任务 {', '.join(t.id for t in stuck)} 还未交付："
                        "用 team_send(to=\"leader\", task_id=...) 交付，"
                        "或说明受阻原因。"))
                    continue
                for task in doing:
                    hub.fail_task(task.id, f"{name} 收工未交付")
                    hub.send(LEADER, LEADER,
                             f"任务 {task.id} 失败：{name} 未交付", type="system")
                hub.set_status(name, "done")
                return
            # 无事可做：挂起等唤醒。
            hub.set_status(name, "parked")
            woke = await hub.wait_wake(name, timeout=park_timeout)
            hub.set_status(name, "running")
            if hub.status != "open":
                hub.set_status(name, "done")
                return
            if not woke:
                # park 超时：板上还有我的活就继续，否则收工。
                if not hub.ready_tasks(name) and not hub.drain(name):
                    hub.set_status(name, "done")
                    return
