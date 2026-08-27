from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from .errors import ProviderError
from .messages import ToolCall, Usage
from .tools import ToolRegistry


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
                       stream_handler: StreamHandler | None = None) -> ProviderResponse:
        raise NotImplementedError


class FakeProvider(Provider):
    """A deterministic provider for tests and offline demonstrations."""

    protocol = "fake"

    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None) -> ProviderResponse:
        self.calls.append([dict(message) for message in messages])
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

    def _convert_tools(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
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
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            converted.append(item)
        return converted

    async def complete(self, messages: list[dict[str, Any]], registry: ToolRegistry,
                       stream_handler: StreamHandler | None = None) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "max_tokens": self.max_output_tokens,
        }
        tools = self._convert_tools(registry)
        if tools:
            payload["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
            tool_calls=_json_tool_calls(message.get("tool_calls")),
            usage=usage,
            stop_reason=choice.get("finish_reason") or "end_turn",
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
                       stream_handler: StreamHandler | None = None) -> ProviderResponse:
        system, converted = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            payload["system"] = system
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
