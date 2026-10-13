from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest
from sqlalchemy import select

from noodle_agent.adapters.blob import LocalBlobStore
from noodle_agent.adapters.llm import FixtureKnowledgeAgent
from noodle_agent.db import (
    AtomicNote,
    BackboneSection,
    Concept,
    ConceptContentEvidence,
    ConceptContribution,
    ConceptModule,
    ConceptModuleJudgeRecord,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptUpdateJob,
    Database,
    NoteVersion,
)
from noodle_agent.synthesis.blueprints import DEEP_LEARNING, MACHINE_LEARNING
from noodle_agent.synthesis.domain import BackboneManifest
from noodle_agent.synthesis.migration import BackboneSourceMigrator
from noodle_agent.synthesis.semantic_builder import SemanticSynthesisBuilder
from noodle_agent.synthesis.service import SynthesisService
from noodle_agent.worker import JobWorker


class _SemanticFixture(FixtureKnowledgeAgent):
    def structured_json(self, **kwargs):
        if kwargs["operation"] == "semantic_synthesis.judge_concept_module_v1":
            return {
                "decision": "PASS",
                "summary": "候选内容均可由所列证据支持。",
                "issues": [],
                "coverage": {"evidence_checked": True},
            }
        if kwargs["operation"] != "semantic_synthesis.write_concept_v3":
            return super().structured_json(**kwargs)
        user = kwargs["user"]
        return {
            "title_zh": user["concept"]["title"],
            "summary": "综合全部已接纳来源。",
            "scope": {"includes": ["测试"], "excludes": []},
            "modules": [
                {
                    "block_key": module["block_key"],
                    "title": module["title"],
                    "semantic_role": module["semantic_role"],
                    "payload": {
                        "blocks": [
                            {
                                "block_key": f"{module['block_key']}-content-1",
                                "type": "PROSE",
                                "markdown": "\n\n".join(
                                    source["text"] for source in module["sources"]
                                ),
                                "evidence_refs": [
                                    source["contribution_id"] for source in module["sources"]
                                ],
                                "note_block_ids": [
                                    block["note_block_id"]
                                    for source in module["sources"]
                                    for block in source.get("blocks", [])
                                ],
                                "evidence_span_ids": [
                                    evidence["evidence_span_id"]
                                    for source in module["sources"]
                                    for block in source.get("blocks", [])
                                    for evidence in block.get("evidence", [])
                                ],
                            }
                        ]
                    },
                }
                for module in user["required_modules"]
            ],
        }


class _FallbackSemanticFixture(_SemanticFixture):
    def structured_json(self, **kwargs):
        if kwargs["operation"] == "semantic_synthesis.write_concept_v3":
            return {
                "title_zh": kwargs["user"]["concept"]["title"],
                "summary": "无有效模块。",
                "modules": [],
            }
        return super().structured_json(**kwargs)


class _RepairSemanticFixture(_SemanticFixture):
    def __init__(self):
        self.judge_calls = 0

    def structured_json(self, **kwargs):
        if kwargs["operation"] == "semantic_synthesis.judge_concept_module_v1":
            self.judge_calls += 1
            if self.judge_calls == 1:
                block = kwargs["user"]["module"]["payload"]["blocks"][0]
                return {
                    "decision": "REPAIR",
                    "summary": "首个内容块略显重复，可局部压缩。",
                    "issues": [
                        {
                            "issue_id": "readability-1",
                            "severity": "MAJOR",
                            "category": "READABILITY",
                            "block_key": block["block_key"],
                            "claim": "",
                            "message": "删除重复表达。",
                            "evidence_refs": block["evidence_refs"],
                            "suggested_action": "压缩为一段。",
                        }
                    ],
                    "coverage": {"evidence_checked": True},
                }
        if kwargs["operation"] == "semantic_synthesis.repair_concept_module_v1":
            return {"module": kwargs["user"]["module"]}
        return super().structured_json(**kwargs)


class _ConceptWorkerService:
    def __init__(self, database, builder):
        self.db = database
        self.builder = builder
        self.settings = SimpleNamespace(
            worker_lease_seconds=60,
            worker_heartbeat_seconds=5,
            worker_poll_seconds=0.2,
        )

    def run_concept_update_job(self, job_id):
        return self.builder.run_concept_update_job(job_id)

    def get_concept_update_job(self, job_id):
        return self.builder.get_concept_update_job(job_id)

    def run_job_once(self, _job_id):
        raise AssertionError("No PDF Job is expected in this test")


def _textbook(path: Path) -> None:
    document = fitz.open()
    for title, body in [
        ("Machine learning", "Machine learning estimates useful patterns from data."),
        ("Linear models", "Linear regression predicts a continuous target from weighted features."),
        ("Back matter", "Index"),
    ]:
        page = document.new_page()
        page.insert_text((72, 72), title, fontsize=16)
        page.insert_text((72, 110), body, fontsize=11)
        if title == "Machine learning":
            page.draw_rect(fitz.Rect(90, 145, 260, 205), color=(0.1, 0.3, 0.8))
            page.insert_text(
                (72, 225),
                "Figure 1.1: A deterministic vector figure for extraction.",
                fontsize=10,
            )
    document.set_toc(
        [
            [1, "1 Introduction", 1],
            [2, "1.1 What is machine learning?", 1],
            [1, "II Linear Models", 2],
            [2, "2 Linear Regression", 2],
            [3, "2.1 Definition", 2],
            [2, "Index", 3],
        ]
    )
    document.save(path)
    document.close()


def test_complete_backbone_seed_and_fixture_enrichment(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    _textbook(pdf)
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    service = SynthesisService(database, FixtureKnowledgeAgent())
    manifest = BackboneManifest(
        stable_key="test-book-v1",
        domain_key="machine-learning-test",
        title="Test Machine Learning",
        authors=["Test Author"],
        edition_label="v1",
        source_url="https://example.test/book",
        license_note="test fixture",
    )

    report = service.register_backbone(pdf, manifest=manifest)
    assert report.toc_nodes == 6
    assert report.concept_nodes == 4
    assert report.seeded_concepts == 4
    assert report.coverage == 1

    synthesis = service.get_synthesis(report.synthesis_id)
    assert len(synthesis["outline"]) == 2
    concepts = []
    queue = list(synthesis["outline"])
    while queue:
        node = queue.pop(0)
        if node["concept_id"]:
            concepts.append(node["concept_id"])
        queue.extend(node["children"])
    assert len(concepts) == 4

    seeded = service.get_concept(concepts[0])
    assert seeded["status"] == "SEEDED"
    assert seeded["blocks"][0]["evidence"][0]["source_locator"]["pages"]

    figures = service.extract_figures(report.corpus_id)
    assert figures.discovered == 1
    assert figures.extracted == 1
    translated = service.translate_figure_captions(report.corpus_id, batch_size=1)
    assert translated.translated == 1
    assert translated.failed == []
    repeated_translation = service.translate_figure_captions(report.corpus_id, batch_size=1)
    assert repeated_translation.translated == 0
    assert repeated_translation.skipped_existing == 1
    concepts_with_figures = [
        service.get_concept(concept_id)
        for concept_id in concepts
        if service.get_concept(concept_id)["figures"]
    ]
    assert len(concepts_with_figures) == 1
    figure = concepts_with_figures[0]["figures"][0]
    assert figure["figure_label"] == "1.1"
    assert figure["caption"].startswith("中文图注：")
    assert figure["caption_original"].startswith("Figure 1.1:")
    assert service.get_figure_path(figure["figure_id"]).is_file()

    enriched = service.enrich_concept(concepts[0])
    assert enriched["status"] == "PUBLISHED"
    published = service.get_concept(concepts[0])
    assert published["version_no"] == 2
    assert published["status"] == "PUBLISHED"
    database.close()


def test_backbone_source_migration_is_complete_and_idempotent(tmp_path: Path) -> None:
    pdf = tmp_path / "book.pdf"
    _textbook(pdf)
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    service = SynthesisService(database, FixtureKnowledgeAgent())
    manifest = BackboneManifest(
        stable_key="migration-book-v1",
        domain_key="machine-learning-migration-test",
        title="Migration Test Machine Learning",
        authors=["Test Author"],
        edition_label="v1",
        source_url="https://example.test/migration-book",
        license_note="test fixture",
    )
    report = service.register_backbone(pdf, manifest=manifest)
    service.extract_figures(report.corpus_id)
    migrator = BackboneSourceMigrator(database, LocalBlobStore(tmp_path / "blobs"))

    first = migrator.migrate(report.corpus_id)
    second = migrator.migrate(report.corpus_id)

    assert first.source_version_id == second.source_version_id
    assert first.source_note_version_id == second.source_note_version_id
    assert first.outline_nodes == 6
    assert first.atomic_notes == 4
    assert first.note_blocks == 4
    assert first.figures == 1
    database.close()


def test_semantic_normalizer_keeps_owned_module_boundary(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    builder = SemanticSynthesisBuilder(database, FixtureKnowledgeAgent())
    spec = {
        "key": "test-concept",
        "title": "测试概念",
        "parent_key": "overview",
        "kind": "ATOMIC",
        "source_prefixes": ["1"],
        "summary_hint": "测试摘要",
    }
    modules = [
        {
            "block_key": "overview",
            "title": "概览",
            "semantic_role": "OVERVIEW",
            "sources": [],
        }
    ]
    payload = {
        "title_zh": "测试概念",
        "summary": "保持构建器定义的模块边界。",
        "scope": {"includes": ["测试"], "excludes": []},
        "modules": [
            {
                "block_key": "overview",
                "title": "概览",
                "render_type": "PROSE",
                "semantic_role": "EXTENSION",
                "payload": {"markdown": "有效正文"},
            },
            {
                "block_key": "provider-invented-subsection",
                "title": "模型擅自增加的细分",
                "render_type": "PROSE",
                "semantic_role": "EXTENSION",
                "payload": {"markdown": "不应进入 Concept。"},
            },
        ],
    }

    draft, used_fallback, detail = builder._normalize_draft(payload, spec, modules)

    assert used_fallback is False
    assert detail == ""
    assert [item.block_key for item in draft.modules] == ["overview"]
    assert draft.modules[0].semantic_role == "OVERVIEW"
    exercise_owner, _prefix = builder._owner("2.9")
    assert exercise_owner["key"] == "random-variables"
    database.close()


def test_shared_concept_uses_global_canonical_title(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    dl_spec = next(
        item
        for item in DEEP_LEARNING.concepts
        if DEEP_LEARNING.stable_concept_key(item) == "ml:random-variables"
    )
    ml_spec = next(
        item
        for item in MACHINE_LEARNING.concepts
        if MACHINE_LEARNING.stable_concept_key(item) == "ml:random-variables"
    )
    builder = SemanticSynthesisBuilder(database, FixtureKnowledgeAgent(), blueprint=DEEP_LEARNING)
    assert builder._global_spec(dl_spec)["title"] == ml_spec["title"]
    database.close()


def test_atomic_note_requires_explicit_acceptance_and_publishes_new_releases(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "book.pdf"
    _textbook(pdf)
    database = Database(f"sqlite:///{tmp_path / 'noodle.db'}")
    database.init()
    service = SynthesisService(database, _SemanticFixture())
    manifest = BackboneManifest(
        stable_key="contribution-book-v1",
        domain_key="machine-learning-contribution-test",
        title="Contribution Test",
        authors=["Test Author"],
        edition_label="v1",
        source_url="https://example.test/contribution-book",
        license_note="test fixture",
    )
    report = service.register_backbone(pdf, manifest=manifest)
    migrated = BackboneSourceMigrator(database, LocalBlobStore(tmp_path / "blobs")).migrate(
        report.corpus_id
    )

    with database.session() as session:
        notes = session.scalars(select(AtomicNote).order_by(AtomicNote.stable_key)).all()
        assert len(notes) >= 2
        versions = [session.get(NoteVersion, note.active_version_id) for note in notes[:2]]
        section = session.scalar(
            select(BackboneSection)
            .where(
                BackboneSection.corpus_id == report.corpus_id,
                BackboneSection.concept_id.is_not(None),
            )
            .order_by(BackboneSection.sort_order)
        )
        concept = session.get(Concept, section.concept_id)
        initial_concept_version_id = concept.active_version_id
        module = ConceptModule(
            concept_id=concept.id,
            module_key="overview",
            semantic_role="OVERVIEW",
        )
        session.add(module)
        session.flush()
        examples_module = ConceptModule(
            concept_id=concept.id,
            module_key="examples",
            semantic_role="EXAMPLE",
        )
        session.add(examples_module)
        session.flush()
        session.add(
            ConceptContribution(
                module_id=module.id,
                atomic_note_version_id=versions[0].id,
                source_role="ATOMIC_NOTE",
                source_ref=f"Backbone:test:{versions[0].id}",
                contribution_role="FOUNDATION",
                decision_status="ACCEPTED",
                rationale="受信任骨架来源",
                assigned_by="test",
            )
        )
        session.add(
            ConceptContribution(
                module_id=examples_module.id,
                atomic_note_version_id=versions[0].id,
                source_role="ATOMIC_NOTE",
                source_ref=f"Backbone:test:{versions[0].id}",
                contribution_role="FOUNDATION",
                decision_status="ACCEPTED",
                rationale="受信任骨架来源",
                assigned_by="test",
            )
        )
        concept_id = concept.id
        stable_key = concept.stable_key
        overview_module_id = module.id
        examples_module_id = examples_module.id
        second_note_version_id = versions[1].id
        original_synthesis_version_id = service.get_synthesis(report.synthesis_id)["version_id"]

    builder = SemanticSynthesisBuilder(database, _RepairSemanticFixture())
    spec = {
        "key": stable_key,
        "stable_key": stable_key,
        "title": "测试概念",
        "parent_key": "",
        "kind": "COMPOSITE",
        "source_prefixes": [],
        "summary_hint": "测试",
    }
    first = builder._compile_one(builder._prepared_from_contributions(spec))
    assert first["concept_version_id"] != initial_concept_version_id
    with database.session() as session:
        before_modules = {
            item.module_id: item.module_version_id
            for item in session.scalars(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == first["concept_version_id"]
                )
            )
        }

    with pytest.raises(ValueError, match="Concept version conflict"):
        builder.accept_atomic_note(
            concept_id=concept_id,
            module_key="overview",
            note_version_id=second_note_version_id,
            expected_concept_version_id=initial_concept_version_id,
            rationale="显式测试接纳",
            actor="test-user",
        )

    queued = builder.accept_atomic_note(
        concept_id=concept_id,
        module_key="overview",
        note_version_id=second_note_version_id,
        expected_concept_version_id=first["concept_version_id"],
        rationale="显式测试接纳",
        actor="test-user",
        run_inline=False,
    )
    assert queued["status"] == "QUEUED"
    with database.session() as session:
        queued_job = session.get(ConceptUpdateJob, queued["update_job_id"])
        assert queued_job.status == "QUEUED"
        assert queued_job.attempt_count == 0
    worker = JobWorker(
        _ConceptWorkerService(database, builder),
        worker_id="concept-worker-test",
    )
    updated = worker.run_once()
    assert updated["update_job_id"] == queued["update_job_id"]
    assert updated["concept_version_id"] != first["concept_version_id"]
    assert updated["update_job_id"]
    assert updated["candidate_concept_version_id"] == updated["concept_version_id"]

    with database.session() as session:
        contribution = session.get(ConceptContribution, updated["contribution_id"])
        assert contribution.decision_status == "ACCEPTED"
        assert contribution.contribution_role == "SUPPORT"
        assert contribution.assigned_by == "test-user"
        release_module = session.scalar(
            select(ConceptReleaseModule).where(
                ConceptReleaseModule.concept_version_id == updated["concept_version_id"]
            )
        )
        module_version = session.get(ConceptModuleVersion, release_module.module_version_id)
        snapshot = __import__("json").loads(module_version.payload_json)["_contributions"]
        assert {row["note_version_id"] for row in snapshot} == {
            versions[0].id,
            versions[1].id,
        }
        after_modules = {
            item.module_id: item.module_version_id
            for item in session.scalars(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == updated["concept_version_id"]
                )
            )
        }
        assert after_modules[overview_module_id] != before_modules[overview_module_id]
        assert after_modules[examples_module_id] == before_modules[examples_module_id]
        job = session.get(ConceptUpdateJob, updated["update_job_id"])
        assert job.status == "PUBLISHED"
        assert job.candidate_concept_version_id == updated["concept_version_id"]
        assert job.completed_at is not None
        judges = session.scalars(
            select(ConceptModuleJudgeRecord)
            .where(ConceptModuleJudgeRecord.concept_update_job_id == job.id)
            .order_by(ConceptModuleJudgeRecord.attempt_no)
        ).all()
        assert [item.decision for item in judges] == ["REPAIR", "PASS"]
        assert all(
            item.candidate_concept_version_id == updated["concept_version_id"] for item in judges
        )
        assert all(item.module_version_id == after_modules[overview_module_id] for item in judges)
        precise = session.scalars(
            select(ConceptContentEvidence).where(
                ConceptContentEvidence.module_version_id == after_modules[overview_module_id]
            )
        ).all()
        assert precise
        assert all(item.evidence_span_id and item.note_block_id for item in precise)
        assert job.attempt_count == 1
        assert job.lease_owner is None

    synthesis = service.get_synthesis(report.synthesis_id)
    assert synthesis["version_id"] != original_synthesis_version_id

    def flatten(nodes):
        return [node for item in nodes for node in [item, *flatten(item["children"])]]

    concept_node = next(
        node for node in flatten(synthesis["outline"]) if node["concept_id"] == concept_id
    )
    assert concept_node["concept_version_id"] == updated["concept_version_id"]
    pinned = service.get_concept(concept_id, version_id=concept_node["concept_version_id"])
    assert pinned["version_id"] == updated["concept_version_id"]
    assert pinned["schema_version"] == "concept-v3"
    assert pinned["scope"] == {
        "includes": ["测试"],
        "excludes": [],
        "prerequisites": [],
        "aliases": [],
    }
    assert pinned["blocks"][0]["content_blocks"][0]["type"] == "PROSE"
    assert len(pinned["blocks"][0]["content_blocks"][0]["evidence_refs"]) == 2
    refresh = builder.refresh_shared_concept_versions()
    assert refresh["status"] == "REFRESHED"
    assert refresh["active_concepts"] >= 1
    assert concept_id not in {item["concept_id"] for item in refresh["skipped_concepts"]}

    repeated = builder.accept_atomic_note(
        concept_id=concept_id,
        module_key="overview",
        note_version_id=second_note_version_id,
        expected_concept_version_id=updated["concept_version_id"],
        rationale="重复请求应保持幂等",
        actor="test-user",
    )
    assert repeated["status"] == "UNCHANGED"
    assert repeated["concept_version_id"] == updated["concept_version_id"]
    assert repeated["reused"] is True

    synthesis_before_failure = service.get_synthesis(report.synthesis_id)["version_id"]
    failing = SemanticSynthesisBuilder(database, _FallbackSemanticFixture())
    with pytest.raises(ValueError, match="candidate rejected"):
        failing.accept_atomic_note(
            concept_id=concept_id,
            module_key="examples",
            note_version_id=second_note_version_id,
            expected_concept_version_id=updated["concept_version_id"],
            rationale="验证 fallback 不得污染正式版本",
            actor="test-user",
        )
    with database.session() as session:
        concept = session.get(Concept, concept_id)
        assert concept.active_version_id == updated["concept_version_id"]
        failed_job = session.scalar(
            select(ConceptUpdateJob)
            .where(ConceptUpdateJob.status == "FAILED")
            .order_by(ConceptUpdateJob.created_at.desc())
        )
        assert failed_job is not None
        assert failed_job.fallback_used is True
        assert failed_job.error_code == "CANDIDATE_REJECTED"
        failed_contribution = session.get(ConceptContribution, failed_job.contribution_id)
        assert failed_contribution.decision_status == "APPROVED"
    assert service.get_synthesis(report.synthesis_id)["version_id"] == synthesis_before_failure

    supersede = builder.transition_contribution(
        contribution_id=updated["contribution_id"],
        action="SUPERSEDE",
        expected_concept_version_id=updated["concept_version_id"],
        replacement_note_version_id=versions[0].id,
        rationale="用新的活动来源替换旧支持来源",
        actor="test-user",
    )
    assert supersede["status"] == "QUEUED"
    superseded_release = builder.run_concept_update_job(supersede["update_job_id"])
    with database.session() as session:
        old = session.get(ConceptContribution, updated["contribution_id"])
        replacement = session.get(ConceptContribution, supersede["contribution_id"])
        assert old.decision_status == "SUPERSEDED"
        assert replacement.decision_status == "ACCEPTED"

    retract = builder.transition_contribution(
        contribution_id=supersede["contribution_id"],
        action="RETRACT",
        expected_concept_version_id=superseded_release["concept_version_id"],
        rationale="撤回不再需要的支持来源",
        actor="test-user",
    )
    retracted_release = builder.run_concept_update_job(retract["update_job_id"])
    assert retracted_release["operation"] == "REMOVE"
    with database.session() as session:
        replacement = session.get(ConceptContribution, supersede["contribution_id"])
        assert replacement.decision_status == "RETRACTED"
    assert migrated.source_version_id
    database.close()
