from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ...composition import Component
from ...db import (
    BlobObject,
    Concept,
    ConceptBuildRecord,
    ConceptContentEvidence,
    ConceptContribution,
    ConceptModule,
    ConceptModuleAsset,
    ConceptModuleJudgeRecord,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptUpdateJob,
    ConceptVersion,
    NoteAsset,
    NoteVersion,
    VisualAsset,
)
from ..machine_learning_blueprint import ConceptSpec
from .common import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    ModuleReviewRequired,
    _json,
    _loads,
    _sha256_text,
)


class CompilationModule(Component):
    def _compile_module_update_candidate(
        self,
        prepared: dict[str, Any],
        *,
        concept_id: str,
        base_version_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate and persist one READY module-level candidate without moving heads."""

        spec: ConceptSpec = prepared["spec"]
        modules: list[dict[str, Any]] = prepared["modules"]
        if len(modules) != 1:
            raise ValueError("Reliable Concept updates must target exactly one module")
        module_seed = modules[0]
        input_hash = _sha256_text(
            _json(
                {
                    "base_version_id": base_version_id,
                    "concept_id": concept_id,
                    "module_key": module_seed["block_key"],
                    "sources": sorted(
                        (
                            source["note_version_id"],
                            source["content_sha256"],
                            source.get("contribution_role", "FOUNDATION"),
                            _json(source.get("selected_block_ids", [])),
                        )
                        for source in module_seed["sources"]
                    ),
                }
            )
        )
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "GENERATING"
            job.input_sha256 = input_hash
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept_id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                    ConceptBuildRecord.result_status == "JUDGED",
                    ConceptBuildRecord.fallback_used.is_(False),
                )
            )
            if record is not None:
                candidate = session.get(ConceptVersion, record.concept_version_id)
                if candidate is not None and candidate.status in {"READY", "PUBLISHED"}:
                    job.status = "READY"
                    job.candidate_concept_version_id = candidate.id
                    return {
                        "concept_id": concept_id,
                        "concept_version_id": candidate.id,
                        "reused": True,
                        "fallback": False,
                    }

        draft, fallback, fallback_detail = self.owner._write_draft(
            spec, modules, self.owner._update_context(job_id)
        )
        if fallback:
            with self.owner.db.session() as session:
                job = session.get(ConceptUpdateJob, job_id)
                job.fallback_used = True
                job.fallback_detail = fallback_detail
            raise ValueError(
                f"Concept candidate rejected because generation used fallback: {fallback_detail}"
            )
        self.owner._validate_or_repair_update_draft(draft, module_seed)
        judge = self.owner._judge_module_candidate(
            spec=spec,
            module_seed=module_seed,
            module_draft=draft.modules[0],
            job_id=job_id,
            attempt_no=1,
        )
        if judge.decision == "REPAIR":
            draft.modules[0] = self.owner._repair_module_candidate(
                spec=spec,
                module_seed=module_seed,
                module_draft=draft.modules[0],
                judge=judge,
                job_id=job_id,
            )
            self.owner._validate_or_repair_update_draft(draft, module_seed)
            judge = self.owner._judge_module_candidate(
                spec=spec,
                module_seed=module_seed,
                module_draft=draft.modules[0],
                job_id=job_id,
                attempt_no=2,
            )
        if judge.decision != "PASS":
            if any(issue.issue_id == "judge-output-invalid" for issue in judge.issues):
                raise ValueError("Judge output schema invalid; retry model response")
            raise ModuleReviewRequired(
                f"Concept candidate rejected by independent Module Judge: {judge.summary}"
            )

        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            base = session.get(ConceptVersion, base_version_id)
            job = session.get(ConceptUpdateJob, job_id)
            if (
                concept is None
                or base is None
                or base.concept_id != concept.id
                or concept.active_version_id != base.id
            ):
                raise ValueError(
                    "Concept version conflict: candidate was not persisted; reload and retry"
                )
            target_module = session.scalar(
                select(ConceptModule).where(
                    ConceptModule.concept_id == concept.id,
                    ConceptModule.module_key == module_seed["block_key"],
                    ConceptModule.visibility == "ACTIVE",
                )
            )
            if target_module is None:
                raise ValueError("Target Concept module disappeared during generation")
            module_draft = draft.modules[0]
            note_version_ids = [item["note_version_id"] for item in module_seed["sources"]]
            contribution_snapshot = [
                {
                    "contribution_id": item.get("contribution_id", ""),
                    "note_version_id": item["note_version_id"],
                    "source_ref": item.get("source_ref", ""),
                    "role": item.get("contribution_role", "FOUNDATION"),
                    "selected_block_ids": item.get("selected_block_ids", []),
                }
                for item in module_seed["sources"]
            ]
            evidence_lookup: dict[str, tuple[str, str]] = {}
            selected_asset_blocks: dict[str, str] = {}
            for source in module_seed["sources"]:
                contribution_id = str(source.get("contribution_id") or "")
                for source_block in source.get("blocks", []):
                    note_block_id = str(source_block.get("note_block_id") or "")
                    for evidence in source_block.get("evidence", []):
                        span_id = str(evidence.get("evidence_span_id") or "")
                        if span_id:
                            evidence_lookup[span_id] = (contribution_id, note_block_id)
            for content_block in module_draft.payload.blocks:
                for asset_id in content_block.asset_ids:
                    selected_asset_blocks[asset_id] = content_block.block_key
            selected_asset_ids = set(selected_asset_blocks)
            assets = (
                session.execute(
                    select(NoteAsset, VisualAsset, BlobObject)
                    .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                    .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
                    .where(
                        NoteAsset.note_version_id.in_(note_version_ids),
                        NoteAsset.visual_asset_id.in_(selected_asset_ids),
                    )
                    .order_by(NoteAsset.order_index)
                ).all()
                if note_version_ids
                else []
            )
            unique_assets: list[tuple[NoteAsset, VisualAsset, BlobObject]] = []
            seen_assets: set[str] = set()
            for row in assets:
                if row[1].id not in seen_assets:
                    seen_assets.add(row[1].id)
                    unique_assets.append(row)
            stored_payload = module_draft.payload.model_dump()
            stored_payload["_contributions"] = contribution_snapshot
            module_hash = _sha256_text(
                _json(
                    {
                        "draft": module_draft.model_dump(),
                        "contributions": contribution_snapshot,
                        "asset_ids": [row[1].id for row in unique_assets],
                    }
                )
            )
            module_version = session.scalar(
                select(ConceptModuleVersion).where(
                    ConceptModuleVersion.module_id == target_module.id,
                    ConceptModuleVersion.content_sha256 == module_hash,
                )
            )
            if module_version is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptModuleVersion.version_no)).where(
                            ConceptModuleVersion.module_id == target_module.id
                        )
                    )
                    or 0
                ) + 1
                module_version = ConceptModuleVersion(
                    module_id=target_module.id,
                    version_no=version_no,
                    render_type=module_draft.render_type,
                    title=module_draft.title,
                    payload_json=_json(stored_payload),
                    plain_text=module_draft.plain_text(),
                    origin="ATOMIC_SYNTHESIZED",
                    lock_state="OPEN",
                    content_sha256=module_hash,
                    created_by=self.owner.agent.name,
                )
                session.add(module_version)
                session.flush()
            for content_block in module_draft.payload.blocks:
                for span_id in content_block.evidence_span_ids:
                    contribution_id, note_block_id = evidence_lookup[span_id]
                    existing_evidence = session.scalar(
                        select(ConceptContentEvidence).where(
                            ConceptContentEvidence.module_version_id == module_version.id,
                            ConceptContentEvidence.content_block_key == content_block.block_key,
                            ConceptContentEvidence.evidence_span_id == span_id,
                        )
                    )
                    if existing_evidence is None:
                        session.add(
                            ConceptContentEvidence(
                                module_version_id=module_version.id,
                                content_block_key=content_block.block_key,
                                contribution_id=contribution_id,
                                note_block_id=note_block_id,
                                evidence_span_id=span_id,
                            )
                        )
            for order, (note_asset, visual, _blob) in enumerate(unique_assets):
                placement = session.scalar(
                    select(ConceptModuleAsset).where(
                        ConceptModuleAsset.module_version_id == module_version.id,
                        ConceptModuleAsset.visual_asset_id == visual.id,
                    )
                )
                if placement is None:
                    session.add(
                        ConceptModuleAsset(
                            module_version_id=module_version.id,
                            visual_asset_id=visual.id,
                            content_block_key=selected_asset_blocks[visual.id],
                            order_index=order,
                            placement=note_asset.placement,
                            caption=visual.caption,
                            alt_text=note_asset.alt_text,
                        )
                    )
                else:
                    placement.content_block_key = selected_asset_blocks[visual.id]

            old_placements = session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == base.id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all()
            release_modules = [
                (
                    item.module_id,
                    module_version.id
                    if item.module_id == target_module.id
                    else item.module_version_id,
                    item.sort_order,
                )
                for item in old_placements
            ]
            if not any(mid == target_module.id for mid, _vid, _order in release_modules):
                if job.operation != "RECONCILE":
                    raise ValueError("Target module is not pinned by the expected Concept version")
                release_modules.append(
                    (
                        target_module.id,
                        module_version.id,
                        max([r[2] for r in release_modules], default=-1) + 1,
                    )
                )
            release_hash = _sha256_text(
                _json(
                    {
                        "title": base.title,
                        "summary": base.summary,
                        "scope": base.scope,
                        "modules": release_modules,
                    }
                )
            )
            candidate = session.scalar(
                select(ConceptVersion).where(
                    ConceptVersion.concept_id == concept.id,
                    ConceptVersion.content_sha256 == release_hash,
                    ConceptVersion.schema_version == SCHEMA_VERSION,
                )
            )
            if candidate is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptVersion.version_no)).where(
                            ConceptVersion.concept_id == concept.id
                        )
                    )
                    or 0
                ) + 1
                candidate = ConceptVersion(
                    concept_id=concept.id,
                    version_no=version_no,
                    title=base.title,
                    original_title=base.original_title,
                    scope=base.scope,
                    summary=base.summary,
                    content_sha256=release_hash,
                    schema_version=SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    status="READY",
                )
                session.add(candidate)
                session.flush()
                for module_id, module_version_id, sort_order in release_modules:
                    session.add(
                        ConceptReleaseModule(
                            concept_version_id=candidate.id,
                            module_id=module_id,
                            module_version_id=module_version_id,
                            sort_order=sort_order,
                        )
                    )
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept.id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                )
            )
            if record is None:
                record = ConceptBuildRecord(
                    concept_id=concept.id,
                    concept_version_id=candidate.id,
                    input_sha256=input_hash,
                    prompt_version=PROMPT_VERSION,
                    model_name=self.owner.agent.name,
                )
                session.add(record)
            record.concept_version_id = candidate.id
            record.result_status = "JUDGED"
            record.fallback_used = False
            record.fallback_detail = ""
            record.output_sha256 = candidate.content_sha256
            job.status = "READY"
            job.input_sha256 = input_hash
            job.candidate_concept_version_id = candidate.id
            for judge_record in session.scalars(
                select(ConceptModuleJudgeRecord).where(
                    ConceptModuleJudgeRecord.concept_update_job_id == job.id
                )
            ):
                judge_record.candidate_concept_version_id = candidate.id
                judge_record.module_version_id = module_version.id
            return {
                "concept_id": concept.id,
                "concept_version_id": candidate.id,
                "reused": False,
                "fallback": False,
            }

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            module = session.get(ConceptModule, job.module_id)
            judges = session.scalars(
                select(ConceptModuleJudgeRecord)
                .where(ConceptModuleJudgeRecord.concept_update_job_id == job.id)
                .order_by(ConceptModuleJudgeRecord.attempt_no)
            ).all()
            return {
                "job_id": job.id,
                "status": job.status,
                "concept_id": job.concept_id,
                "module_key": module.module_key if module else "",
                "note_version_id": job.atomic_note_version_id,
                "contribution_id": job.contribution_id,
                "operation": job.operation,
                "supersedes_contribution_id": job.supersedes_contribution_id,
                "expected_concept_version_id": job.expected_concept_version_id,
                "candidate_concept_version_id": job.candidate_concept_version_id,
                "input_sha256": job.input_sha256,
                "fallback_used": job.fallback_used,
                "fallback_detail": job.fallback_detail,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
                "lease_expires_at": (
                    job.lease_expires_at.isoformat() if job.lease_expires_at else None
                ),
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
                "completed_at": (job.completed_at.isoformat() if job.completed_at else None),
                "judges": [
                    {
                        "judge_id": item.id,
                        "attempt_no": item.attempt_no,
                        "decision": item.decision,
                        "summary": item.summary,
                        "issues": _loads(item.issues_json, []),
                        "input_sha256": item.input_sha256,
                        "output_sha256": item.output_sha256,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in judges
                ],
            }

    def list_concept_update_jobs(self, concept_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown Concept: {concept_id}")
            ids = session.scalars(
                select(ConceptUpdateJob.id)
                .where(ConceptUpdateJob.concept_id == concept_id)
                .order_by(ConceptUpdateJob.created_at.desc())
                .limit(max(1, min(limit, 50)))
            ).all()
        return [self.owner.get_concept_update_job(job_id) for job_id in ids]

    def list_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown Concept: {concept_id}")
            statement = (
                select(ConceptContribution, ConceptModule, NoteVersion)
                .join(ConceptModule, ConceptModule.id == ConceptContribution.module_id)
                .outerjoin(
                    NoteVersion,
                    NoteVersion.id == ConceptContribution.atomic_note_version_id,
                )
                .where(ConceptModule.concept_id == concept_id)
                .order_by(ConceptModule.module_key, ConceptContribution.created_at)
            )
            if module_key:
                statement = statement.where(ConceptModule.module_key == module_key)
            rows = session.execute(statement).all()
            return [
                {
                    "contribution_id": contribution.id,
                    "module_key": module.module_key,
                    "note_version_id": contribution.atomic_note_version_id,
                    "note_title": version.title if version else "",
                    "contribution_role": contribution.contribution_role,
                    "decision_status": contribution.decision_status,
                    "rationale": contribution.rationale,
                    "assigned_by": contribution.assigned_by,
                    "superseded_by_id": contribution.superseded_by_id,
                    "created_at": contribution.created_at.isoformat(),
                    "updated_at": (
                        contribution.updated_at.isoformat()
                        if contribution.updated_at
                        else contribution.created_at.isoformat()
                    ),
                }
                for contribution, module, version in rows
            ]

    def _compile_one(self, prepared: dict[str, Any]) -> dict[str, Any]:
        stable_key = self.owner.blueprint.stable_concept_key(prepared["spec"])
        with self.owner._concept_lock(stable_key):
            return self.owner._compile_one_locked(prepared)
