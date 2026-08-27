from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from .errors import ToolExecutionError
from .tools import Tool, ToolContext, ToolRegistry, ToolResult


class ExternalConnector:
    async def start(self) -> None:
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class StdioExternalConnector(ExternalConnector):
    """基于行分隔 JSON-RPC 的外部工具进程连接器。"""

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        self.command = command
        self.env = env or {}
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env or None,
        )

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise ToolExecutionError("外部工具进程尚未启动")
        request_id = uuid.uuid4().hex
        line = json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
        }, ensure_ascii=False) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), 10)
        if not raw:
            raise ToolExecutionError("外部工具进程关闭了输出")
        response = json.loads(raw.decode("utf-8"))
        if response.get("id") != request_id:
            raise ToolExecutionError("外部工具响应 ID 不匹配")
        if "error" in response:
            raise ToolExecutionError(f"外部工具错误：{response['error']}")
        return dict(response.get("result", {}))

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()


class ExternalToolManager:
    def __init__(self, connector: ExternalConnector, namespace: str) -> None:
        self.connector = connector
        self.namespace = namespace
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        await self.connector.start()
        await self.connector.request("initialize", {
            "protocol": "conti-external-tools/1",
            "namespace": self.namespace,
        })
        listed = await self.connector.request("tools/list", {})
        self.tools = list(listed.get("tools", []))

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.connector.request("tools/call", {"name": name, "arguments": arguments})

    async def register(self, registry: ToolRegistry) -> None:
        for raw in self.tools:
            registry.register(ExternalTool(self, raw), replace=True)

    async def close(self) -> None:
        await self.connector.close()


class ExternalTool(Tool):
    def __init__(self, manager: ExternalToolManager, raw: dict[str, Any]) -> None:
        self.manager = manager
        self._raw = raw
        self.name = f"{manager.namespace}.{raw['name']}"
        self.description = raw.get("description", "")
        self.parameters = raw.get("input_schema", {
            "type": "object", "properties": {}, "required": []
        })
        self.effects = frozenset(raw.get("effects", ["execute"]))

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        local_name = self._raw["name"]
        response = await self.manager.call(local_name, arguments)
        content = response.get("content")
        if not isinstance(content, str):
            content = json.dumps(response, ensure_ascii=False)
        return ToolResult(content, {"namespace": self.manager.namespace,
                                    "tool": local_name},
                          is_error=bool(response.get("is_error", False)))
