from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .errors import ToolValidationError
from .skills import SkillLibrary
from .tools import Tool, ToolContext, ToolResult
from .workspace import Workspace


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "按名称加载一个已安装 Skill 的完整正文。"
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    effects = frozenset({"read"})

    def __init__(self, library: SkillLibrary) -> None:
        self.library = library

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        skill = self.library.find(arguments["name"])
        return ToolResult(skill.body, {"skill": skill.metadata})


class TaskNoteTool(Tool):
    name = "task_note"
    description = "记录当前任务目标、结论或待办，便于恢复上下文。"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "status": {"type": "string", "enum": ["todo", "doing", "done"]},
        },
        "required": ["title", "body"],
    }
    effects = frozenset({"write"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self.workspace.resolve(".conti/runtime/tasks/notes.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        if path.exists():
            try:
                records = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                records = []
        records.append({
            "title": arguments["title"],
            "body": arguments["body"],
            "status": arguments.get("status", "todo"),
            "session_id": context.session_id,
        })
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult("任务笔记已保存", {"count": len(records)})


class RequestInputTool(Tool):
    name = "request_input"
    description = "向本地用户请求一个澄清答案。"
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    }
    effects = frozenset({"control"})

    def __init__(self, input_function: Callable[[str], str]) -> None:
        self.input_function = input_function

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        answer = self.input_function(arguments["question"])
        return ToolResult(answer, {"question": arguments["question"]})
