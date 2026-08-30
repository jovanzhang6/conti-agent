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
    """三档预设（HIGHLIGHTS 3.4）：只读 / 标准 / 放行。"""

    READ_ONLY = "read_only"
    WORKSPACE = "workspace"  # 标准档
    TRUSTED = "trusted"      # 放行档


# 兼容旧配置的档位别名；approved 模式并入标准档。
_MODE_ALIASES = {
    "read_only": PermissionMode.READ_ONLY,
    "read-only": PermissionMode.READ_ONLY,
    "readonly": PermissionMode.READ_ONLY,
    "只读": PermissionMode.READ_ONLY,
    "workspace": PermissionMode.WORKSPACE,
    "workspace-write": PermissionMode.WORKSPACE,
    "standard": PermissionMode.WORKSPACE,
    "标准": PermissionMode.WORKSPACE,
    "approved": PermissionMode.WORKSPACE,
    "trusted": PermissionMode.TRUSTED,
    "full": PermissionMode.TRUSTED,
    "full-access": PermissionMode.TRUSTED,
    "danger-full-access": PermissionMode.TRUSTED,
    "放行": PermissionMode.TRUSTED,
}

MODE_CHOICES = ("read_only", "workspace", "trusted")


def normalize_mode(mode: str) -> str:
    """把任意历史别名收敛为三档之一；未知档位报错而非静默放行。"""
    key = str(mode).strip().lower()
    if key in _MODE_ALIASES:
        return _MODE_ALIASES[key].value
    raise PermissionDenied(
        f"未知权限档位：{mode}（可选 {' / '.join(MODE_CHOICES)}）"
    )


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    source: str = "policy"
    rule: str | None = None
    # 危险/越界/受保护操作放行前建议打 git 检查点（/undo 的依据）。
    checkpoint: bool = False


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
    # 亮点 3 补充：版本历史破坏与批量删除。
    r"\bgit\s+push\b[^|;&]*\s(-f\b|--force\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b[^|;&]*\s-[a-z]*f",
    r"\bdel\s+/[sq]\b",
    r"\brd\s+/s\b",
    r">\s*/dev/(?:sd|nvme|hd)",
))

# 危险命令的识别键（用于审批缓存的 key 与审计说明）。

# 不透明命令（HIGHLIGHTS 3.4 第二层）：被解释器/外壳包装或经管道进入
# shell 的命令不尝试看穿内容，一律升级为人工审批——"看不懂的不赌"。
_OPAQUE_PREFIXES = (
    "bash", "sh ", "zsh ", "ash ", "dash ",
    "python -c", "python3 -c", "py -c",
    "node -e", "node -p", "deno eval", "perl -e", "ruby -e", "php -r",
    "eval ", "exec ",
    "powershell -enc", "powershell -e ", "pwsh -enc", "pwsh -e ",
    "cmd /c", "cmd.exe /c",
    "base64 -d", "base64 --decode", "openssl enc -d", "certutil -decode",
)
_OPAQUE_INFIXES = (
    "| sh", "|sh", "| bash", "|bash", "| sh -", "| bash -",
    "&& sh ", "&& bash ", "; sh ", "; bash ",
)


def _command_text(arguments: dict[str, Any]) -> str:
    command = arguments.get("command")
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    if isinstance(command, str):
        return command
    return str(arguments.get("command_line", ""))


def is_opaque_command(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    for prefix in _OPAQUE_PREFIXES:
        if lowered == prefix.strip() or lowered.startswith(prefix):
            return True
    return any(infix in lowered for infix in _OPAQUE_INFIXES)


# 已知无害命令短白名单：含重定向/组合符的一律不算无害。
SAFE_COMMAND_PREFIXES = (
    "ls", "pwd", "echo ", "cat ", "head ", "tail ", "wc ", "file ",
    "git status", "git log", "git diff", "git show", "git branch",
    "git remote", "git stash list", "git --version",
    "python --version", "python3 --version", "py --version",
    "node --version", "npm --version", "pytest", "python -m pytest",
)


def is_safe_command(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered or any(mark in lowered for mark in (">", ";", "&&", "||", "|", "`")):
        return False
    return any(lowered == prefix or lowered.startswith(prefix + " ")
               for prefix in SAFE_COMMAND_PREFIXES)


# 受保护目录：.git 与 .conti（版本历史/账本不可被 agent 自毁）。
_PROTECTED_SEGMENT = re.compile(r"(?:^|[\\/\s\"'])((?:\.git)|(?:\.conti))(?:[\\/]|[\"'\s]|$)",
                                re.IGNORECASE)

# 绝对路径提取（工作区外检测）：Windows 盘符路径与常见 Unix 系统目录。
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s\"'|;&<>]*"
    r"|(?<![A-Za-z0-9])/(?:home|Users|root|etc|var|usr|opt|tmp|mnt|media"
    r"|Windows|Program Files(?: \(x86\))?)\b[^\s\"'|;&<>]*)",
    re.IGNORECASE,
)


def parse_approval(text: str) -> str:
    """把审批答复归一化为 once / always / deny；旧式 yes/no 也兼容。"""
    lowered = str(text).strip().lower()
    if lowered in {"1", "允许一次", "once", "y", "yes"}:
        return "once"
    if lowered in {"2", "本次会话都允许", "本会话都允许", "always"}:
        return "always"
    return "deny"


def _wildcard_regex(pattern: str) -> re.Pattern[str]:
    """规则 pattern 用通配符语义：* 匹配任意字符（大小写不敏感）。"""
    return re.compile(re.escape(pattern).replace(r"\*", ".*"), re.IGNORECASE)


class DangerousCommandDetector:
    """保守检测可能破坏系统或泄露凭据的命令。"""

    def inspect(self, arguments: dict[str, Any]) -> str | None:
        text = _command_text(arguments)
        if not text or text == "None":
            return None
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(text):
                return pattern.pattern
        return None


class PathSandbox:
    """校验工具参数中的路径与命令引用的路径是否处于活动工作区。"""

    PATH_KEYS = {"path"}

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _is_inside(self, raw: Any) -> bool:
        try:
            resolved = Path(str(raw)).expanduser()
            if not resolved.is_absolute():
                resolved = self.workspace.root / resolved
            resolved = resolved.resolve()
            root = Path(self.workspace.root).resolve()
            return resolved == root or root in resolved.parents
        except (OSError, ValueError):
            return False

    def outside_paths(self, arguments: dict[str, Any]) -> list[str]:
        """返回所有指向工作区之外的路径引用（参数键 + 命令文本中的绝对路径）。"""
        outside: list[str] = []
        for key in self.PATH_KEYS & arguments.keys():
            if not self._is_inside(arguments[key]):
                outside.append(str(arguments[key]))
        text = _command_text(arguments)
        if text and text != "None":
            for match in _ABSOLUTE_PATH_RE.finditer(text):
                candidate = match.group(0)
                if not self._is_inside(candidate):
                    outside.append(candidate)
        return outside

    def protected_references(self, arguments: dict[str, Any]) -> bool:
        """路径参数或命令文本是否触碰 .git / .conti。"""
        candidates: list[str] = [str(arguments[key])
                                 for key in self.PATH_KEYS & arguments.keys()]
        text = _command_text(arguments)
        if text and text != "None":
            candidates.append(text)
        return any(_PROTECTED_SEGMENT.search(item) for item in candidates)

    def check(self, arguments: dict[str, Any]) -> Decision:
        outside = self.outside_paths(arguments)
        if outside:
            return Decision(False, f"路径越出工作区：{outside[0]}", source="sandbox")
        return Decision(True, "路径位于工作区内", source="sandbox")


class RuleEngine:
    """分层权限规则：本地项目 > 项目 > 用户；同一文本后匹配的规则胜出。"""

    def __init__(self, paths: list[Path] | None = None) -> None:
        self.paths = paths or []
        self.rules: list[PermissionRule] = []
        self._load()

    def _load(self) -> None:
        # paths 按低优先级到高优先级传入；last-match-wins 语义下
        # 高优先级文件后加载，其规则自然覆盖低优先级。
        for path in self.paths:
            if not path.exists():
                continue
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
            for item in raw.get("rule", []):
                self.rules.append(PermissionRule(
                    tool=item.get("tool", "*"),
                    decision=item.get("decision", "deny"),
                    effect=item.get("effect"),
                    pattern=item.get("pattern"),
                ))

    def match(self, tool: Tool, arguments: dict[str, Any]) -> Decision | None:
        parts: list[str] = []
        if "path" in arguments:
            parts.append(str(arguments["path"]))
        command = _command_text(arguments)
        if command and command != "None":
            parts.append(command)
        text = " ".join(parts)
        decision: Decision | None = None
        for rule in self.rules:
            if rule.tool not in {"*", tool.name}:
                continue
            if rule.effect and rule.effect not in tool.effects:
                continue
            if rule.pattern and not _wildcard_regex(rule.pattern).search(text):
                continue
            # last-match-wins：后面的规则覆盖前面（用户可在低优先级写宽、
            # 高优先级写窄进行收口）。
            decision = Decision(
                rule.decision == "allow",
                f"规则命中：{rule.tool}/{rule.pattern or rule.effect or '全部'}",
                source="rules",
                rule=rule.pattern or rule.effect or rule.tool,
            )
        return decision


# 审批入口返回 "once" / "always" / "deny" 三态（旧式 bool 兼容）。
Approver = Callable[[str, dict[str, Any], str], Any]


class PermissionChecker:
    """统一的工具权限检查入口：规则 → 无害白名单 → 风险门 → 档位默认。

    风险门（危险命令/不透明命令/受保护目录/工作区外路径）在标准档与
    只读档下都走人工审批；放行档跳过审批但危险命令仍留 git 检查点。
    没有审批入口时一律 fail-closed（拒绝），绝不静默放行。
    """

    def __init__(self, mode: PermissionMode | str, *, workspace: Workspace,
                 rules: RuleEngine | None = None,
                 detector: DangerousCommandDetector | None = None,
                 approver: Approver | None = None) -> None:
        self.mode = (mode if isinstance(mode, PermissionMode)
                     else PermissionMode(normalize_mode(str(mode))))
        self.sandbox = PathSandbox(workspace)
        self.rules = rules or RuleEngine()
        self.detector = detector or DangerousCommandDetector()
        self.approver = approver
        self.session_approvals: set[str] = set()

    async def _request(self, key: str, arguments: dict[str, Any],
                       reason: str) -> Decision:
        if key in self.session_approvals:
            return Decision(True, "已在本会话批准", source="approval")
        if self.approver is None:
            return Decision(False, "需要用户批准，但没有可用的批准入口",
                            source="approval")
        result = self.approver(key, arguments, reason)
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, bool):
            verdict = "once" if result else "deny"
        else:
            verdict = parse_approval(str(result))
        if verdict == "always":
            self.session_approvals.add(key)
            return Decision(True, "用户已批准（本会话有效）", source="approval")
        if verdict == "once":
            return Decision(True, "用户已批准（一次）", source="approval")
        return Decision(False, f"用户拒绝该操作：{reason}", source="approval")

    async def check(self, tool: Tool, arguments: dict[str, Any],
                    context: ToolContext) -> Decision:
        try:
            tool.validate(arguments)
        except Exception as exc:
            return Decision(False, f"参数校验失败：{exc}", source="schema")

        rule_decision = self.rules.match(tool, arguments)
        if rule_decision is not None:
            return rule_decision

        non_read = bool(tool.effects - {"read"})
        command_text = _command_text(arguments)

        # 放行档：全放行，危险命令仍建议打检查点（/undo 兜底）。
        if self.mode is PermissionMode.TRUSTED:
            dangerous = self.detector.inspect(arguments)
            return Decision(True, "放行档默认允许", source="mode",
                            checkpoint=bool(dangerous))

        # 无害白名单：明确的只读/查询命令直接放行，免打扰。
        if command_text and command_text != "None" \
                and is_safe_command(command_text) and not is_opaque_command(command_text):
            return Decision(True, "已知无害命令", source="safe-list")

        # 风险门：危险命令 / 不透明命令 / 受保护目录 / 工作区外路径。
        dangerous = self.detector.inspect(arguments)
        opaque = bool(command_text and command_text != "None"
                      and is_opaque_command(command_text))
        protected = self.sandbox.protected_references(arguments)
        outside = self.sandbox.outside_paths(arguments)
        if dangerous or opaque or protected or outside:
            reasons: list[str] = []
            if dangerous:
                reasons.append(f"危险命令模式 {dangerous}")
            if opaque:
                reasons.append("不透明命令（解释器/管道包装，无法透视内容）")
            if protected:
                reasons.append("触碰受保护目录 .git/.conti")
            if outside:
                reasons.append("引用工作区外路径 " + ", ".join(outside[:3]))
            decision = await self._request(
                "gate:" + "|".join(reasons)[:120], arguments, "；".join(reasons)
            )
            return Decision(decision.allowed, decision.reason,
                            source=decision.source,
                            checkpoint=decision.allowed)

        # 只读档：非读操作需要审批（用户可批准一次放行）。
        if non_read and self.mode is PermissionMode.READ_ONLY:
            return await self._request(
                f"write:{tool.name}", arguments,
                f"只读档：工具 {tool.name} 需要写/执行权限"
            )

        return Decision(True, "标准档默认允许", source="mode")


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
            "schema_version": 2,
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
                "checkpoint": decision.checkpoint,
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
