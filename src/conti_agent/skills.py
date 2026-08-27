from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

from .errors import ToolValidationError


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    keywords: list[str]
    version: int
    body: str

    @property
    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "version": self.version,
        }


class SkillLibrary:
    """发现、校验和显式加载 Markdown Skill 包。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def discover(self) -> list[Skill]:
        if not self.directory.exists():
            return []
        return [self._parse(path) for path in sorted(self.directory.glob("*.md"))]

    def find(self, name: str) -> Skill:
        for skill in self.discover():
            if skill.name == name:
                return skill
        raise ToolValidationError(f"未找到 Skill：{name}")

    def _parse(self, path: Path) -> Skill:
        # 允许文件开头有 UTF-8 BOM 或一个空行，便于手写 Skill 文件。
        text = path.read_text(encoding="utf-8-sig").lstrip()
        if not text.startswith("---"):
            raise ToolValidationError(f"Skill 缺少 front matter：{path.name}")
        parts = text.split("---", 2)
        if len(parts) != 3:
            raise ToolValidationError(f"Skill front matter 不完整：{path.name}")
        try:
            meta = tomllib.loads(parts[1])
        except tomllib.TOMLDecodeError as exc:
            raise ToolValidationError(f"Skill 元数据解析失败：{path.name}: {exc}") from exc
        name = meta.get("name")
        description = meta.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ToolValidationError(f"Skill 必须有 name 和 description：{path.name}")
        keywords = meta.get("keywords", [])
        if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise ToolValidationError(f"Skill keywords 必须是字符串数组：{path.name}")
        version = int(meta.get("version", 1))
        return Skill(name, description, keywords, version, parts[2].strip())
