from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


class SkillLoader:
    """Discover Skill metadata first and load full instructions only when selected."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _frontmatter(text: str) -> dict[str, str]:
        if not text.startswith("---"):
            return {}
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}
        values: dict[str, str] = {}
        for line in parts[1].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def discover(self) -> list[SkillInfo]:
        if not self.root.exists():
            return []
        skills = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            metadata = self._frontmatter(path.read_text(encoding="utf-8"))
            name = metadata.get("name") or path.parent.name
            description = metadata.get("description") or ""
            skills.append(SkillInfo(name=name, description=description, path=path))
        return skills

    def select(self, user_message: str) -> SkillInfo | None:
        message = user_message.lower()
        skills = self.discover()
        if not skills:
            return None
        preferred = (
            "note-editing"
            if any(
                token in message
                for token in ("修改", "替换", "编辑", "改写", "删除图片", "换成")
            )
            else (
                "pdf-to-atomic-notes"
                if any(token in message for token in ("pdf", "上传", "解析", "生成笔记"))
                else "evidence-grounded-qa"
            )
        )
        return next((skill for skill in skills if skill.name == preferred), skills[0])

    @staticmethod
    def load(skill: SkillInfo, *, max_chars: int = 12_000) -> str:
        text = skill.path.read_text(encoding="utf-8")
        text = re.sub(r"\A---.*?---\s*", "", text, flags=re.DOTALL)
        return text[:max_chars]
