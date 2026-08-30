from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ProviderError
from .messages import ToolCall, Usage
from .tools import ToolRegistry

# JSON 合法转义字符：\u0000-\u001f 之外只允许 "\ / b f n r t u。
_UNESCAPED_BACKSLASH_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _load_tool_arguments(raw: str) -> dict[str, Any]:
    """解析流式聚合的工具参数。

    模型写 Windows 路径时经常漏掉反斜杠转义（D:\conti 而不是
    D:\\conti），严格解析会报 Invalid \\escape；先做一次修复重试：
    把后面不跟合法转义字符的孤立反斜杠补成双反斜杠。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _UNESCAPED_BACKSLASH_RE.sub(r'\\\\', raw)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"无效的工具参数流：{exc}") from exc


StreamHandler = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ProviderResponse:
    text: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: Usage | None = None
    stop_reason: str = "end_turn"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def assistant_message(self) -> dict[str, Any]:
        from .messages import assistant_message
        return assistant_message(self.text, self.tool_calls)


class Provider(ABC):
    protocol: str = ""

    @abstractmethod
    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None,
                       tool_choice: str | None = None) -> ProviderResponse:
        raise NotImplementedError


class FakeProvider(Provider):
    """用于测试和离线示例的确定性 Provider。"""

    protocol = "fake"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_choices: list[str | None] = []

    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None,
                       tool_choice: str | None = None) -> ProviderResponse:
        self.calls.append([dict(message) for message in messages])
        self.tool_choices.append(tool_choice)
        if not self.responses:
            raise AssertionError("FakeProvider received more calls than configured")
        response = self.responses.pop(0)
        if response.text and stream_handler:
            for index in range(0, len(response.text), 3):
                stream_handler("text.delta", {"text": response.text[index:index + 3]})
        return response


Transport = Callable[[str, str, dict[str, str], dict[str, Any]], dict[str, Any]]


def urllib_transport(url: str, method: str, headers: dict[str, str],
                     payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        transient = exc.code in {408, 409, 425, 429} or exc.code >= 500
        detail = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code}: {detail}", transient=transient,
                            status_code=exc.code) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ProviderError(f"provider connection failed: {exc}", transient=True) from exc


def _json_tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(raw or []):
        function = item.get("function", {})
        arguments = function.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderError(f"invalid tool arguments JSON: {exc}") from exc
        else:
            parsed = arguments
        calls.append(ToolCall(
            id=item.get("id") or f"call_{index}",
            name=function.get("name", ""),
            arguments=parsed,
        ))
    return calls


def _wire_tool_name(name: str) -> str:
    """把注册表命名空间转换成 OpenAI 允许的工具名。"""
    return re.sub(r"[^A-Za-z0-9_-]", "__", name)


class OpenAICompatibleProvider(Provider):
    protocol = "openai"

    def __init__(self, *, base_url: str, model: str, api_key: str,
                 transport: Transport | None = None,
                 max_output_tokens: int = 8192) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.transport = transport or urllib_transport
        self._tool_names_by_wire: dict[str, str] = {}

    def _convert_tools(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": _wire_tool_name(tool.name),
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in registry.all()
        ]

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            item = {key: value for key, value in message.items() if key != "is_error"}
            calls = item.get("tool_calls")
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": _wire_tool_name(call.name),
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            converted.append(item)
        return converted

    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None,
                       tool_choice: str | None = None) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "max_tokens": self.max_output_tokens,
        }
        tools = self._convert_tools(registry)
        if tools:
            payload["tools"] = tools
        # tool_choice 是请求参数、不属于消息前缀：schema 原样保留以维持
        # 与主请求的公共前缀（KV cache），"none" 仅禁止本次实际调用工具。
        if tool_choice is not None and tools:
            payload["tool_choice"] = tool_choice
        self._tool_names_by_wire = {
            _wire_tool_name(tool.name): tool.name for tool in registry.all()
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if stream_handler is not None and self.transport is urllib_transport:
            payload["stream"] = True
            return await asyncio.to_thread(
                self._stream_request,
                f"{self.base_url}/chat/completions",
                headers,
                payload,
                stream_handler,
            )
        raw = await asyncio.to_thread(
            self.transport, f"{self.base_url}/chat/completions", "POST", headers, payload
        )
        choices = raw.get("choices") or []
        if not choices:
            raise ProviderError("provider response contained no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content")
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
            output_tokens=int(usage_raw.get("completion_tokens", 0)),
        )
        return ProviderResponse(
            text=text,
            tool_calls=[
                ToolCall(
                    item.id,
                    self._tool_names_by_wire.get(item.name, item.name),
                    item.arguments,
                )
                for item in _json_tool_calls(message.get("tool_calls"))
            ],
            usage=usage,
            stop_reason=choice.get("finish_reason") or "end_turn",
        )

    def _stream_request(self, url: str, headers: dict[str, str],
                         payload: dict[str, Any],
                         stream_handler: StreamHandler) -> ProviderResponse:
        """同步读取 SSE；由 asyncio.to_thread 调用，不阻塞事件循环。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        stop_reason = "end_turn"
        usage = Usage()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_value = line[5:].strip()
                    if data_value == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_value)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(f"无效的流式响应片段：{exc}", transient=True) from exc
                    choices = chunk.get("choices") or []
                    if choices:
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        delta_text = delta.get("content")
                        if delta_text:
                            text_parts.append(str(delta_text))
                            stream_handler("text.delta", {"text": str(delta_text)})
                        for raw_call in delta.get("tool_calls") or []:
                            index = int(raw_call.get("index", 0))
                            item = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if raw_call.get("id"):
                                item["id"] = raw_call["id"]
                            function = raw_call.get("function") or {}
                            if function.get("name"):
                                item["name"] = function["name"]
                            if function.get("arguments"):
                                item["arguments"] += function["arguments"]
                        if choice.get("finish_reason"):
                            stop_reason = choice["finish_reason"]
                    usage_raw = chunk.get("usage")
                    if usage_raw:
                        usage = Usage(
                            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
                            output_tokens=int(usage_raw.get("completion_tokens", 0)),
                        )
        except urllib.error.HTTPError as exc:
            transient = exc.code in {408, 409, 425, 429} or exc.code >= 500
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"HTTP {exc.code}: {detail}", transient=transient,
                                status_code=exc.code) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise ProviderError(f"provider connection failed: {exc}", transient=True) from exc

        calls: list[ToolCall] = []
        for index in sorted(tool_parts):
            item = tool_parts[index]
            arguments = _load_tool_arguments(item["arguments"] or "{}")
            wire_name = item["name"]
            calls.append(ToolCall(
                item["id"] or f"call_{index}",
                self._tool_names_by_wire.get(wire_name, wire_name),
                arguments,
            ))
        text = "".join(text_parts)
        if not text and not calls:
            raise ProviderError("provider stream ended without content", transient=True)
        return ProviderResponse(
            text=text or None,
            tool_calls=calls,
            usage=usage,
            stop_reason=stop_reason,
        )


class AnthropicCompatibleProvider(Provider):
    protocol = "anthropic"

    def __init__(self, *, base_url: str, model: str, api_key: str,
                 transport: Transport | None = None,
                 max_output_tokens: int = 8192) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.transport = transport or urllib_transport

    def _convert_messages(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message["role"]
            if role == "system":
                system_parts.append(message["content"])
                continue
            if role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"],
                    }],
                })
                continue
            calls = message.get("tool_calls")
            if calls:
                converted.append({
                    "role": "assistant",
                    "content": [
                        *([{"type": "text", "text": message["content"]}] if message.get("content") else []),
                        *[
                            {
                                "type": "tool_use",
                                "id": call.id,
                                "name": call.name,
                                "input": call.arguments,
                            }
                            for call in calls
                        ],
                    ],
                })
                continue
            converted.append({"role": role, "content": message.get("content", "")})
        return "\n\n".join(system_parts), converted

    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None,
                       tool_choice: str | None = None) -> ProviderResponse:
        system, converted = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            payload["system"] = system
        # Anthropic 的 tool_choice 不支持 "none"：摘要请求省略工具字段，
        # 功能上同样禁止工具调用（与旧版空 registry 行为一致），不劣化现状。
        if tool_choice == "none":
            pass
        else:
            tools = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in registry.all()
            ]
            if tools:
                payload["tools"] = tools
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        raw = await asyncio.to_thread(
            self.transport, f"{self.base_url}/messages", "POST", headers, payload
        )
        content = raw.get("content") or []
        text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        calls = [
            ToolCall(block["id"], block["name"], block.get("input", {}))
            for block in content if block.get("type") == "tool_use"
        ]
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        )
        return ProviderResponse(text=text, tool_calls=calls, usage=usage,
                                stop_reason=raw.get("stop_reason") or "end_turn")
