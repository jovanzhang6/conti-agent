"""最小拒绝 Hook：用于验证工具执行前能被策略阻断。"""

from __future__ import annotations

import json


def main() -> None:
    print(json.dumps({
        "decision": "deny",
        "message": "端到端策略拒绝该操作",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
