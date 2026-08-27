from __future__ import annotations

import asyncio
from pathlib import Path

from .errors import ContiAgentError


class SnapshotError(ContiAgentError):
    pass


class SnapshotManager:
    """基于 Git worktree 的显式工作区快照管理。"""

    def __init__(self, repository_root: Path, parent_dir: Path | None = None) -> None:
        self.repository_root = Path(repository_root)
        self.parent_dir = Path(parent_dir) if parent_dir else self.repository_root / ".conti" / "workspace"

    async def _git(self, *arguments: str, cwd: Path | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *arguments,
            cwd=str(cwd or self.repository_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise SnapshotError(stderr.decode("utf-8", errors="replace").strip())
        return stdout.decode("utf-8", errors="replace").strip()

    async def require_repository(self) -> None:
        try:
            await self._git("rev-parse", "--is-inside-work-tree")
        except (SnapshotError, FileNotFoundError) as exc:
            raise SnapshotError(f"当前目录不是可用 Git 仓库：{exc}") from exc

    async def create(self, slug: str, base: str = "HEAD") -> Path:
        await self.require_repository()
        self.parent_dir.mkdir(parents=True, exist_ok=True)
        path = self.parent_dir / slug
        if path.exists():
            raise SnapshotError(f"快照路径已存在：{path}")
        await self._git("worktree", "add", str(path), "-b", f"conti/{slug}", base)
        return path

    async def status(self, path: Path) -> list[str]:
        output = await self._git("status", "--porcelain", cwd=path)
        return output.splitlines() if output else []

    async def cleanup(self, path: Path) -> None:
        await self.require_repository()
        await self._git("worktree", "remove", str(path), "--force")
