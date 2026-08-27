from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ContiAgentError


class CollaborationError(ContiAgentError):
    pass


@dataclass
class Task:
    id: str
    title: str
    owner: str = ""
    status: str = "todo"
    result: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Message:
    id: str
    from_agent: str
    to_agent: str
    body: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CrewState:
    name: str
    tasks: dict[str, Task] = field(default_factory=dict)
    mailbox: list[Message] = field(default_factory=list)


class CrewManager:
    """持久化的本地任务板和邮箱协调器。"""

    def __init__(self, root: Path, name: str) -> None:
        self.root = Path(root)
        self.name = name
        self.path = self.root / f"{name}.json"
        self.state = self._load()

    def _load(self) -> CrewState:
        if not self.path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            return CrewState(name=self.name)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        tasks = {key: Task(**value) for key, value in raw.get("tasks", {}).items()}
        messages = [Message(**item) for item in raw.get("mailbox", [])]
        return CrewState(raw.get("name", self.name), tasks, messages)

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "name": self.state.name,
            "tasks": {key: asdict(value) for key, value in self.state.tasks.items()},
            "mailbox": [asdict(item) for item in self.state.mailbox],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def create_task(self, task_id: str, title: str, owner: str = "") -> Task:
        if task_id in self.state.tasks:
            raise CollaborationError(f"任务 ID 已存在：{task_id}")
        task = Task(task_id, title, owner)
        self.state.tasks[task_id] = task
        self._save()
        return task

    def update_task(self, task_id: str, *, status: str | None = None,
                    owner: str | None = None, result: str | None = None) -> Task:
        if task_id not in self.state.tasks:
            raise CollaborationError(f"任务不存在：{task_id}")
        task = self.state.tasks[task_id]
        if status not in {None, "todo", "doing", "done", "failed"}:
            raise CollaborationError(f"非法任务状态：{status}")
        task.status = status or task.status
        task.owner = owner if owner is not None else task.owner
        task.result = result if result is not None else task.result
        task.updated_at = time.time()
        self._save()
        return task

    def get_task(self, task_id: str) -> Task:
        try:
            return self.state.tasks[task_id]
        except KeyError as exc:
            raise CollaborationError(f"任务不存在：{task_id}") from exc

    def list_tasks(self) -> list[Task]:
        return sorted(self.state.tasks.values(), key=lambda item: item.created_at)

    def send(self, message_id: str, from_agent: str, to_agent: str, body: str) -> Message:
        if any(item.id == message_id for item in self.state.mailbox):
            raise CollaborationError(f"消息 ID 已存在：{message_id}")
        message = Message(message_id, from_agent, to_agent, body)
        self.state.mailbox.append(message)
        self._save()
        return message

    def drain(self, agent_name: str) -> list[Message]:
        matched = [item for item in self.state.mailbox if item.to_agent == agent_name]
        self.state.mailbox = [item for item in self.state.mailbox if item.to_agent != agent_name]
        self._save()
        return matched
