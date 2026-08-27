from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_message(content: str | None = None,
                      tool_calls: list[ToolCall] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def tool_message(call: ToolCall, content: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
        "is_error": is_error,
    }
