from __future__ import annotations

import asyncio
from dataclasses import replace
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ToolValidationError
from .schema import validate_value


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    session_id: str = ""
    task_id: str = ""
    profile: str = "default"
    services: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    effects: frozenset[str] = frozenset()

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolValidationError("arguments must be an object")
        validate_value(arguments, self.parameters)

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if not tool.name:
            raise ToolValidationError("tool name is required")
        if tool.name in self._tools and not replace:
            raise ToolValidationError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolValidationError(f"unknown tool: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def filter(self, names: list[str]) -> "ToolRegistry":
        registry = ToolRegistry()
        for name in names:
            registry.register(self.get(name))
        return registry


async def execute_tool(registry: ToolRegistry, call: Any, context: ToolContext) -> ToolResult:
    started = time.monotonic()
    try:
        tool = registry.get(call.name)
        tool.validate(call.arguments)
        result = await tool.execute(call.arguments, context)
        if not isinstance(result, ToolResult):
            result = ToolResult(output=str(result))
    except Exception as exc:
        return ToolResult(
            output=f"{type(exc).__name__}: {exc}",
            is_error=True,
            metadata={"elapsed": round(time.monotonic() - started, 6)},
        )
    return replace(result, metadata={
        **result.metadata,
        "elapsed": round(time.monotonic() - started, 6),
    })
