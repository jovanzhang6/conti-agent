"""最小 Hook 示例：默认允许，但可读取 stdin 并返回 deny。"""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"decision": "deny", "message": "invalid hook input"}))
        return
    # 在这里检查 payload["tool"] 和 payload["payload"]["arguments"]。
    print(json.dumps({"decision": "allow", "message": "accepted"}))


if __name__ == "__main__":
    main()
