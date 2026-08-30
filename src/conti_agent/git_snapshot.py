from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .errors import ContiAgentError


class CheckpointError(ContiAgentError):
    pass


class GitCheckpoint:
    """危险操作前的 git 检查点（无沙箱形态下的主力防线，HIGHLIGHTS 3.4）。

    capture 通过临时改写索引生成包含未跟踪文件在内的全量树对象：
    不产生提交、不污染分支历史；undo 用 read-tree + checkout-index
    恢复（检查点之后新建的文件不会被删除）。记录存于
    .conti/checkpoints.json，上限 MAX_CHECKPOINTS 条。
    """

    MAX_CHECKPOINTS = 20

    def __init__(self, root: Path, store_path: Path | None = None) -> None:
        self.root = Path(root)
        self.store_path = store_path or self.root / ".conti" / "checkpoints.json"

    async def _git(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments, cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise CheckpointError(stderr.decode("utf-8", errors="replace").strip())
        return stdout.decode("utf-8", errors="replace").strip()

    async def available(self) -> bool:
        try:
            await self._git("rev-parse", "--is-inside-work-tree")
        except (CheckpointError, FileNotFoundError, OSError):
            return False
        return True

    async def capture(self, label: str) -> str | None:
        """记录当前工作区全量状态；非 git 仓库返回 None（静默跳过）。"""
        if not await self.available():
            return None
        try:
            original_index = await self._git("write-tree")
            await self._git("add", "-A")
            tree = await self._git("write-tree")
            await self._git("read-tree", original_index)
        except (CheckpointError, FileNotFoundError, OSError):
            return None
        entry = {
            "id": f"cp-{time.strftime('%Y%m%d-%H%M%S')}-{tree[:8]}",
            "tree": tree,
            "label": label,
            "ts": time.time(),
        }
        checkpoints = self._load()
        checkpoints.append(entry)
        self._save(checkpoints[-self.MAX_CHECKPOINTS:])
        return entry["id"]

    async def undo(self) -> str:
        """回滚到最近一个检查点。"""
        checkpoints = self._load()
        if not checkpoints:
            raise CheckpointError("没有可回滚的检查点")
        entry = checkpoints.pop()
        await self._git("read-tree", entry["tree"])
        await self._git("checkout-index", "-a", "-f")
        self._save(checkpoints)
        return (f"已回滚到检查点 {entry['id']}（{entry['label']}）；"
                f"检查点之后新建的文件不会被删除。")

    def _load(self) -> list[dict]:
        if not self.store_path.exists():
            return []
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return raw if isinstance(raw, list) else []

    def _save(self, items: list[dict]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        temporary.replace(self.store_path)
