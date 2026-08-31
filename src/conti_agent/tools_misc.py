from __future__ import annotations

import inspect
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


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "把用户明确要求记住的长期偏好或项目事实写入长期记忆"
        "（.conti/memory/MEMORY.md，下个会话生效）。只在用户说"
        "\"记住这个\"之类的明确意图时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "statement": {"type": "string", "description": "一句话记忆内容"},
            "section": {
                "type": "string",
                "enum": ["用户偏好", "项目事实", "踩过的坑"],
                "description": "记忆分区，默认用户偏好",
            },
            "matches": {"type": "string",
                        "description": "与已有记忆同义时填其 id（如 P03）"},
            "supersedes": {"type": "string",
                           "description": "用户改主意、需覆盖旧记忆时填其 id"},
        },
        "required": ["statement"],
    }
    effects = frozenset({"write"})

    def __init__(self, store) -> None:
        from .memory import MEMORY_SECTIONS
        self.store = store
        self._sections = MEMORY_SECTIONS

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from .memory import merge_findings
        statement = str(arguments["statement"]).strip()
        if not statement:
            raise ToolValidationError("记忆内容不能为空")
        finding: dict[str, Any]
        if arguments.get("supersedes"):
            finding = {"action": "supersedes", "target": str(arguments["supersedes"]),
                       "section": arguments.get("section"), "statement": statement}
        elif arguments.get("matches"):
            finding = {"action": "matches", "target": str(arguments["matches"]),
                       "section": arguments.get("section"), "statement": statement}
        else:
            finding = {"action": "new", "target": None,
                       "section": arguments.get("section"), "statement": statement}
        entries, notes = merge_findings(self.store.load(), [finding], source="manual")
        self.store.save(entries)
        detail = "；".join(notes) or "无变化"
        return ToolResult(f"已写入长期记忆（下个会话生效）：{detail}",
                          {"memory": notes})


class RequestInputTool(Tool):
    name = "request_input"
    description = "向本地用户请求一个澄清答案。可给出 2-4 个预设选项供快速选择，用户也可以自行输入。"
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要向用户澄清的问题"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "预设答案选项（2-4 个短语，可为空）",
            },
        },
        "required": ["question"],
    }
    # 向用户请求信息不是资源副作用：空 effects 让只读档下也能提问，
    # 与 team_send 同理（通信/交互 ≠ 写入）。
    effects = frozenset()

    def __init__(self, input_function: Callable[..., str]) -> None:
        self.input_function = input_function

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        question = arguments["question"]
        options = arguments.get("options") or None
        answer = self.input_function(question, options)
        # TUI 等界面会注入异步处理器（等待用户在输入框作答，不阻塞事件循环）；
        # 同步 input() 只用于行式模式。
        if inspect.isawaitable(answer):
            answer = await answer
        return ToolResult(answer, {"question": question, "options": options or []})
