from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from noodle_agent.adapters.llm import HashEmbedding
from noodle_agent.db import (
    Concept,
    ConceptAlias,
    ConceptModule,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptVersion,
    Database,
    Synthesis,
    SynthesisNode,
)
from noodle_agent.domain import SearchHit
from noodle_agent.services import NoodleService
from noodle_agent.synthesis.curation import KnowledgeCurationService


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seed_concept(database: Database, stable_key: str, title: str, body: str) -> tuple[str, str]:
    with database.session() as session:
        concept = Concept(
            stable_key=stable_key,
            domain_key="test",
            canonical_name=title,
            original_name=title,
            status="PUBLISHED",
        )
        session.add(concept)
        session.flush()
        module = ConceptModule(
            concept_id=concept.id,
            module_key="overview",
            semantic_role="OVERVIEW",
        )
        session.add(module)
        session.flush()
        payload = {"blocks": [{"block_key": "overview-1", "type": "PROSE", "markdown": body}]}
        module_version = ConceptModuleVersion(
            module_id=module.id,
            version_no=1,
            render_type="PROSE",
            title="概述",
            payload_json=json.dumps(payload, ensure_ascii=False),
            plain_text=body,
            origin="TEST",
            content_sha256=_sha(body),
            created_by="test",
        )
        session.add(module_version)
        session.flush()
        version = ConceptVersion(
            concept_id=concept.id,
            version_no=1,
            title=title,
            original_title=title,
            scope=json.dumps({"aliases": []}, ensure_ascii=False),
            summary=body,
            content_sha256=_sha(title + body),
            schema_version="concept-v3",
            prompt_version="test-v1",
            status="PUBLISHED",
        )
        session.add(version)
        session.flush()
        session.add(
            ConceptReleaseModule(
                concept_version_id=version.id,
                module_id=module.id,
                module_version_id=module_version.id,
                sort_order=0,
            )
        )
        concept.active_version_id = version.id
        return concept.id, version.id


def test_curated_synthesis_registry_search_and_merge(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'curation.db'}")
    database.init()
    service = KnowledgeCurationService(database, HashEmbedding())
    target_id, target_version_id = _seed_concept(
        database,
        "ml:convolutional-neural-networks",
        "卷积神经网络",
        "卷积核通过局部连接和权重共享提取空间特征。",
    )
    source_id, source_version_id = _seed_concept(
        database,
        "dl:cnn",
        "CNN",
        "CNN 对图像具有平移等变性，并可堆叠形成层级表示。",
    )

    first = service.create_synthesis(
        stable_key="large-language-models",
        domain_key="llm",
        title="大语言模型知识体系",
        description="由共享 Concept 组成的可编辑知识体系。",
        outline=[
            {
                "stable_key": "foundations",
                "title": "基础",
                "node_kind": "GROUP",
                "children": [
                    {"stable_key": "cnn", "title": "卷积神经网络", "node_kind": "CONCEPT", "concept_id": target_id},
                    {"stable_key": "cnn-alias", "title": "CNN 补充", "node_kind": "CONCEPT", "concept_id": source_id},
                ],
            }
        ],
        actor="test-user",
    )
    assert first["status"] == "PUBLISHED"
    assert first["coverage"]["concept_nodes"] == 2

    results = service.search_concepts("卷积神经网络", limit=5)
    assert results
    assert results[0].entity_type == "CONCEPT"
    assert results[0].concept_id == target_id
    assert results[0].synthesis_paths

    merged = service.merge_concepts(
        source_concept_id=source_id,
        target_concept_id=target_id,
        expected_source_version_id=source_version_id,
        expected_target_version_id=target_version_id,
        rationale="两个标识描述同一个知识点",
        actor="test-user",
    )
    assert merged["status"] == "MERGED"
    assert merged["target_version_id"] != target_version_id
    assert merged["refreshed_synthesis_versions"]

    with database.session() as session:
        source = session.get(Concept, source_id)
        target = session.get(Concept, target_id)
        synthesis = session.scalar(select(Synthesis).where(Synthesis.stable_key == "large-language-models"))
        nodes = session.scalars(
            select(SynthesisNode).where(SynthesisNode.synthesis_version_id == synthesis.active_version_id)
        ).all()
        aliases = session.scalars(select(ConceptAlias).where(ConceptAlias.concept_id == target_id)).all()
        modules = session.scalars(select(ConceptReleaseModule).where(ConceptReleaseModule.concept_version_id == target.active_version_id)).all()
        assert source.visibility == "MERGED"
        assert {node.concept_id for node in nodes if node.concept_id} == {target_id}
        assert sum(node.concept_id == target_id for node in nodes) == 1
        assert len(modules) == 1
        merged_module = session.get(ConceptModuleVersion, modules[0].module_version_id)
        assert "局部连接" in merged_module.plain_text
        assert "平移等变性" in merged_module.plain_text
        assert merged["combined_modules"] == 1
        assert any(alias.alias == "CNN" for alias in aliases)

    alias_results = service.list_concepts("CNN")
    assert alias_results[0]["concept_id"] == target_id
    with pytest.raises(ValueError, match="version conflict"):
        service.publish_synthesis(
            synthesis_id=first["synthesis_id"],
            expected_version_id=first["version_id"],
            title="过期发布",
            description="CAS 应拒绝",
            outline=[{"title": "卷积神经网络", "node_kind": "CONCEPT", "concept_id": target_id}],
            actor="test-user",
        )
    database.close()


def test_unified_rag_preserves_concept_and_atomic_note_layers() -> None:
    service = object.__new__(NoodleService)
    service.search = lambda *_args, **_kwargs: [
        SearchHit(note_id="note-1", note_version_id="note-v1", title="来源笔记", thesis="页码证据", body="source", score=0.45),
        SearchHit(note_id="note-2", note_version_id="note-v2", title="另一来源", thesis="补充", body="source", score=0.40),
    ]
    service.knowledge_curation = SimpleNamespace(search_concepts=lambda *_args, **_kwargs: [
        SearchHit(note_id="concept:c-1", note_version_id="cv-1", title="共享概念", thesis="规范解释", body="concept", score=0.90, entity_type="CONCEPT", concept_id="c-1", synthesis_paths=["体系 / 共享概念"]),
        SearchHit(note_id="concept:c-2", note_version_id="cv-2", title="相关概念", thesis="关系", body="concept", score=0.80, entity_type="CONCEPT", concept_id="c-2"),
    ])

    hits = NoodleService.search_knowledge(service, "查询", limit=4)
    assert [hit.entity_type for hit in hits] == ["CONCEPT", "ATOMIC_NOTE", "CONCEPT", "ATOMIC_NOTE"]
    assert hits[0].synthesis_paths == ["体系 / 共享概念"]


def test_merge_preview_is_read_only_and_exact_merge_preserves_old_release(tmp_path):
    from noodle_agent.db import ConceptContribution
    from noodle_agent.synthesis.service import SynthesisService
    db = Database(f"sqlite:///{tmp_path / 'merge.db'}")
    db.init()
    target, tv = _seed_concept(db, "test:target", "CNN", "Same fact")
    source, sv = _seed_concept(db, "test:source", "CNN", "Same fact")
    with db.session() as session:
        for cid, vid, ref in [(target, tv, "target-source"), (source, sv, "source-source")]:
            placement = session.scalar(select(ConceptReleaseModule).where(ConceptReleaseModule.concept_version_id == vid))
            contribution = ConceptContribution(module_id=placement.module_id, source_ref=ref, source_role="TEST", contribution_role="SUPPORT", decision_status="ACCEPTED", rationale="test", assigned_by="test")
            session.add(contribution); session.flush()
            mv = session.get(ConceptModuleVersion, placement.module_version_id)
            payload = json.loads(mv.payload_json)
            payload["blocks"][0]["evidence_refs"] = [contribution.id]
            payload["_contributions"] = [{"contribution_id": contribution.id, "role": "SUPPORT"}]
            mv.payload_json = json.dumps(payload)
    service = KnowledgeCurationService(db, HashEmbedding())
    reader = SynthesisService(db, None)
    old = reader.get_concept(target, version_id=tv)
    preview = service.preview_merge(source_concept_id=source, target_concept_id=target, expected_source_version_id=sv, expected_target_version_id=tv)
    assert preview["modules"][0]["action"] == "COMBINE"
    assert preview["modules"][0]["identical_blocks"] == 1
    with db.session() as session:
        assert session.get(Concept, target).active_version_id == tv
    merged = service.merge_concepts(source_concept_id=source, target_concept_id=target, expected_source_version_id=sv, expected_target_version_id=tv, rationale="Same meaning verified", actor="test")
    assert merged["deduplicated_blocks"] == 1
    current = reader.get_concept(target)
    assert len(current["blocks"]) == 1
    block = current["blocks"][0]["content_blocks"][0]
    assert len(block["evidence_refs"]) == 2
    assert reader.get_concept(target, version_id=tv) == old
    with pytest.raises(ValueError):
        service.preview_merge(source_concept_id=source, target_concept_id=target, expected_source_version_id=sv, expected_target_version_id=tv)
    db.close()


def test_index_network_call_never_holds_writer_transaction(tmp_path):
    from sqlalchemy import text
    db = Database(f"sqlite:///{tmp_path / 'index.db'}")
    db.init()
    _seed_concept(db, "test:cnn", "CNN", "Convolution")
    class Embedding:
        model_name = "test-index"
        calls = 0
        def embed(self, texts):
            self.calls += 1
            # A second connection must be able to write during provider work.
            with db.session() as other:
                other.execute(text("UPDATE concepts SET status = status"))
            return [[1.0, 0.0] for _ in texts]
    embedding = Embedding()
    service = KnowledgeCurationService(db, embedding)
    assert service.ensure_concept_index() == 1
    assert embedding.calls == 0
    assert service.ensure_concept_index(force=True) == 1
    assert embedding.calls == 1
    db.close()


def test_source_scoped_rag_does_not_search_unrelated_concepts():
    service = object.__new__(NoodleService)
    def search(_query, **kwargs):
        assert kwargs["source_version_id"] == "selected-source"
        return [SearchHit(note_id="a", note_version_id="v", title="Selected", thesis="", body="", score=0.6)]
    service.search = search
    def concept_search(*args, **kwargs):
        raise AssertionError("Source-scoped questions must not include global Concepts")
    service.knowledge_curation = SimpleNamespace(search_concepts=concept_search)
    hits = service.search_knowledge("Question", source_version_id="selected-source")
    assert [hit.note_id for hit in hits] == ["a"]
