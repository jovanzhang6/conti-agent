from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from .errors import ToolValidationError
from .tools import Tool, ToolContext, ToolRegistry, ToolResult
from .workspace import Workspace


class WorkspaceReadTool(Tool):
    name = "workspace_read"
    # 默认 limit 的推导：600 行 × ~80 字节/行 ≈ 48KB，落在单结果的
    # 上下文预算（窗口 × 5%，下限 1 万字符）内——默认页不触发落盘替换，
    # 模型一次就能看到全部内容；更大范围由模型按返回的 offset 续读。
    DEFAULT_READ_LINES = 600
    description = (
        "读取工作区内 UTF-8 文本文件，支持按行分页：offset=起始行（从 1 起），"
        "limit=行数（默认 600，约一页以内不触发结果落盘）。部分返回时自动带行号"
        "并在结尾给出续读 offset，按提示的参数继续读即可。目录请用 workspace_list。"
        "读文件请一律使用本工具，不要用 python/type/findstr 读文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1,
                       "description": "起始行号（从 1 起）"},
            "limit": {"type": "integer", "minimum": 1,
                      "description": "本次最多读取的行数（默认 600）"},
            "max_bytes": {"type": "integer", "minimum": 1,
                          "description": "单次返回的字节上限（默认 256000）"},
        },
        "required": ["path"],
    }
    effects = frozenset({"read"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        block, meta = self.workspace.read_lines(
            arguments["path"],
            offset=int(arguments.get("offset", 1)),
            limit=int(arguments.get("limit", self.DEFAULT_READ_LINES)),
            max_bytes=int(arguments.get("max_bytes", 256_000)),
        )
        return ToolResult(block, meta)


class WorkspaceWriteTool(Tool):
    name = "workspace_write"
    description = "在工作区内创建或替换 UTF-8 文本文件。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    effects = frozenset({"write"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        change = self.workspace.write_text(arguments["path"], arguments["content"])
        return ToolResult(
            f"wrote {arguments['path']}",
            {"path": arguments["path"], "byte_change": change},
        )


class WorkspaceEditTool(Tool):
    name = "workspace_edit"
    description = "替换精确文本片段，并拒绝歧义匹配。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "expected_count": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "old", "new"],
    }
    effects = frozenset({"write"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        metadata = self.workspace.edit_text(
            arguments["path"], arguments["old"], arguments["new"],
            expected_count=arguments.get("expected_count"),
        )
        return ToolResult(f"edited {arguments['path']}", metadata)


class WorkspaceListTool(Tool):
    name = "workspace_list"
    description = "列出受限制的工作区路径，并忽略依赖目录。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_depth": {"type": "integer", "minimum": 1},
            "include_hidden": {"type": "boolean"},
        },
    }
    effects = frozenset({"read"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        paths = self.workspace.list_paths(
            arguments.get("path", "."),
            max_depth=arguments.get("max_depth", 4),
            include_hidden=arguments.get("include_hidden", False),
        )
        lines = []
        for path in paths[:1000]:
            kind = "dir" if path.is_dir() else "file"
            lines.append(f"{kind} {self.workspace.relative_display(path)}")
        return ToolResult("\n".join(lines), {"count": len(paths)})


class WorkspaceSearchTool(Tool):
    name = "workspace_search"
    description = "使用字面量或正则表达式搜索文本文件。"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "regex": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1},
        },
        "required": ["pattern"],
    }
    effects = frozenset({"read"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = arguments["pattern"]
        try:
            matcher = re.compile(pattern) if arguments.get("regex", False) else None
        except re.error as exc:
            raise ToolValidationError(f"invalid regular expression: {exc}") from exc
        literal = None if matcher else pattern
        max_results = min(arguments.get("max_results", 200), 1000)
        matches: list[str] = []
        for path in self.workspace.list_paths(arguments.get("path", "."), max_depth=8):
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if matcher is not None:
                    found = matcher.search(line) is not None
                else:
                    found = literal in line
                if found:
                    matches.append(
                        f"{self.workspace.relative_display(path)}:{line_number}:{line.strip()}"
                    )
                    if len(matches) >= max_results:
                        return ToolResult("\n".join(matches), {"count": len(matches), "truncated": False})
        return ToolResult("\n".join(matches), {"count": len(matches)})


# 默认继承的系统基础变量：让 git/python 等工具开箱可用。
# 这些不是机密；密钥类变量仍然默认不继承（需显式 inherit_env 列出）。
_DEFAULT_INHERIT = (
    "PATH", "Path", "PATHEXT",
    "SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "SystemDrive",
    "TEMP", "TMP", "TMPDIR",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "ProgramW6432",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "USERNAME",
    "LOCALAPPDATA", "APPDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "LANG", "LC_ALL",
)


def _environment(declared: dict[str, str], inherit: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    # 未显式给出 inherit_env 时继承安全基础集；给了就严格按清单继承。
    keys = inherit if inherit else list(_DEFAULT_INHERIT)
    for key in keys:
        if key in os.environ:
            result[key] = os.environ[key]
    for key, value in declared.items():
        result[key] = value
    if os.name == "nt":
        result.setdefault("SystemRoot", os.environ.get("SystemRoot", r"C:\Windows"))
        result.setdefault("COMSPEC", os.environ.get("COMSPEC", r"C:\Windows\system32\cmd.exe"))
    return result


class ProcessRunTool(Tool):
    name = "process_run"
    description = (
        "运行有边界限制的本地命令并捕获输出。默认继承系统基础环境"
        "（PATH 等），git/python 等工具直接可用；可用 env 追加变量，"
        "inherit_env 显式指定要继承的变量清单（密钥类变量须显式列出）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "array", "items": {"type": "string"}},
            "command_line": {"type": "string"},
            "path": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 1},
            "max_output": {"type": "integer", "minimum": 100},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "inherit_env": {"type": "array", "items": {"type": "string"}},
        },
    }
    effects = frozenset({"execute", "write"})

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments.get("command")
        command_line = arguments.get("command_line")
        if bool(command) == bool(command_line):
            raise ToolValidationError("provide either command or command_line")
        cwd = self.workspace.resolve(arguments.get("path", "."))
        if not cwd.is_dir():
            raise ToolValidationError("command path must be a directory")
        timeout = min(arguments.get("timeout_ms", 30_000), 300_000) / 1000
        max_output = min(arguments.get("max_output", 20_000), 100_000)
        env = _environment(arguments.get("env", {}), arguments.get("inherit_env", []))
        started = asyncio.get_running_loop().time()
        try:
            if command:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=str(cwd), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command_line, cwd=str(cwd), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
            try:
                raw, _ = await asyncio.wait_for(process.communicate(), timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolResult(
                    "command timed out",
                    {"exit_code": None, "timed_out": True,
                     "duration": round(asyncio.get_running_loop().time() - started, 6)},
                    is_error=True,
                )
        except FileNotFoundError as exc:
            return ToolResult(f"command not found: {exc}", {"exit_code": 127}, is_error=True)
        output = raw.decode("utf-8", errors="replace")
        truncated = len(output.encode("utf-8")) > max_output
        if truncated:
            output = output.encode("utf-8")[:max_output].decode("utf-8", errors="ignore")
        metadata = {
            "exit_code": process.returncode,
            "timed_out": False,
            "truncated": truncated,
            "duration": round(asyncio.get_running_loop().time() - started, 6),
            "cwd": self.workspace.relative_display(cwd),
        }
        return ToolResult(output, metadata, is_error=process.returncode != 0)


def create_local_registry(workspace: Workspace) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        WorkspaceReadTool(workspace),
        WorkspaceWriteTool(workspace),
        WorkspaceEditTool(workspace),
        WorkspaceListTool(workspace),
        WorkspaceSearchTool(workspace),
        ProcessRunTool(workspace),
    ):
        registry.register(tool)
    return registry
