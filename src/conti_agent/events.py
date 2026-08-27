from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def event(type_name: str, **payload: Any) -> AgentEvent:
    return AgentEvent(type=type_name, payload=payload)


def encode_events(events: Iterator[AgentEvent]) -> Iterator[str]:
    return (item.to_json() for item in events)
