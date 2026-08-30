from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 记忆分区（HIGHLIGHTS 亮点 2）。
MEMORY_SECTIONS = ("用户偏好", "项目事实", "踩过的坑")

# 条目行格式：- #P01 (x3, 2026-08-30, dream) 内容（source 段可省略）
_ENTRY_RE = re.compile(
    r"^-\s*#(?P<id>P\d+)\s*"
    r"\(x(?P<count>\d+),\s*(?P<date>[\d-]+)(?:,\s*(?P<source>[a-z_]+))?\)\s*"
    r"(?P<statement>.+)$"
)
# 人工编辑丢失 id 的行：- (x1, 2026-08-30) 内容 或 - 内容 → 加载时补号。
_IDLESS_RE = re.compile(
    r"^-\s*(?:\(x(?P<count>\d+),\s*(?P<date>[\d-]+)(?:,\s*(?P<source>[a-z_]+))?\)\s*)?(?P<statement>.+)$"
)
# 提取调用产出的操作标记：[new] / [matches:P03] / [supersedes:P03]
_TAG_RE = re.compile(
    r"^\[(?P<action>new|matches|supersedes)(?::(?P<target>P\d+))?\]\s*(?P<statement>.+)$",
    re.IGNORECASE,
)

_INJECT_BUDGET = 2_000


@dataclass
class MemoryEntry:
    id: str
    section: str
    statement: str
    count: int = 1
    source: str = "manual"
    last_seen: str = ""


def normalize_statement(text: str) -> str:
    """廉价归一化：小写、去空白与标点，用于合并端的二道去重防线。"""
    return re.sub(r"[\s，。；：、,.;:!？?'\"()（）\-—_/\\]+", "", text.lower())


def _entry_id(number: int) -> str:
    return f"P{number:02d}"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class MemoryStore:
    """workspace 级 MEMORY.md 的读写与渲染（人可直接编辑）。"""

    def __init__(self, conti_root: Path) -> None:
        self.directory = Path(conti_root) / "memory"
        self.path = self.directory / "MEMORY.md"

    # ---------- 读取 / 解析 ----------

    def load(self) -> list[MemoryEntry]:
        """解析 MEMORY.md；人工编辑破坏 id 时顺带补号。"""
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        entries: list[MemoryEntry] = []
        section = MEMORY_SECTIONS[0]
        for line in text.splitlines():
            heading = line.strip().lstrip("#").strip()
            if line.startswith("##") and heading in MEMORY_SECTIONS:
                section = heading
                continue
            match = _ENTRY_RE.match(line.strip())
            if match:
                entries.append(MemoryEntry(
                    id=match.group("id"),
                    section=section,
                    statement=match.group("statement").strip(),
                    count=max(1, int(match.group("count"))),
                    source=match.group("source") or "manual",
                    last_seen=match.group("date"),
                ))
                continue
            idless = _IDLESS_RE.match(line.strip())
            if idless and line.strip().startswith("-"):
                entries.append(MemoryEntry(
                    id="",
                    section=section,
                    statement=idless.group("statement").strip(),
                    count=max(1, int(idless.group("count") or 1)),
                    source=idless.group("source") or "manual",
                    last_seen=idless.group("date") or "",
                ))
        return self._reassign_missing_ids(entries)

    def _reassign_missing_ids(self, entries: list[MemoryEntry]) -> list[MemoryEntry]:
        number = 1
        taken = {entry.id for entry in entries if entry.id}
        for entry in entries:
            if entry.id:
                continue
            while _entry_id(number) in taken:
                number += 1
            entry.id = _entry_id(number)
            taken.add(entry.id)
        return entries

    # ---------- 写入 / 渲染 ----------

    def save(self, entries: list[MemoryEntry]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        lines = ["# MEMORY", "",
                 "（本文件可人工编辑；模型于会话启动读取一次，"
                 "修改在下个会话生效。条目格式：- #P01 (x次数, 日期) 内容）", ""]
        for section in MEMORY_SECTIONS:
            owned = [entry for entry in entries if entry.section == section]
            if not owned:
                continue
            lines.append(f"## {section}")
            for entry in owned:
                lines.append(
                    f"- #{entry.id} (x{min(99, entry.count)}, {entry.last_seen}"
                    f", {entry.source}) {entry.statement}"
                )
            lines.append("")
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(self.path)

    def inject_text(self, budget: int = _INJECT_BUDGET) -> str:
        """会话启动注入 system prompt 的记忆文本（带截断预算）。"""
        entries = self.load()
        if not entries:
            return ""
        lines = [f"- #{entry.id} [{entry.section}] (x{entry.count}) {entry.statement}"
                 for entry in entries]
        body = "\n".join(lines)
        if len(body) > budget:
            body = body[:budget] + "\n…（已截断）"
        return ("长期记忆（来自既往会话；文件位于 .conti/memory/MEMORY.md，"
                "可直接编辑，修改下个会话生效）：\n" + body)

    def index_text(self) -> str:
        """给提取调用对表用的条目索引（id + 分区 + 内容）。"""
        entries = self.load()
        if not entries:
            return ""
        return "\n".join(
            f"#{entry.id} [{entry.section}] {entry.statement}" for entry in entries
        )


def merge_findings(entries: list[MemoryEntry], findings: list[dict[str, Any]],
                   *, source: str, today: str | None = None
                   ) -> tuple[list[MemoryEntry], list[str]]:
    """三层写入口共用的合并纯函数（HIGHLIGHTS 亮点 2）。

    findings 每项：{"action": "new"|"matches"|"supersedes", "target": "P03"|None,
    "section": 分区, "statement": 内容}。语义判断发生在提取调用里（模型对表），
    这里只做机械折叠：matches 计数、new 分配 id、supersedes 覆盖重置；
    幻觉 id 降级为 new；同分区归一化重复合并计数；重复合并只加计数（幂等）。

    返回（新条目列表, 变更说明列表）。
    """
    today = today or _today()
    working = [MemoryEntry(**vars(entry)) for entry in entries]
    notes: list[str] = []
    for finding in findings:
        statement = str(finding.get("statement", "")).strip()
        if not statement:
            continue
        section = finding.get("section") if finding.get("section") in MEMORY_SECTIONS \
            else MEMORY_SECTIONS[0]
        action = str(finding.get("action", "new")).lower()
        target = finding.get("target")

        target_entry = None
        if action in {"matches", "supersedes"} and target:
            target_entry = next((e for e in working if e.id == target), None)
            if target_entry is None:
                action = "new"  # 幻觉 id：降级为 new，不崩

        if action == "supersedes" and target_entry is not None:
            target_entry.statement = statement
            target_entry.count = 1
            target_entry.last_seen = today
            target_entry.source = source
            notes.append(f"{target_entry.id} 已覆盖：{statement}")
            continue

        if action == "matches" and target_entry is not None:
            target_entry.count = min(99, target_entry.count + 1)
            target_entry.last_seen = today
            notes.append(f"{target_entry.id} 计数 +1")
            continue

        # 归一化重复：同分区近似同文 → 只加计数。
        key = normalize_statement(statement)
        existing = next(
            (e for e in working
             if e.section == section and normalize_statement(e.statement) == key),
            None,
        )
        if existing is not None:
            existing.count = min(99, existing.count + 1)
            existing.last_seen = today
            notes.append(f"{existing.id} 计数 +1")
            continue

        number = 1
        taken = {e.id for e in working}
        while _entry_id(number) in taken:
            number += 1
        entry = MemoryEntry(id=_entry_id(number), section=section,
                            statement=statement, count=1, source=source,
                            last_seen=today)
        working.append(entry)
        notes.append(f"新增 {entry.id}：{statement}")
    return working, notes


def parse_memory_findings(text: str) -> list[dict[str, Any]]:
    """从"值得长期记住的事"小节或 dream 模型输出解析 findings。

    支持两种形态：
    1. 行式：- [new] 内容 / - [matches:P02] 内容
    2. JSON 数组（dream 批量输出）：[{"action": "new", ...}, ...]
    """
    text = text.strip()
    if text.startswith("["):
        try:
            raw = json.loads(text)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
        except json.JSONDecodeError:
            pass
    findings: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        match = _TAG_RE.match(stripped)
        if not match or match.group("statement").strip() in {"无", ""}:
            continue
        findings.append({
            "action": match.group("action").lower(),
            "target": match.group("target"),
            "section": MEMORY_SECTIONS[0],
            "statement": match.group("statement").strip(),
        })
    return findings


def split_section(text: str, heading: str) -> str:
    """取 Markdown 指定小节的正文（到下一个 ## 为止）。"""
    pattern = re.compile(
        r"^##\s*" + re.escape(heading) + r"\s*$\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


def session_to_units(messages: list[dict[str, Any]],
                     assistant_chars: int = 400) -> list[dict[str, str]]:
    """把会话消息压成 dream 对话单元：用户消息 + 它得到的助手回复摘要。

    工具结果永不进入 dream 输入（成本控制 + 工具输出对偏好提炼无意义）。
    单条超长用户消息（粘贴大段日志）截头去尾——偏好信号不会埋在
    超长粘贴的正文里。
    """
    units: list[dict[str, str]] = []
    pending_user: str | None = None

    def flush() -> None:
        nonlocal pending_user
        if pending_user is not None:
            units.append({"user": pending_user, "assistant": ""})
            pending_user = None

    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "").strip()
        if role == "assistant":
            if content and pending_user is not None:
                units.append({"user": pending_user,
                              "assistant": content[:assistant_chars]})
                pending_user = None
            continue
        if role != "user" or not content:
            continue
        if content.startswith("[历史摘要]"):
            continue  # 压缩摘要不是用户输入
        flush()  # 前一问没有等到回复
        if len(content) > 2_600:
            omitted = len(content) - 2_400
            content = (content[:1_200] + f"\n…（省略 {omitted} 字）\n"
                       + content[-1_200:])
        pending_user = content
    flush()
    return units
