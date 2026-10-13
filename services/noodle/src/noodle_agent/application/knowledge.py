from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..composition import Component
from ..db import (
    Concept,
    ConceptContribution,
    ConceptModule,
    KnowledgeModuleState,
    KnowledgeSyncEvent,
)
from ..synthesis.knowledge_sync import blocked_concepts


class KnowledgeModule(Component):
    def register_backbone(self, pdf_path: Path) -> dict[str, Any]:
        return self.owner.synthesis.register_backbone(pdf_path).model_dump()

    def extract_backbone_figures(self, corpus_id: str) -> dict[str, Any]:
        return self.owner.synthesis.extract_figures(corpus_id).model_dump()

    def translate_backbone_figure_captions(
        self,
        corpus_id: str,
        *,
        batch_size: int = 16,
        concurrency: int = 4,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.owner.synthesis.translate_figure_captions(
            corpus_id,
            batch_size=batch_size,
            concurrency=concurrency,
            force=force,
        ).model_dump()

    def import_d2l_source(self, repository: Path) -> dict[str, Any]:
        return self.owner.d2l_importer.import_repository(repository).model_dump()

    def migrate_backbone_source(self, corpus_id: str) -> dict[str, Any]:
        return self.owner.backbone_migrator.migrate(corpus_id).model_dump()

    def build_semantic_synthesis(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> dict[str, Any]:
        return self.owner.semantic_synthesis.build(
            corpus_id, source_version_id, concurrency=concurrency
        ).model_dump()

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        return self.owner.semantic_synthesis.refresh_shared_concept_versions()

    def accept_atomic_note_contribution(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self.owner.semantic_synthesis.accept_atomic_note(
            concept_id=concept_id,
            module_key=module_key,
            note_version_id=note_version_id,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
            run_inline=False,
        )

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self.owner.semantic_synthesis.run_concept_update_job(job_id)

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self.owner.semantic_synthesis.get_concept_update_job(job_id)

    def list_concept_update_jobs(self, concept_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self.owner.semantic_synthesis.list_concept_update_jobs(concept_id, limit=limit)

    def list_concept_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        return self.owner.semantic_synthesis.list_contributions(concept_id, module_key=module_key)

    def transition_concept_contribution(self, **kwargs: Any) -> dict[str, Any]:
        return self.owner.semantic_synthesis.transition_contribution(**kwargs)

    def retry_concept_update_job(self, job_id: str, *, actor: str) -> dict[str, Any]:
        return self.owner.semantic_synthesis.retry_concept_update_job(job_id, actor=actor)

    def suggest_concept_candidates(
        self, concept_id: str, module_key: str, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Retrieve proposals only; similarity never grants write authority."""

        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            module = session.scalar(
                select(ConceptModule).where(
                    ConceptModule.concept_id == concept_id,
                    ConceptModule.module_key == module_key,
                    ConceptModule.visibility == "ACTIVE",
                )
            )
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown concept_id: {concept_id}")
            if module is None:
                raise ValueError(f"Unknown active module_key: {module_key}")
            linked = set(
                session.scalars(
                    select(ConceptContribution.atomic_note_version_id).where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.atomic_note_version_id.is_not(None),
                        ConceptContribution.decision_status.notin_(["REJECTED", "RETRACTED"]),
                    )
                ).all()
            )
            title = concept.canonical_name
        query = " ".join(item for item in [title, module_key.replace("-", " ")] if item)
        hits = self.owner.search(query, limit=max(limit * 3, 12))
        candidates = []
        for hit in hits:
            if hit.note_version_id in linked:
                continue
            pages = sorted(
                {
                    int(item["page_index"]) + 1
                    for item in hit.evidence
                    if item.get("page_index") is not None
                }
            )
            candidates.append(
                {
                    "concept_id": concept_id,
                    "module_key": module_key,
                    "note_id": hit.note_id,
                    "note_version_id": hit.note_version_id,
                    "title": hit.title,
                    "thesis": hit.thesis,
                    "score": round(hit.score, 6),
                    "pages": pages,
                    "evidence_count": len(hit.evidence),
                    "asset_count": len(hit.assets),
                    "proposal_only": True,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def get_concept_update_diff(self, job_id: str) -> dict[str, Any]:
        job = self.owner.get_concept_update_job(job_id)
        candidate_id = job.get("candidate_concept_version_id")
        if not candidate_id:
            return {**job, "diff_available": False, "modules": []}
        before = self.owner.get_concept(
            job["concept_id"], version_id=job["expected_concept_version_id"]
        )
        after = self.owner.get_concept(job["concept_id"], version_id=candidate_id)
        before_by_key = {item["block_key"]: item for item in before["blocks"]}
        after_by_key = {item["block_key"]: item for item in after["blocks"]}
        modules = []
        for key in sorted(set(before_by_key) | set(after_by_key)):
            old = before_by_key.get(key)
            new = after_by_key.get(key)
            old_content = old.get("content", "") if old else ""
            new_content = new.get("content", "") if new else ""
            modules.append(
                {
                    "module_key": key,
                    "title": (new or old or {}).get("title", key),
                    "status": (
                        "ADDED"
                        if old is None
                        else "REMOVED"
                        if new is None
                        else "CHANGED"
                        if old_content != new_content
                        else "UNCHANGED"
                    ),
                    "before": old_content,
                    "after": new_content,
                    "before_module_version_id": old.get("block_version_id") if old else None,
                    "after_module_version_id": new.get("block_version_id") if new else None,
                }
            )
        return {**job, "diff_available": True, "modules": modules}

    def get_backbone_figure_path(self, figure_id: str) -> Path:
        return self.owner.synthesis.get_figure_path(figure_id)

    def enrich_backbone(
        self,
        corpus_id: str,
        *,
        limit: int | None = None,
        concurrency: int | None = None,
        include_published: bool = False,
        long_leaves_only: bool = False,
    ) -> dict[str, Any]:
        return self.owner.synthesis.enrich_pending(
            corpus_id,
            limit=limit,
            concurrency=concurrency or self.owner.settings.draft_concurrency,
            include_published=include_published,
            long_leaves_only=long_leaves_only,
        )

    def list_syntheses(self) -> list[dict[str, Any]]:
        return self.owner.synthesis.list_syntheses()

    def get_synthesis(self, synthesis_id: str) -> dict[str, Any]:
        return self.owner.synthesis.get_synthesis(synthesis_id)

    def get_concept(self, concept_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        resolved_id = (
            concept_id
            if version_id
            else self.owner.knowledge_curation.resolve_concept_id(concept_id)
        )
        # A historical pinned version belongs to the pre-merge identity and
        # cannot be applied to its canonical redirect.
        resolved_version = version_id if resolved_id == concept_id else None
        result = self.owner.synthesis.get_concept(resolved_id, version_id=resolved_version)
        with self.owner.db.session() as session:
            identity = session.get(Concept, resolved_id)
            current_read = version_id is None or bool(
                identity and identity.active_version_id == resolved_version
            )
            blocked = current_read and resolved_id in blocked_concepts(session)
            sync_status = "READY" if current_read else "HISTORICAL"
            if blocked:
                states = session.scalars(
                    select(KnowledgeModuleState)
                    .join(ConceptModule, ConceptModule.id == KnowledgeModuleState.module_id)
                    .where(
                        ConceptModule.concept_id == resolved_id,
                        KnowledgeModuleState.dirty.is_(True),
                    )
                ).all()
                events = [session.get(KnowledgeSyncEvent, item.event_id) for item in states]
                busy = any(e and e.status in {"QUEUED", "RUNNING"} for e in events)
                sync_status = (
                    "REBUILDING"
                    if busy
                    else "NEEDS_REVIEW"
                    if any(e and e.status == "COMPLETED_WITH_REVIEW" for e in events)
                    else "SYNC_FAILED"
                )
        result["sync_status"] = sync_status
        if blocked:
            result["blocks"] = []
            result["figures"] = []
            result["summary"] = (
                "来源已变更，知识正在同步；暂不提供旧内容。"
                if sync_status == "REBUILDING"
                else "有效来源重建的候选尚未通过检查，当前知识暂停用于阅读与检索。请在工作台的知识同步中查看原因并重新审校。"
            )
        return result

    def list_concepts(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        return self.owner.knowledge_curation.list_concepts(query, limit=limit)

    def create_synthesis(
        self,
        *,
        stable_key: str,
        domain_key: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self.owner.knowledge_curation.create_synthesis(
            stable_key=stable_key,
            domain_key=domain_key,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def publish_synthesis(
        self,
        synthesis_id: str,
        *,
        expected_version_id: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self.owner.knowledge_curation.publish_synthesis(
            synthesis_id=synthesis_id,
            expected_version_id=expected_version_id,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def merge_concepts(
        self,
        *,
        source_concept_id: str,
        target_concept_id: str,
        expected_source_version_id: str,
        expected_target_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self.owner.knowledge_curation.merge_concepts(
            source_concept_id=source_concept_id,
            target_concept_id=target_concept_id,
            expected_source_version_id=expected_source_version_id,
            expected_target_version_id=expected_target_version_id,
            rationale=rationale,
            actor=actor,
        )
