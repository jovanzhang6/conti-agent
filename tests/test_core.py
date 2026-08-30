from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from typing import Any

from conti_agent.agent import Agent, AgentRunConfig
from conti_agent.errors import AgentIterationLimit, ProviderError, ToolValidationError
from conti_agent.events import AgentEvent
from conti_agent.messages import ToolCall, Usage, user_message
from conti_agent.providers import urllib_transport
from conti_agent.providers import (
    AnthropicCompatibleProvider,
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderResponse,
    _load_tool_arguments,
)
from conti_agent.tools import Tool, ToolContext, ToolRegistry, ToolResult, execute_tool


class EchoTool(Tool):
    name = "echo"
    description = "Return the supplied text."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    effects = frozenset({"read"})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(output=arguments["text"], metadata={"workspace": context.workspace.name})


class CoreTestCase(unittest.IsolatedAsyncioTestCase):
    def test_registry_rejects_duplicate(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        with self.assertRaises(ToolValidationError):
            registry.register(EchoTool())

    async def test_execute_tool_validates_and_measures(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        context = ToolContext(workspace=Path("."))
        call = ToolCall("one", "echo", {"text": "hello"})
        result = await execute_tool(registry, call, context)
        self.assertFalse(result.is_error)
        self.assertEqual(result.output, "hello")
        self.assertIn("elapsed", result.metadata)

    async def test_execute_tool_returns_validation_error(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        call = ToolCall("one", "echo", {})
        result = await execute_tool(registry, call, ToolContext(workspace=Path(".")))
        self.assertTrue(result.is_error)
        self.assertIn("text is required", result.output)

    async def collect(self, agent: Agent, messages: list[dict[str, Any]]) -> list[AgentEvent]:
        return [event async for event in agent.run(messages)]

    async def test_agent_direct_answer(self) -> None:
        provider = FakeProvider([ProviderResponse(text="ready", usage=Usage(4, 2))])
        agent = Agent(provider, ToolRegistry(), ToolContext(workspace=Path(".")))
        events = await self.collect(agent, [user_message("hi")])
        self.assertEqual(events[0].type, "run.started")
        self.assertEqual(events[-1].type, "run.completed")
        self.assertTrue(any(item.type == "text.delta" and item.payload["text"] == "rea" for item in events))
        self.assertEqual(len(provider.calls), 1)

    async def test_agent_tool_round(self) -> None:
        provider = FakeProvider([
            ProviderResponse(tool_calls=[ToolCall("one", "echo", {"text": "called"})]),
            ProviderResponse(text="done"),
        ])
        registry = ToolRegistry()
        registry.register(EchoTool())
        agent = Agent(provider, registry, ToolContext(workspace=Path(".")))
        messages = [user_message("use echo")]
        events = await self.collect(agent, messages)
        self.assertTrue(any(item.type == "tool.completed" and
                            item.payload["output"] == "called" for item in events))
        self.assertEqual(events[-1].payload["iterations"], 2)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-2]["content"], "called")

    async def test_agent_iteration_limit(self) -> None:
        call = ToolCall("one", "echo", {"text": "again"})
        provider = FakeProvider([ProviderResponse(tool_calls=[call])] * 2)
        registry = ToolRegistry()
        registry.register(EchoTool())
        agent = Agent(provider, registry, ToolContext(workspace=Path(".")),
                      AgentRunConfig(max_tool_iterations=2, retry_attempts=0))
        with self.assertRaises(AgentIterationLimit):
            await self.collect(agent, [user_message("loop")])

    async def test_provider_transient_retry(self) -> None:
        class FlakyProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__([ProviderResponse(text="ok")])
                self.attempts = 0

            async def complete(self, messages, registry, stream_handler=None):
                self.attempts += 1
                if self.attempts == 1:
                    raise ProviderError("temporary", transient=True)
                return await super().complete(messages, registry, stream_handler)

        provider = FlakyProvider()
        agent = Agent(provider, ToolRegistry(), ToolContext(workspace=Path(".")),
                      AgentRunConfig(retry_base_seconds=0))
        events = await self.collect(agent, [user_message("hi")])
        self.assertTrue(any(item.type == "run.retry" for item in events))
        self.assertEqual(provider.attempts, 2)

    def test_openai_transport_mapping(self) -> None:
        captured: dict[str, Any] = {}
        def transport(url: str, method: str, headers: dict[str, str], payload: dict[str, Any]):
            captured.update({"url": url, "headers": headers, "payload": payload})
            return {
                "choices": [{"message": {
                    "content": "hi",
                    "tool_calls": [{"id": "x", "function": {
                        "name": "echo", "arguments": "{\"text\":\"a\"}"
                    }}],
                    "finish_reason": "tool_calls",
                }}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }
        provider = OpenAICompatibleProvider(
            base_url="https://example/v1", model="m", api_key="secret", transport=transport
        )
        registry = ToolRegistry(); registry.register(EchoTool())
        async def call():
            return await provider.complete([user_message("q")], registry)
        response = asyncio.run(call())
        self.assertEqual(captured["url"], "https://example/v1/chat/completions")
        self.assertEqual(response.tool_calls[0].name, "echo")

    def test_openai_tool_choice_none_keeps_tools_in_payload(self) -> None:
        """tool_choice="none" 是请求参数、不影响消息前缀；tool schema
        必须原样保留以命中 KV cache（HIGHLIGHTS 1.3.A）。"""
        captured: dict[str, Any] = {}
        def transport(url: str, method: str, headers: dict[str, str], payload: dict[str, Any]):
            captured["payload"] = payload
            return {
                "choices": [{"message": {"content": "摘要"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }
        provider = OpenAICompatibleProvider(
            base_url="https://example/v1", model="m", api_key="secret", transport=transport
        )
        registry = ToolRegistry()
        registry.register(EchoTool())
        async def call():
            return await provider.complete([user_message("q")], registry,
                                           tool_choice="none")
        asyncio.run(call())
        self.assertEqual(captured["payload"]["tool_choice"], "none")
        self.assertIn("tools", captured["payload"])
        # 不传 tool_choice 时 payload 不携带该键（主请求行为不变）。
        async def call2():
            return await provider.complete([user_message("q")], registry)
        asyncio.run(call2())
        self.assertNotIn("tool_choice", captured["payload"])

    def test_openai_stream_request_mapping(self) -> None:
        provider = OpenAICompatibleProvider(
            base_url="https://example/v1", model="m", api_key="secret",
            transport=urllib_transport,
        )
        captured: dict[str, Any] = {}
        deltas: list[str] = []
        def fake_stream(url: str, headers: dict[str, str],
                        payload: dict[str, Any], handler) -> Any:
            captured.update({"url": url, "payload": payload})
            handler("text.delta", {"text": " streamed"})
            from conti_agent.providers import ProviderResponse
            return ProviderResponse(text="streamed")
        provider._stream_request = fake_stream
        registry = ToolRegistry()
        registry.register(EchoTool())
        async def call():
            return await provider.complete(
                [user_message("q")], registry,
                lambda kind, payload: deltas.append(payload["text"]),
            )
        response = asyncio.run(call())
        self.assertTrue(captured["payload"]["stream"])
        self.assertEqual(deltas, [" streamed"])
        self.assertEqual(response.text, "streamed")

    def test_tool_arguments_repair_unescaped_backslashes(self) -> None:
        # 模型写 Windows 路径漏掉反斜杠转义（非法 JSON）也能修复解析。
        invalid = '{"path": "D:\\conti-agent\\src\\x.py"}'
        parsed = _load_tool_arguments(invalid)
        self.assertEqual(parsed["path"], "D:\\conti-agent\\src\\x.py")
        # 合法 JSON 原样通过。
        valid = '{"note": "a\\\\b"}'
        self.assertEqual(_load_tool_arguments(valid), {"note": "a\\b"})
        # 无法修复时保持原有报错语义。
        with self.assertRaises(ProviderError):
            _load_tool_arguments('{"path": "unfinished')

    def test_anthropic_transport_mapping(self) -> None:
        def transport(url: str, method: str, headers: dict[str, str], payload: dict[str, Any]):
            self.assertIn("x-api-key", headers)
            self.assertEqual(payload["messages"][0]["role"], "user")
            return {"content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 1, "output_tokens": 2}}
        provider = AnthropicCompatibleProvider(
            base_url="https://example", model="m", api_key="secret", transport=transport
        )
        registry = ToolRegistry()
        registry.register(EchoTool())
        async def call():
            return await provider.complete([user_message("q")], registry)
        response = asyncio.run(call())
        self.assertEqual(response.text, "hello")


if __name__ == "__main__":
    unittest.main()
