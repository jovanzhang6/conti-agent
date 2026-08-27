from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .errors import ContiAgentError
from .messages import ToolCall


class SessionError(ContiAgentError):
    pass


SESSION_SCHEMA_VERSION = 1


def _encode_message(message: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(message)
    calls = encoded.get("tool_calls")
    if calls:
        encoded["tool_calls"] = [
            call if isinstance(call, dict) else asdict(call)
            for call in calls
        ]
    return encoded


def _decode_message(message: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(message)
    calls = decoded.get("tool_calls")
    if calls:
        decoded["tool_calls"] = [
            ToolCall(
                call["id"],
                call["name"],
                call.get("arguments", {}),
            )
            for call in calls
        ]
    return decoded


class SessionStore:
    """会话账本的追加式存储和恢复实现。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / "sessions"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id or Path(session_id).name != session_id:
            raise SessionError("非法的会话 ID")
        return self.directory / f"{session_id}.jsonl"

    def create(self, workspace: Path, title: str = "") -> tuple[str, list[dict[str, Any]]]:
        session_id = uuid.uuid4().hex
        record = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "kind": "session.started",
            "timestamp": time.time(),
            "session_id": session_id,
            "workspace": str(workspace),
            "title": title,
        }
        self._append(session_id, record)
        return session_id, []

    def _append(self, session_id: str, record: dict[str, Any]) -> None:
        with self._path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        self._append(session_id, {
            "schema_version": SESSION_SCHEMA_VERSION,
            "kind": "message.appended",
            "timestamp": time.time(),
            "message": _encode_message(message),
        })

    def append_compaction(self, session_id: str, summary: str,
                          compacted_count: int) -> None:
        self._append(session_id, {
            "schema_version": SESSION_SCHEMA_VERSION,
            "kind": "history.compacted",
            "timestamp": time.time(),
            "summary": summary,
            "compacted_count": compacted_count,
        })

    def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    first = json.loads(handle.readline())
                result.append({
                    "session_id": first.get("session_id", path.stem),
                    "title": first.get("title", ""),
                    "timestamp": first.get("timestamp", 0),
                    "workspace": first.get("workspace", ""),
                })
            except (OSError, json.JSONDecodeError):
                continue
        return result

    def load(self, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = self._path(session_id)
        if not path.exists():
            raise SessionError(f"会话不存在：{session_id}")
        metadata: dict[str, Any] | None = None
        messages: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("schema_version") != SESSION_SCHEMA_VERSION:
                        raise SessionError(f"不支持的第 {line_number} 行账本版本")
                    if metadata is None and record["kind"] != "session.started":
                        raise SessionError("账本缺少会话起始记录")
                    if record["kind"] == "session.started":
                        metadata = record
                    elif record["kind"] == "message.appended":
                        messages.append(_decode_message(record["message"]))
                    elif record["kind"] == "history.compacted":
                        # 回放时仅保留摘要及其后续内容，压缩掉的旧消息不会恢复。
                        messages = [{
                            "role": "system",
                            "content": "[历史摘要]\n" + record["summary"],
                        }]
                    else:
                        raise SessionError(f"未知账本记录：{record['kind']}")
        except json.JSONDecodeError as exc:
            raise SessionError(f"账本损坏于第 {line_number} 行：{exc}") from exc
        if metadata is None:
            raise SessionError("账本为空或缺少元数据")
        return metadata, messages
