from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value).strip()


def summarize_tool_action(name: str, arguments: dict[str, Any]) -> str:
    """把工具名和参数翻译成用户能理解的动作。"""
    path = _text(arguments.get("path", "."))
    name = name.lower()
    if name == "workspace_read":
        return f"读取文件 {path}"
    if name == "workspace_write":
        return f"写入文件 {path}"
    if name == "workspace_edit":
        return f"编辑文件 {path}"
    if name == "workspace_list":
        return f"查看目录 {path}"
    if name == "workspace_search":
        pattern = _text(arguments.get("pattern", ""))
        return f"搜索内容 {pattern}"
    if name == "process_run":
        command = arguments.get("command")
        command = " ".join(str(item) for item in command) if isinstance(command, list) else _text(arguments.get("command_line", ""))
        return f"执行命令 {command}"
    if name == "request_input":
        return "等待你补充信息"
    if name == "spawn_task":
        return f"派发子任务 {_text(arguments.get('profile', ''))}"
    if name == "load_skill":
        return f"加载技能 {_text(arguments.get('name', ''))}"
    return f"执行工具 {name}"


def format_tool_started(name: str, arguments: dict[str, Any]) -> str:
    return "开始" + summarize_tool_action(name, arguments)


def format_tool_completed(name: str, arguments: dict[str, Any], *,
                          is_error: bool = False,
                          elapsed: float | None = None) -> str:
    action = summarize_tool_action(name, arguments)
    suffix = ""
    if elapsed is not None:
        suffix = f"，{elapsed:.2f} 秒"
    if is_error:
        return f"{action}失败{suffix}"
    return f"{action}已完成{suffix}"
