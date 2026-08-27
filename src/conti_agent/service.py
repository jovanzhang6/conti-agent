from __future__ import annotations

from typing import Any

from .errors import ContiAgentError


class ServiceRequestError(ContiAgentError):
    pass


class RuntimeService:
    """校验服务请求并转换为 Runtime 事件输出。"""

    def __init__(self, runtime, *, allow_remote: bool = False) -> None:
        self.runtime = runtime
        self.allow_remote = allow_remote

    def validate_submission(self, payload: dict[str, Any]) -> tuple[str, str | None, str]:
        if not isinstance(payload, dict):
            raise ServiceRequestError("请求体必须是 JSON 对象")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ServiceRequestError("prompt 必须是非空字符串")
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ServiceRequestError("session_id 必须是字符串")
        output_format = payload.get("output_format", "jsonl")
        if output_format not in {"text", "jsonl"}:
            raise ServiceRequestError("output_format 只支持 text 或 jsonl")
        return prompt, session_id, output_format

    async def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt, session_id, output_format = self.validate_submission(payload)
        lines: list[str] = []
        original = self.runtime.output_function
        if output_format == "jsonl":
            async def sink(text: str) -> None:
                lines.append(text)
        else:
            def sink(text: str) -> None:
                lines.append(text)
            original = self.runtime.output_function
            self.runtime.output_function = sink
        try:
            final, session_id, events = await self.runtime.ask(
                prompt, session_id=session_id, output_format=output_format
            )
        finally:
            if output_format == "jsonl":
                self.runtime.output_function = original
        return {
            "result": final,
            "session_id": session_id,
            "events": [item.to_dict() for item in events],
            "event_lines": lines,
        }
