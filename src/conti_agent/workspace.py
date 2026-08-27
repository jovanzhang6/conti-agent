from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import ToolValidationError


DEFAULT_IGNORES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
}


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


class Workspace:
    """受限制的本地文件系统视图。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        if not self.root.exists():
            raise ToolValidationError(f"workspace does not exist: {self.root}")
        if not self.root.is_dir():
            raise ToolValidationError(f"workspace is not a directory: {self.root}")

    def resolve(self, value: str | Path = ".") -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        # 词法归一化后再检查真实路径，父目录跳转和符号链接逃逸都会被拦截。
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ToolValidationError("path escapes the workspace boundary")
        return resolved

    def relative_display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def read_text(self, value: str | Path, *, max_bytes: int = 256_000) -> tuple[str, int]:
        path = self.resolve(value)
        if not path.exists():
            raise ToolValidationError(f"file does not exist: {self.relative_display(path)}")
        if not path.is_file():
            raise ToolValidationError(f"path is not a file: {self.relative_display(path)}")
        size = path.stat().st_size
        if size > max_bytes:
            raise ToolValidationError(
                f"file is too large: {size} bytes exceeds {max_bytes}"
            )
        try:
            # newline="" 保留源文件中的 CRLF/LF，编辑时不会意外改写换行格式。
            with path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read(), size
        except UnicodeDecodeError as exc:
            raise ToolValidationError(f"file is not UTF-8 text: {exc}") from exc

    def write_text(self, value: str | Path, content: str, *,
                   max_bytes: int = 1_000_000) -> int:
        path = self.resolve(value)
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            raise ToolValidationError(f"write exceeds {max_bytes} byte limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.exists() else b""
        path.write_bytes(encoded)
        return len(encoded) - len(previous)

    def edit_text(self, value: str | Path, old: str, new: str, *,
                  expected_count: int | None = None) -> dict[str, Any]:
        content, _ = self.read_text(value)
        count = content.count(old)
        expected = expected_count if expected_count is not None else 1
        if count != expected:
            raise ToolValidationError(
                f"expected {expected} match(es), found {count}; no edit was applied"
            )
        self.write_text(value, content.replace(old, new))
        return {"matches": count, "bytes_added": len(new.encode("utf-8"))}

    def list_paths(self, value: str | Path = ".", *, max_depth: int = 4,
                   include_hidden: bool = False) -> list[Path]:
        base = self.resolve(value)
        if not base.exists():
            raise ToolValidationError(f"path does not exist: {self.relative_display(base)}")
        results: list[Path] = []
        base_depth = len(base.parts)

        def visit(directory: Path) -> None:
            if len(results) >= 1000:
                return
            for child in sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.name in DEFAULT_IGNORES:
                    continue
                if len(child.parts) - base_depth > max_depth:
                    continue
                results.append(child)
                if child.is_dir():
                    visit(child)

        if base.is_file():
            return [base]
        visit(base)
        return results
