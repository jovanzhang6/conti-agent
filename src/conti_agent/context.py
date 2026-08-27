from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


Summarizer = Callable[[list[dict[str, Any]], str], str]


@dataclass(frozen=True)
class ContextPlan:
    messages: list[dict[str, Any]]
    compacted_count: int
    estimated_tokens: int


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        return estimate_tokens(list(value.keys())) + estimate_tokens(list(value.values()))
    if isinstance(value, (list, tuple)):
        return sum(estimate_tokens(item) for item in value)
    return max(1, len(str(value)) // 4)


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(message) + 4 for message in messages)


class ContextManager:
    """基于保守 token 估算的历史窗口规划和压缩。"""

    def __init__(self, *, context_window: int, max_output_tokens: int = 8192,
                 tool_schema_tokens: int = 0) -> None:
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.tool_schema_tokens = tool_schema_tokens

    @property
    def budget(self) -> int:
        return max(1000, self.context_window - self.max_output_tokens - self.tool_schema_tokens)

    def needs_compaction(self, messages: list[dict[str, Any]]) -> bool:
        return estimate_message_tokens(messages) > self.budget

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

    def compact(self, messages: list[dict[str, Any]], summarizer: Summarizer,
                instruction: str = "请保留任务目标、关键路径、已验证结论和待办事项。",
                keep_recent: int = 4) -> tuple[list[dict[str, Any]], str, int]:
        prefix: list[dict[str, Any]] = []
        remainder = list(messages)
        while remainder and remainder[0].get("role") == "system":
            prefix.append(remainder.pop(0))
        old = remainder[:-keep_recent] if keep_recent < len(remainder) else remainder
        recent = remainder[len(old):]
        summary = summarizer(old, instruction)
        compacted = [
            *prefix,
            {"role": "system", "content": "[历史摘要]\n" + summary},
            *recent,
        ]
        return compacted, summary, len(old)
