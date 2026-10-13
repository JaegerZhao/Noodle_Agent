from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from noodle_agent.adapters.blob import LocalBlobStore
from noodle_agent.adapters.llm import FixtureKnowledgeAgent
from noodle_agent.db import AtomicNote, BackboneSection, Database, NoteAsset
from noodle_agent.synthesis.blueprints import DEEP_LEARNING
from noodle_agent.synthesis.d2l_importer import D2LSourceImporter, _build_nodes
from noodle_agent.synthesis.semantic_builder import SemanticSynthesisBuilder


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_repository(root: Path) -> None:
    _write(
        root / "index.md",
        """《测试教材》
============

```toc
chapter_preface/index
```

```toc
:numbered:
chapter_introduction/index
```

```toc
chapter_references/zreferences
```
""",
    )
    _write(root / "chapter_preface/index.md", "# 前言\n\n这是前言。\n")
    _write(
        root / "chapter_introduction/index.md",
        """# 深度学习

本章介绍深度学习。

```toc
:maxdepth: 2
basics
```
""",
    )
    _write(
        root / "chapter_introduction/basics.md",
        """# 基本模型

核心公式为

$$y = Wx + b.$$

![示例网络](../img/network.svg)

## 练习

1. 推导参数数量。
""",
    )
    _write(root / "chapter_references/zreferences.md", "# 参考文献\n\n测试引用。\n")
    _write(
        root / "img/network.svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"></svg>',
    )


def test_d2l_source_import_is_complete_and_idempotent(tmp_path: Path) -> None:
    repository = tmp_path / "d2l-zh"
    _fixture_repository(repository)
    nodes = _build_nodes(repository)
    assert [(item.number, item.title) for item in nodes] == [
        ("P", "前言"),
        ("1", "深度学习"),
        ("1.1", "基本模型"),
        ("R", "参考文献"),
    ]

    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    importer = D2LSourceImporter(database, LocalBlobStore(tmp_path / "blobs"))
    first = importer.import_repository(repository)
    second = importer.import_repository(repository)

    assert first.source_version_id == second.source_version_id
    assert first.source_note_version_id == second.source_note_version_id
    assert first.outline_nodes == 4
    assert first.atomic_notes == 4
    assert first.figures == 1
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(AtomicNote)) == 4
        assert session.scalar(select(func.count()).select_from(BackboneSection)) == 4
        assert session.scalar(select(func.count()).select_from(NoteAsset)) == 1
    database.close()


def test_deep_learning_blueprint_reuses_shared_concepts(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    builder = SemanticSynthesisBuilder(
        database, FixtureKnowledgeAgent(), blueprint=DEEP_LEARNING
    )
    spec, prefix = builder._owner("10.7")
    assert prefix == "10.7"
    assert spec["key"] == "transformers"
    assert DEEP_LEARNING.stable_concept_key(spec) == "ml:transformers"
    assert builder._owner("P") is None
    database.close()
