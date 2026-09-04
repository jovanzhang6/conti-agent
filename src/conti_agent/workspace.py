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
        """读取文本文件；超过 max_bytes 时返回前 max_bytes 字节（截断不报错）。

        截断按字节切，边界处的多字节字符以 replace 容错解码；
        未截断的文件仍严格校验 UTF-8。
        """
        path = self.resolve(value)
        if not path.exists():
            raise ToolValidationError(f"file does not exist: {self.relative_display(path)}")
        if not path.is_file():
            hint = ("path is a directory（用 workspace_list 列目录）"
                    if path.is_dir() else "path is not a file")
            raise ToolValidationError(f"{hint}: {self.relative_display(path)}")
        size = path.stat().st_size
        truncated = size > max_bytes
        # 字节级读取对齐 max_bytes 语义；字节流不经过换行转义，保留源格式。
        with path.open("rb") as handle:
            data = handle.read(max_bytes) if truncated else handle.read()
        if truncated:
            return data.decode("utf-8", errors="replace"), size
        try:
            return data.decode("utf-8"), size
        except UnicodeDecodeError as exc:
            raise ToolValidationError(f"file is not UTF-8 text: {exc}") from exc

    def read_lines(self, value: str | Path, *, offset: int = 1,
                   limit: int = 600, max_bytes: int = 256_000) -> tuple[str, dict[str, Any]]:
        """按行分页读取文本文件（行号从 1 起）。

        默认 limit=600：600 行 × ~80 字节/行 ≈ 48KB，落在单结果上下文
        预算（窗口 × 5%）内，默认页不触发落盘替换。

        返回 (文本块, 元数据)。文本块超出 max_bytes 时自动收缩；
        未读到文件末尾时元数据带 next_offset 供续读。
        部分返回时每行带 6 位行号前缀（仅供定位，编辑匹配时不要包含）。
        """
        path = self.resolve(value)
        if not path.exists():
            raise ToolValidationError(f"file does not exist: {self.relative_display(path)}")
        if not path.is_file():
            hint = ("path is a directory（用 workspace_list 列目录）"
                    if path.is_dir() else "path is not a file")
            raise ToolValidationError(f"{hint}: {self.relative_display(path)}")
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolValidationError(f"file is not UTF-8 text: {exc}") from exc
        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(offset))
        end = min(total, start + max(1, int(limit)) - 1)
        # 需要分页的判定：显式翻页、行数超限或字节数超限。
        needs_paging = start > 1 or end < total or size > max_bytes
        if not needs_paging:
            # 整读返回原始文本（保留换行等精确字节），供编辑工具精确匹配。
            meta: dict[str, Any] = {
                "path": self.relative_display(path),
                "size": size,
                "total_lines": total,
                "start": 1 if total else 0,
                "end": total,
                "next_offset": None,
            }
            return text, meta
        used = 0
        out: list[str] = []
        next_offset: int | None = None
        for lineno in range(start, end + 1):
            line = lines[lineno - 1]
            rendered = f"{lineno:6d}\t{line}"
            size_with_nl = len(rendered.encode("utf-8")) + 1
            if used + size_with_nl > max_bytes:
                if lineno == start and not out:
                    # 首行自身超限：硬截该行保证至少返回一行；
                    # 本行已返回（截断版），下一页从下一行开始，
                    # 否则 next_offset 指回本行会死循环。
                    rendered = rendered.encode("utf-8")[:max_bytes].decode(
                        "utf-8", errors="replace")
                    out.append(rendered)
                    next_offset = lineno + 1
                else:
                    next_offset = lineno
                break
            out.append(rendered)
            used += size_with_nl
        # 字节帽没触发、但请求窗口之外还有行：同样给出续读 offset。
        if next_offset is None and end < total:
            next_offset = end + 1
        meta: dict[str, Any] = {
            "path": self.relative_display(path),
            "size": size,
            "total_lines": total,
            "start": start if out else 0,
            "end": (start + len(out) - 1) if out else 0,
            "next_offset": next_offset,
        }
        block = "\n".join(out)
        if next_offset is not None:
            block += (f"\n\n[分页] 共 {total} 行，本次返回 {meta['start']}–{meta['end']} 行。"
                      f"继续读取：workspace_read(path, offset={next_offset}, "
                      f"limit={max(1, int(limit))})。行号仅供定位，workspace_edit "
                      "匹配时不要包含行号。")
        return block, meta

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
