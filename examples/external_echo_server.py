"""最小外部工具服务：用于端到端验证 JSON-RPC 工具协议。"""

from __future__ import annotations

import json
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            result = {"protocol": "conti-external-tools/1", "server": "echo"}
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "echo",
                    "description": "原样返回输入文本。",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "effects": ["read"],
                }]
            }
        elif method == "tools/call":
            arguments = request.get("params", {}).get("arguments", {})
            result = {"content": f"external-echo:{arguments.get('text', '')}"}
        else:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "method not found"}})
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
