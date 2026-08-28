from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


Summarizer = Callable[[list[dict[str, Any]], str], str]

# 压缩输出预留：给摘要生成留的安全边际（参考实际 p99 数据的保守值）。
COMPACT_OUTPUT_RESERVE = 20_000
# 当轮余量：压缩后到下次检查之间还要发的内容预留。
ROUND_HEADROOM = 13_000
# 自动压缩时保留的近期原文预算（token）。
RECENT_KEEP_TOKENS = 10_000
# 工具结果落盘阈值：单结果字符数 / 单轮累计字符数 / 上下文保留的预览长度。
SPILL_SINGLE_CHARS = 50_000
SPILL_ROUND_CHARS = 200_000
SPILL_PREVIEW_CHARS = 2_000

# 宽字符区间：中日韩文字与全角标点，按约 1 token/字估算。
_WIDE_RANGES = (
    (0x1100, 0x11FF),   # 谚文字母
    (0x2E80, 0x9FFF),   # CJK 部首、标点、统一表意文字
    (0xA960, 0xA97F),   # 谚文扩展
    (0xAC00, 0xD7FF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFE30, 0xFE4F),   # CJK 兼容形式
    (0xFF00, 0xFF60),   # 全角形式
    (0x20000, 0x3134F), # CJK 扩展 B-F
)


def _is_wide_char(ch: str) -> bool:
    code = ord(ch)
    return any(start <= code <= end for start, end in _WIDE_RANGES)


@dataclass(frozen=True)
class ContextPlan:
    messages: list[dict[str, Any]]
    compacted_count: int
    estimated_tokens: int


def estimate_tokens(value: Any) -> int:
    """按字符类别估算 token：宽字符（中日韩）约 1 token/字，其余约 4 字符/token。"""
    if value is None:
        return 0
    if isinstance(value, dict):
        return estimate_tokens(list(value.keys())) + estimate_tokens(list(value.values()))
    if isinstance(value, (list, tuple)):
        return sum(estimate_tokens(item) for item in value)
    if isinstance(value, str):
        if not value:
            return 0
        wide = sum(1 for ch in value if _is_wide_char(ch))
        narrow = len(value) - wide
        return wide + (narrow + 3) // 4
    return max(1, len(str(value)) // 4)


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(message) + 4 for message in messages)


def default_summary(old: list[dict[str, Any]], instruction: str = "请保留任务目标、关键路径、已验证结论和待办事项。") -> str:
    """无模型时的回退摘要：仅罗列早期用户目标。"""
    user_goals = [item.get("content", "") for item in old if item.get("role") == "user"]
    return "\n".join([
        "压缩说明：" + instruction,
        "早期用户目标：",
        *[f"- {goal}" for goal in user_goals[-8:]],
    ])


class ResultSpiller:
    """工具结果落盘：超大结果写入文件，上下文里只保留预览和文件路径。

    不调用模型、不丢信息——原始数据完整保留在磁盘上，模型需要时
    可以用 workspace_read 读回。
    """

    def __init__(self, directory: Path, *, single_limit: int = SPILL_SINGLE_CHARS,
                 round_limit: int = SPILL_ROUND_CHARS,
                 preview_chars: int = SPILL_PREVIEW_CHARS) -> None:
        self.directory = Path(directory)
        self.single_limit = single_limit
        self.round_limit = round_limit
        self.preview_chars = preview_chars
        self.round_total = 0
        self.sequence = 0
        self.spilled: list[str] = []
        self.directory.mkdir(parents=True, exist_ok=True)

    def reset_round(self) -> None:
        self.round_total = 0

    def process(self, tool_name: str, output: str) -> str:
        """超过阈值的结果落盘，返回放进上下文的替换内容。"""
        size = len(output)
        if size <= self.single_limit and self.round_total + size <= self.round_limit:
            self.round_total += size
            return output
        self.sequence += 1
        self.round_total += size
        path = self.directory / f"{time.strftime('%H%M%S')}-{self.sequence:03d}-{tool_name}.txt"
        path.write_text(output, encoding="utf-8")
        self.spilled.append(str(path))
        preview = output[: self.preview_chars]
        return (
            f"[工具结果过大，已落盘] 工具：{tool_name}，原始大小 {size} 字符，"
            f"完整内容：{path}\n"
            f"---- 以下为前 {self.preview_chars} 字符预览，需要更多内容请用 workspace_read 读取该文件 ----\n"
            f"{preview}"
        )


class ContextManager:
    """基于字符类别估算的历史窗口规划和压缩。"""

    def __init__(self, *, context_window: int, max_output_tokens: int = 8192,
                 tool_schema_tokens: int = 0) -> None:
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.tool_schema_tokens = tool_schema_tokens
        # 最近一次请求的精确用量（来自 provider 返回的 usage）。
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None

    @property
    def budget(self) -> int:
        return max(1000, self.context_window - self.max_output_tokens - self.tool_schema_tokens)

    @property
    def compaction_trigger(self) -> int:
        """自动压缩触发点：窗口扣掉压缩输出预留和当轮余量。"""
        return max(1000, self.context_window - COMPACT_OUTPUT_RESERVE - ROUND_HEADROOM)

    def observe_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.last_input_tokens = input_tokens
        self.last_output_tokens = output_tokens

    def projected_input_tokens(self, pending_messages: list[dict[str, Any]] | None = None) -> int:
        """预测下一次请求的输入规模：精确基数 + 待发送增量估算。"""
        if self.last_input_tokens is not None:
            base = self.last_input_tokens + (self.last_output_tokens or 0)
            return base + estimate_message_tokens(pending_messages or [])
        return estimate_message_tokens(pending_messages or [])

    def needs_compaction(self, messages: list[dict[str, Any]],
                         pending: list[dict[str, Any]] | None = None) -> bool:
        """pending 是上次精确用量之后新增的消息；无基线时退化为全量估算。"""
        if self.last_input_tokens is not None:
            projected = (self.last_input_tokens + (self.last_output_tokens or 0)
                         + estimate_message_tokens(pending or []))
        else:
            projected = estimate_message_tokens(messages)
        return projected > self.compaction_trigger

    def plan(self, messages: list[dict[str, Any]], keep_recent: int = 8) -> ContextPlan:
        prefix: list[dict[str, Any]] = []
        remainder = list(messages)
        while remainder and remainder[0].get("role") == "system":
            prefix.append(remainder.pop(0))
        keep = remainder[-max(1, keep_recent):]
        old = remainder[:-len(keep)] if keep else remainder
        planned = [*prefix, *old, *keep]
        while len(planned) > len(prefix) + 1 and estimate_message_tokens(planned) > self.budget:
            # 从旧向新丢弃非系统内容；最后一条用户请求始终保留。
            removable = next((index for index, message in enumerate(planned)
                              if index >= len(prefix) and message is not planned[-1]), None)
            if removable is None:
                break
            planned.pop(removable)
        return ContextPlan(planned, 0, estimate_message_tokens(planned))

    def split_for_compaction(self, messages: list[dict[str, Any]],
                             keep_tokens: int = RECENT_KEEP_TOKENS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """把历史切成（被压缩部分，保留原文部分）。

        保留部分从尾部按 token 预算向前取整条消息，然后把切点回退到
        最近的 user 消息——保证保留部分以 user 开头，tool/assistant
        配对（tool_use/tool_result）不会被切断，协议完整。
        返回 None 表示没有可安全压缩的内容。
        """
        if len(messages) < 3:
            return None
        kept = 0
        cut = len(messages)
        while cut > 1:
            index = cut - 1
            size = estimate_tokens(messages[index]) + 4
            if kept + size > keep_tokens and cut < len(messages):
                break
            kept += size
            cut = index
        # 切点回退到 user 边界；tool 消息必须跟在带 tool_calls 的 assistant 后面，
        # 从 user 起切可以保证保留片段内部配对完整。
        while cut < len(messages) and messages[cut].get("role") != "user":
            cut += 1
        if cut <= 0 or cut >= len(messages):
            return None
        return messages[:cut], messages[cut:]

    def compact(self, messages: list[dict[str, Any]], summary_text: str,
                *, keep_tokens: int = RECENT_KEEP_TOKENS,
                extras: str = "") -> tuple[list[dict[str, Any]], int]:
        """用给定的摘要文本压缩历史：[system, 摘要(user), 保留的近期原文]。"""
        prefix: list[dict[str, Any]] = []
        remainder = list(messages)
        while remainder and remainder[0].get("role") == "system":
            prefix.append(remainder.pop(0))
        split = self.split_for_compaction(remainder, keep_tokens)
        if split is None:
            return messages, 0
        old, tail = split
        content = "[历史摘要]\n" + summary_text
        if extras:
            content += "\n" + extras
        compacted = [*prefix, {"role": "user", "content": content}, *tail]
        return compacted, len(old)
