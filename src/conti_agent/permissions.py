from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import tomllib

from .errors import PermissionDenied
from .tools import Tool, ToolContext, ToolRegistry, ToolResult
from .workspace import Workspace


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE = "workspace"
    APPROVED = "approved"
    TRUSTED = "trusted"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    source: str = "policy"
    rule: str | None = None


@dataclass(frozen=True)
class PermissionRule:
    tool: str
    decision: str
    effect: str | None = None
    pattern: str | None = None


DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+",
    r"\bremove-item\b.*\b-recurse\b",
    r"\bformat\b\s+[a-z]:",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bsudo\b",
    r"\bsh\s+curl\b|\bbash\s+curl\b|\b.invoke-webrequest\b.*\biex\b",
    r"(api[_-]?key|secret|password)\s*=",
))


class DangerousCommandDetector:
    """保守检测可能破坏系统或泄露凭据的命令。"""

    def inspect(self, arguments: dict[str, Any]) -> str | None:
        command = arguments.get("command")
        if isinstance(command, list):
            text = " ".join(str(item) for item in command)
        else:
            text = str(arguments.get("command_line", ""))
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(text):
                return pattern.pattern
        return None


class PathSandbox:
    """校验工具参数中的路径是否仍处于活动工作区。"""

    PATH_KEYS = {"path"}

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def check(self, arguments: dict[str, Any]) -> Decision:
        for key in self.PATH_KEYS & arguments.keys():
            try:
                self.workspace.resolve(arguments[key])
            except Exception as exc:
                return Decision(False, f"路径检查失败：{exc}", source="sandbox")
        return Decision(True, "路径位于工作区内", source="sandbox")


class RuleEngine:
    """按 本地项目 > 项目 > 用户 > 默认 的顺序合并权限规则。"""

    def __init__(self, paths: list[Path] | None = None) -> None:
        self.paths = paths or []
        self.rules: list[PermissionRule] = []
        self._load()

    def _load(self) -> None:
        # paths 由调用方按低优先级到高优先级传入，这里反向读取，先命中高优先级。
        for path in reversed(self.paths):
            if not path.exists():
                continue
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
            for item in reversed(raw.get("rule", [])):
                self.rules.append(PermissionRule(
                    tool=item.get("tool", "*"),
                    decision=item.get("decision", "deny"),
                    effect=item.get("effect"),
                    pattern=item.get("pattern"),
                ))

    def match(self, tool: Tool, arguments: dict[str, Any]) -> Decision | None:
        command = arguments.get("command")
        # 规则表达式可同时匹配路径和命令，便于对文件类工具写精确例外。
        parts: list[str] = []
        if "path" in arguments:
            parts.append(str(arguments["path"]))
        if isinstance(command, list):
            parts.extend(str(item) for item in command)
        elif "command_line" in arguments:
            parts.append(str(arguments["command_line"]))
        text = " ".join(parts)
        for rule in self.rules:
            if rule.tool not in {"*", tool.name}:
                continue
            if rule.effect and rule.effect not in tool.effects:
                continue
            if rule.pattern and not re.search(rule.pattern, text):
                continue
            return Decision(
                rule.decision == "allow",
                f"规则命中：{rule.tool}/{rule.pattern or rule.effect or '全部'}",
                source="rules",
                rule=rule.pattern or rule.effect or rule.tool,
            )
        return None


Approver = Callable[[str, dict[str, Any], str], bool | Awaitable[bool]]


class PermissionChecker:
    """统一的工具权限检查入口。"""

    def __init__(self, mode: PermissionMode | str, *, workspace: Workspace,
                 rules: RuleEngine | None = None,
                 detector: DangerousCommandDetector | None = None,
                 approver: Approver | None = None) -> None:
        self.mode = mode if isinstance(mode, PermissionMode) else PermissionMode(mode)
        self.sandbox = PathSandbox(workspace)
        self.rules = rules or RuleEngine()
        self.detector = detector or DangerousCommandDetector()
        self.approver = approver
        self.session_approvals: set[str] = set()

    async def _request(self, key: str, arguments: dict[str, Any], reason: str) -> Decision:
        if self.approver is None:
            return Decision(False, "需要用户批准，但没有可用的批准入口", source="approval")
        result = self.approver(key, arguments, reason)
        if hasattr(result, "__await__"):
            result = await result
        if result:
            self.session_approvals.add(key)
            return Decision(True, "用户已批准", source="approval")
        return Decision(False, "用户拒绝该操作", source="approval")

    async def check(self, tool: Tool, arguments: dict[str, Any], context: ToolContext) -> Decision:
        try:
            tool.validate(arguments)
        except Exception as exc:
            return Decision(False, f"参数校验失败：{exc}", source="schema")

        rule_decision = self.rules.match(tool, arguments)
        if rule_decision is not None:
            return rule_decision

        if self.mode is PermissionMode.READ_ONLY and tool.effects - {"read"}:
            return Decision(False, "只读模式禁止写入、执行或控制类操作", source="mode")

        dangerous = self.detector.inspect(arguments)
        if dangerous:
            if self.mode is not PermissionMode.TRUSTED:
                return Decision(False, f"检测到危险命令模式：{dangerous}", source="danger")
            approval = await self._request(f"dangerous:{dangerous}", arguments, dangerous)
            if not approval.allowed:
                return approval

        path_decision = self.sandbox.check(arguments)
        if not path_decision.allowed:
            return path_decision

        if self.mode is PermissionMode.APPROVED and tool.effects - {"read"}:
            key = f"{tool.name}:{','.join(sorted(tool.effects))}"
            if key not in self.session_approvals:
                approval = await self._request(key, arguments, f"工具 {tool.name} 需要首次批准")
                if not approval.allowed:
                    return approval

        return Decision(True, "默认策略允许", source="mode")


class AuditLogger:
    """把权限决策写入不可依赖模型行为的 JSONL 审计文件。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, tool: Tool, arguments: dict[str, Any],
               decision: Decision, context: ToolContext) -> None:
        safe_arguments = {key: value for key, value in arguments.items() if key != "content"}
        if "env" in safe_arguments:
            safe_arguments["env"] = "[已省略]"
        record = {
            "schema_version": 1,
            "timestamp": time.time(),
            "event": event,
            "tool": tool.name,
            "effects": sorted(tool.effects),
            "arguments": safe_arguments,
            "decision": {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "source": decision.source,
                "rule": decision.rule,
            },
            "session_id": context.session_id,
            "profile": context.profile,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


async def execute_tool_with_permissions(registry: ToolRegistry, call: Any,
                                        context: ToolContext, checker: PermissionChecker,
                                        auditor: AuditLogger | None = None) -> ToolResult:
    """先做权限检查，通过后才执行工具；任何拒绝都不会触达工具实现。"""
    try:
        tool = registry.get(call.name)
    except Exception as exc:
        return ToolResult(str(exc), is_error=True)
    decision = await checker.check(tool, call.arguments, context)
    if auditor:
        auditor.record("denied" if not decision.allowed else "approved",
                       tool, call.arguments, decision, context)
    if not decision.allowed:
        return ToolResult(f"权限拒绝：{decision.reason}", {"decision": decision.reason}, is_error=True)
    from .tools import execute_tool
    return await execute_tool(registry, call, context)
