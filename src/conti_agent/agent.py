from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .errors import AgentIterationLimit, ProviderError
from .events import AgentEvent, event
from .messages import ToolCall, tool_message
from .permissions import AuditLogger, PermissionChecker, execute_tool_with_permissions
from .providers import Provider
from .tools import ToolContext, ToolRegistry, execute_tool


@dataclass(frozen=True)
class AgentRunConfig:
    max_tool_iterations: int = 32
    retry_attempts: int = 2
    retry_base_seconds: float = 0.05


class Agent:
    """确定性的模型/工具执行循环。"""

    def __init__(self, provider: Provider, registry: ToolRegistry,
                 context: ToolContext, config: AgentRunConfig | None = None,
                 permission_checker: PermissionChecker | None = None,
                 auditor: AuditLogger | None = None,
                 session_store=None, session_id: str = "") -> None:
        self.provider = provider
        self.registry = registry
        self.context = context
        self.config = config or AgentRunConfig()
        self.permission_checker = permission_checker
        self.auditor = auditor
        self.session_store = session_store
        self.session_id = session_id

    async def _complete_with_retry(self, messages: list[dict[str, Any]],
                                   emit: Any) -> Any:
        last_error: ProviderError | None = None
        for attempt in range(self.config.retry_attempts + 1):
            try:
                def stream(event_type: str, payload: dict[str, Any]) -> None:
                    emit(event(event_type, **payload))
                return await self.provider.complete(messages, self.registry, stream)
            except ProviderError as exc:
                last_error = exc
                if not exc.transient or attempt >= self.config.retry_attempts:
                    raise
                emit(event("run.retry", attempt=attempt + 1, error=str(exc)))
                await asyncio.sleep(self.config.retry_base_seconds * (2 ** attempt))
        raise last_error or ProviderError("provider failed")

    async def run(self, messages: list[dict[str, Any]]) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

        def emit(item: AgentEvent) -> None:
            queue.put_nowait(item)

        async def runner() -> None:
            started = time.monotonic()
            emit(event("run.started", session_id=self.context.session_id,
                       workspace=str(self.context.workspace)))
            try:
                total_input = total_output = 0
                for iteration in range(1, self.config.max_tool_iterations + 1):
                    response = await self._complete_with_retry(messages, emit)
                    if response.usage:
                        total_input += response.usage.input_tokens
                        total_output += response.usage.output_tokens
                        emit(event("usage.recorded",
                                   input_tokens=response.usage.input_tokens,
                                   output_tokens=response.usage.output_tokens))
                    messages.append(response.assistant_message())
                    emit(event("message.created", role="assistant", text=response.text,
                               tool_calls=[
                                   {"id": call.id, "name": call.name, "arguments": call.arguments}
                                   for call in (response.tool_calls or [])
                               ]))
                    if not response.has_tool_calls:
                        emit(event("run.completed", iterations=iteration,
                                   stop_reason=response.stop_reason,
                                   duration=time.monotonic() - started,
                                   usage={"input_tokens": total_input,
                                          "output_tokens": total_output}))
                        return

                    for call in response.tool_calls or []:
                        emit(event("tool.requested", tool_call_id=call.id,
                                   tool_name=call.name, arguments=call.arguments))
                        if self.permission_checker is None:
                            from .tools import execute_tool as run_tool
                            result = await run_tool(self.registry, call, self.context)
                            emit(event("tool.approved", tool_call_id=call.id,
                                       decision="allowed", source="unchecked"))
                        else:
                            result = await execute_tool_with_permissions(
                                self.registry, call, self.context,
                                self.permission_checker, self.auditor,
                            )
                            emit(event("tool.approved", tool_call_id=call.id,
                                       decision="denied" if result.is_error else "allowed"))
                        emit(event("tool.completed", tool_call_id=call.id,
                                   tool_name=call.name, output=result.output,
                                   is_error=result.is_error, metadata=result.metadata))
                        messages.append(tool_message(call, result.output,
                                                     is_error=result.is_error))
                        if self.session_store and self.session_id:
                            self.session_store.append_message(
                                self.session_id,
                                tool_message(call, result.output, is_error=result.is_error),
                            )
                emit(event("run.failed", error="maximum tool iterations reached",
                           error_type="AgentIterationLimit"))
                raise AgentIterationLimit("maximum tool iterations reached")
            except Exception as exc:
                emit(event("run.failed", error=str(exc), error_type=type(exc).__name__))
                raise
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
