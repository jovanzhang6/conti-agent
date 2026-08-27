from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from .config import HookConfig


@dataclass(frozen=True)
class HookOutcome:
    allowed: bool
    message: str
    replace_output: str | None = None
    source: str = "hook"


class HookEngine:
    """执行声明式 Hook；失败默认拒绝，不会把错误变成放行。"""

    def __init__(self, hooks: list[HookConfig], enabled: bool = True) -> None:
        self.hooks = hooks
        self.enabled = enabled

    def matching(self, event: str, tool_name: str) -> list[HookConfig]:
        return [
            hook for hook in self.hooks
            if hook.event == event and hook.match_tool in {None, tool_name}
        ]

    async def run(self, event: str, tool_name: str, payload: dict[str, Any]) -> HookOutcome | None:
        if not self.enabled:
            return None
        for hook in self.matching(event, tool_name):
            data = json.dumps({
                "event": event,
                "tool": tool_name,
                "payload": payload,
            }, ensure_ascii=False).encode("utf-8")
            try:
                process = await asyncio.create_subprocess_exec(
                    *hook.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(data), hook.timeout_ms / 1000
                )
            except (asyncio.TimeoutError, OSError) as exc:
                if hook.continue_on_error:
                    continue
                return HookOutcome(False, f"Hook 执行失败：{exc}")
            if process.returncode != 0:
                if hook.continue_on_error:
                    continue
                detail = stderr.decode("utf-8", errors="replace").strip()
                return HookOutcome(False, f"Hook 返回失败码 {process.returncode}: {detail}")
            raw = stdout.decode("utf-8", errors="replace").strip()
            try:
                result = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                if hook.continue_on_error:
                    continue
                return HookOutcome(False, "Hook 输出不是有效 JSON")
            if result.get("decision") == "deny":
                return HookOutcome(False, str(result.get("message", "Hook 拒绝该操作")))
            if "replace_output" in result:
                return HookOutcome(True, "Hook 替换了输出",
                                   replace_output=str(result["replace_output"]))
        return HookOutcome(True, "Hook 通过")
