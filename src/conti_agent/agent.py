from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .errors import AgentIterationLimit, ProviderError
from .events import AgentEvent, event
from .messages import ToolCall, tool_message
from .permissions import AuditLogger, PermissionChecker, execute_tool_with_permissions
from .tools import ToolResult
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
                 session_store=None, session_id: str = "",
                 hook_engine=None, result_spiller=None,
                 usage_observer=None, pre_request_hook=None,
                 checkpoint=None) -> None:
        self.provider = provider
        self.registry = registry
        self.context = context
        self.config = config or AgentRunConfig()
        self.permission_checker = permission_checker
        self.auditor = auditor
        self.session_store = session_store
        self.session_id = session_id
        self.hook_engine = hook_engine
        self.result_spiller = result_spiller
        # git 检查点管理器：危险/越界/受保护操作放行前先打检查点。
        self.checkpoint = checkpoint
        # 每次响应到达时同步记录精确用量与覆盖的消息条数。
        self.usage_observer = usage_observer
        # 每次向模型发请求之前调用：做上下文投影检查，超限就压缩。
        self.pre_request_hook = pre_request_hook

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

    def _handle_tool_result(self, call: ToolCall, result: ToolResult,
                            messages: list[dict[str, Any]], emit: Any) -> None:
        """统一发出工具结果、写回消息并持久化；超大结果先落盘。"""
        output = result.output
        if self.result_spiller is not None and output:
            output = self.result_spiller.process(call.name, output)
        emit(event("tool.completed", tool_call_id=call.id,
                   tool_name=call.name, output=output,
                   is_error=result.is_error, metadata=result.metadata))
        message = tool_message(call, output, is_error=result.is_error)
        messages.append(message)
        if self.session_store and self.session_id:
            self.session_store.append_message(self.session_id, message)

    async def _execute_call(self, call: ToolCall, emit: Any) -> ToolResult:
        """按 权限 → 前置 Hook → 执行 → 后置 Hook 的顺序处理工具。"""
        try:
            tool = self.registry.get(call.name)
        except Exception as exc:
            return ToolResult(str(exc), is_error=True)

        if self.permission_checker is None:
            emit(event("tool.approved", tool_call_id=call.id,
                       decision="allowed", source="unchecked"))
        else:
            decision = await self.permission_checker.check(
                tool, call.arguments, self.context
            )
            if self.auditor:
                self.auditor.record(
                    "denied" if not decision.allowed else "approved",
                    tool, call.arguments, decision, self.context,
                )
            emit(event("tool.approved", tool_call_id=call.id,
                       decision="allowed" if decision.allowed else "denied"))
            if self.session_store and self.session_id:
                self.session_store.append_event(
                    self.session_id, "permission.decided",
                    tool=tool.name,
                    allowed=decision.allowed,
                    reason=decision.reason,
                    source=decision.source,
                )
            if not decision.allowed:
                return ToolResult(f"权限拒绝：{decision.reason}", is_error=True)
            # 危险/越界/受保护操作放行前先打 git 检查点，供 /undo 回滚。
            if getattr(decision, "checkpoint", False) and self.checkpoint is not None:
                try:
                    await self.checkpoint.capture(tool.name)
                except Exception:
                    pass

        before = None
        if self.hook_engine:
            before = await self.hook_engine.run(
                "tool.before", call.name,
                {"arguments": call.arguments, "tool_call_id": call.id},
            )
            if before and not before.allowed:
                if self.auditor:
                    from .permissions import Decision
                    self.auditor.record(
                        "denied", tool, call.arguments,
                        Decision(False, before.message, source="hook"),
                        self.context,
                    )
                return ToolResult(f"Hook 拒绝：{before.message}", is_error=True)
            if before and before.replace_output is not None:
                return ToolResult(before.replace_output,
                                  {"replaced_by": "tool.before"})

        from .tools import execute_tool as run_tool
        result = await run_tool(self.registry, call, self.context)

        if self.hook_engine:
            after = await self.hook_engine.run(
                "tool.after", call.name,
                {"arguments": call.arguments, "tool_call_id": call.id,
                 "output": result.output},
            )
            if after and after.replace_output is not None:
                result = ToolResult(after.replace_output,
                                    {"replaced_by": "tool.after"})
        return result

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
                length_retried = False
                pending_calls: list[ToolCall] = []
                for iteration in range(1, self.config.max_tool_iterations + 1):
                    if self.pre_request_hook is not None:
                        await self.pre_request_hook(messages)
                    sent_count = len(messages)
                    response = await self._complete_with_retry(messages, emit)
                    if response.usage:
                        total_input += response.usage.input_tokens
                        total_output += response.usage.output_tokens
                        if self.usage_observer is not None:
                            self.usage_observer(
                                response.usage.input_tokens,
                                response.usage.output_tokens,
                                sent_count,
                            )
                        emit(event("usage.recorded",
                                   input_tokens=response.usage.input_tokens,
                                   output_tokens=response.usage.output_tokens))
                    messages.append(response.assistant_message())
                    if self.session_store and self.session_id and response.has_tool_calls:
                        self.session_store.append_message(
                            self.session_id, response.assistant_message()
                        )
                    emit(event("message.created", role="assistant", text=response.text,
                               tool_calls=[
                                   {"id": call.id, "name": call.name, "arguments": call.arguments}
                                   for call in (response.tool_calls or [])
                               ]))
                    if not response.has_tool_calls:
                        # 回复被窗口截断（finish_reason=length）：丢弃残缺回复，
                        # 强制压缩后重新生成一次。截断不报错，必须主动检测。
                        if (response.stop_reason == "length" and not length_retried
                                and self.pre_request_hook is not None):
                            length_retried = True
                            messages.pop()
                            emit(event("context.compacted", reason="truncated"))
                            await self.pre_request_hook(messages, force=True,
                                                        reason="truncated")
                            continue
                        emit(event("run.completed", iterations=iteration,
                                   stop_reason=response.stop_reason,
                                   duration=time.monotonic() - started,
                                   usage={"input_tokens": total_input,
                                          "output_tokens": total_output}))
                        return

                    for call in response.tool_calls or []:
                        emit(event("tool.requested", tool_call_id=call.id,
                                   tool_name=call.name, arguments=call.arguments))
                        pending_calls.append(call)
                        result = await self._execute_call(call, emit)
                        pending_calls.remove(call)
                        self._handle_tool_result(call, result, messages, emit)
                emit(event("run.failed", error="maximum tool iterations reached",
                           error_type="AgentIterationLimit"))
                raise AgentIterationLimit("maximum tool iterations reached")
            except Exception as exc:
                emit(event("run.failed", error=str(exc), error_type=type(exc).__name__))
                raise
            finally:
                # 中断/异常时给每个未完成的 tool_call 补一条 tool_result：
                # 协议要求 assistant(tool_calls) 必须有配对的 tool 消息，
                # 否则下一轮请求（以及会话回放）都会因悬空配对而被拒。
                for call in pending_calls:
                    message = tool_message(
                        call, "（任务被中断，该工具调用未执行完成）", is_error=True
                    )
                    messages.append(message)
                    if self.session_store and self.session_id:
                        self.session_store.append_message(self.session_id, message)
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
