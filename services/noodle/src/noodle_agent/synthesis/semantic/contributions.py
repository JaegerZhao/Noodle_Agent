from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ...composition import Component
from ...db import (
    AtomicNote,
    Concept,
    ConceptContribution,
    ConceptModule,
    ConceptUpdateJob,
    ConceptVersion,
    NoteVersion,
    utcnow,
)
from .common import (
    ConceptUpdateError,
    _json,
    _loads,
)


class ContributionsModule(Component):
    def _ensure_concepts(self) -> None:
        with self.owner.db.session() as session:
            for spec in self.owner.blueprint.concepts:
                stable_key = self.owner.blueprint.stable_concept_key(spec)
                concept = session.scalar(select(Concept).where(Concept.stable_key == stable_key))
                if concept is None:
                    session.add(
                        Concept(
                            stable_key=stable_key,
                            domain_key=self.owner.blueprint.domain_key,
                            canonical_name=spec["title"],
                            original_name=spec["title"],
                            status="PLANNED",
                        )
                    )

    def accept_atomic_note(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        run_inline: bool = True,
    ) -> dict[str, Any]:
        """Explicitly accept one non-backbone AtomicNote and rebuild one Concept."""

        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None:
                raise ValueError(f"Unknown Concept: {concept_id}")
            stable_key = concept.stable_key
        with self.owner._concept_lock(stable_key):
            return self.owner._accept_atomic_note_locked(
                concept_id=concept_id,
                module_key=module_key,
                note_version_id=note_version_id,
                expected_concept_version_id=expected_concept_version_id,
                rationale=rationale,
                actor=actor,
                run_inline=run_inline,
            )

    def _accept_atomic_note_locked(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        run_inline: bool,
    ) -> dict[str, Any]:

        with self.owner.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown Concept: {concept_id}")
            if concept.active_version_id != expected_concept_version_id:
                raise ValueError(
                    "Concept version conflict: reload the Concept before adding a contribution"
                )
            module = session.scalar(
                select(ConceptModule).where(
                    ConceptModule.concept_id == concept.id,
                    ConceptModule.module_key == module_key,
                    ConceptModule.visibility == "ACTIVE",
                )
            )
            if module is None:
                raise ValueError(
                    f"Unknown active module_key for Concept {concept_id}: {module_key}"
                )
            version = session.get(NoteVersion, note_version_id)
            note = session.get(AtomicNote, version.note_id) if version else None
            if (
                version is None
                or note is None
                or note.visibility != "ACTIVE"
                or note.active_version_id != version.id
            ):
                raise ValueError(
                    "AtomicNote version is not the current active version; reload it first"
                )
            contribution = session.scalar(
                select(ConceptContribution).where(
                    ConceptContribution.module_id == module.id,
                    ConceptContribution.atomic_note_version_id == version.id,
                    ConceptContribution.contribution_role == "SUPPORT",
                )
            )
            if (
                contribution is not None
                and contribution.decision_status == "ACCEPTED"
                and self.owner._release_contains_contribution(
                    session, concept.active_version_id, module.id, contribution.id
                )
            ):
                previous_job = session.scalar(
                    select(ConceptUpdateJob)
                    .where(
                        ConceptUpdateJob.contribution_id == contribution.id,
                        ConceptUpdateJob.status == "PUBLISHED",
                    )
                    .order_by(ConceptUpdateJob.created_at.desc())
                )
                return {
                    "status": "UNCHANGED",
                    "concept_id": concept.id,
                    "concept_version_id": concept.active_version_id,
                    "candidate_concept_version_id": concept.active_version_id,
                    "contribution_id": contribution.id,
                    "update_job_id": previous_job.id if previous_job else "",
                    "note_version_id": note_version_id,
                    "module_key": module_key,
                    "reused": True,
                    "synthesis_versions": [],
                }
            if contribution is None:
                contribution = ConceptContribution(
                    module_id=module.id,
                    atomic_note_version_id=version.id,
                    source_role="ATOMIC_NOTE",
                    source_ref=f"AtomicNote:{note.stable_key}:{version.id}",
                    contribution_role="SUPPORT",
                )
                session.add(contribution)
                session.flush()
            contribution.decision_status = "APPROVED"
            contribution.rationale = rationale.strip()
            contribution.assigned_by = actor
            contribution_id = contribution.id
            job = ConceptUpdateJob(
                concept_id=concept.id,
                module_id=module.id,
                atomic_note_version_id=version.id,
                contribution_id=contribution.id,
                operation="ADD",
                expected_concept_version_id=expected_concept_version_id,
                status="QUEUED",
                actor=actor,
                rationale=rationale.strip(),
            )
            session.add(job)
            session.flush()
            job_id = job.id
        if not run_inline:
            return {
                "status": "QUEUED",
                "concept_id": concept_id,
                "concept_version_id": expected_concept_version_id,
                "candidate_concept_version_id": "",
                "contribution_id": contribution_id,
                "update_job_id": job_id,
                "note_version_id": note_version_id,
                "module_key": module_key,
                "reused": False,
                "synthesis_versions": [],
            }
        return self.owner._run_concept_update_job_locked(job_id)

    def transition_contribution(
        self,
        *,
        contribution_id: str,
        action: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        replacement_note_version_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply an audited lifecycle transition; rebuilds are always queued."""

        normalized_action = action.strip().upper()
        if normalized_action not in {"REJECT", "RETRACT", "SUPERSEDE"}:
            raise ValueError(f"Unsupported Contribution action: {action}")
        with self.owner.db.session() as session:
            contribution = session.get(ConceptContribution, contribution_id)
            module = session.get(ConceptModule, contribution.module_id) if contribution else None
            concept = session.get(Concept, module.concept_id) if module else None
            if contribution is None or module is None or concept is None:
                raise ValueError(f"Unknown ConceptContribution: {contribution_id}")
            if concept.active_version_id != expected_concept_version_id:
                raise ValueError("Concept version conflict: reload before changing Contribution")
            if normalized_action == "REJECT":
                if contribution.decision_status != "APPROVED":
                    raise ValueError("Only APPROVED Contribution can be rejected")
                contribution.decision_status = "REJECTED"
                contribution.rationale = rationale.strip()
                contribution.assigned_by = actor
                contribution.decided_at = utcnow()
                pending_jobs = session.scalars(
                    select(ConceptUpdateJob).where(
                        ConceptUpdateJob.contribution_id == contribution.id,
                        ConceptUpdateJob.status.in_(["QUEUED", "RUNNING"]),
                    )
                ).all()
                for job in pending_jobs:
                    job.status = "CANCELLED"
                    job.error_code = "CONTRIBUTION_REJECTED"
                    job.error_detail = rationale.strip()
                    job.completed_at = utcnow()
                return {
                    "status": "REJECTED",
                    "contribution_id": contribution.id,
                    "concept_id": concept.id,
                    "update_job_id": "",
                }
            if contribution.decision_status != "ACCEPTED":
                raise ValueError(
                    f"Only ACCEPTED Contribution can be {normalized_action.casefold()}d"
                )
            replacement: ConceptContribution | None = None
            operation = "REMOVE"
            if normalized_action == "RETRACT":
                remaining = (
                    session.scalar(
                        select(func.count(ConceptContribution.id)).where(
                            ConceptContribution.module_id == module.id,
                            ConceptContribution.id != contribution.id,
                            ConceptContribution.decision_status == "ACCEPTED",
                        )
                    )
                    or 0
                )
                if remaining < 1:
                    raise ValueError("Cannot retract the last accepted source of a Module")
                contribution.decision_status = "RETRACT_PENDING"
                job_contribution = contribution
            else:
                if not replacement_note_version_id:
                    raise ValueError("SUPERSEDE requires replacement_note_version_id")
                version = session.get(NoteVersion, replacement_note_version_id)
                note = session.get(AtomicNote, version.note_id) if version else None
                if (
                    version is None
                    or note is None
                    or note.visibility != "ACTIVE"
                    or note.active_version_id != version.id
                ):
                    raise ValueError("Replacement AtomicNote version is not active")
                replacement = session.scalar(
                    select(ConceptContribution).where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.atomic_note_version_id == version.id,
                        ConceptContribution.contribution_role == contribution.contribution_role,
                    )
                )
                if replacement is None:
                    replacement = ConceptContribution(
                        module_id=module.id,
                        atomic_note_version_id=version.id,
                        source_role="ATOMIC_NOTE",
                        source_ref=f"AtomicNote:{note.stable_key}:{version.id}",
                        contribution_role=contribution.contribution_role,
                    )
                    session.add(replacement)
                    session.flush()
                replacement.decision_status = "APPROVED"
                replacement.rationale = rationale.strip()
                replacement.assigned_by = actor
                contribution.decision_status = "SUPERSEDE_PENDING"
                contribution.superseded_by_id = replacement.id
                job_contribution = replacement
                operation = "REPLACE"
            job = ConceptUpdateJob(
                concept_id=concept.id,
                module_id=module.id,
                atomic_note_version_id=job_contribution.atomic_note_version_id,
                contribution_id=job_contribution.id,
                operation=operation,
                supersedes_contribution_id=(contribution.id if operation == "REPLACE" else None),
                expected_concept_version_id=expected_concept_version_id,
                status="QUEUED",
                actor=actor,
                rationale=rationale.strip(),
            )
            session.add(job)
            session.flush()
            return {
                "status": "QUEUED",
                "action": normalized_action,
                "contribution_id": job_contribution.id,
                "previous_contribution_id": contribution.id,
                "concept_id": concept.id,
                "module_key": module.module_key,
                "update_job_id": job.id,
            }

    def retry_concept_update_job(self, job_id: str, *, actor: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            if job.status != "FAILED":
                raise ValueError("Only FAILED Concept update jobs can be retried")
            contribution = session.get(ConceptContribution, job.contribution_id)
            if contribution is None or contribution.decision_status not in {
                "APPROVED",
                "RETRACT_PENDING",
            }:
                raise ValueError("Contribution state no longer permits retry")
            job.status = "QUEUED"
            job.actor = actor
            job.error_code = ""
            job.error_detail = ""
            job.completed_at = None
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_expires_at = None
        return self.owner.get_concept_update_job(job_id)

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        """Execute or recover one durable Concept update claimed by the Worker."""

        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            concept = session.get(Concept, job.concept_id) if job else None
            if job is None or concept is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            stable_key = concept.stable_key
        with self.owner._concept_lock(stable_key):
            return self.owner._run_concept_update_job_locked(job_id)

    def _run_concept_update_job_locked(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            if job.status == "PUBLISHED":
                return _loads(job.result_json, {"status": "UPDATED", "update_job_id": job.id})
            concept = session.get(Concept, job.concept_id)
            module = session.get(ConceptModule, job.module_id)
            contribution = session.get(ConceptContribution, job.contribution_id)
            note_version = session.get(NoteVersion, job.atomic_note_version_id)
            note = session.get(AtomicNote, note_version.note_id) if note_version else None
            if not all([concept, module, contribution, note_version, note]):
                raise ValueError("Concept update job references missing domain records")
            concept_id = concept.id
            module_key = module.module_key
            contribution_id = contribution.id
            note_version_id = note_version.id
            source_version_id = note_version.source_version_id
            expected_concept_version_id = job.expected_concept_version_id
            operation = job.operation or "ADD"
            supersedes_contribution_id = job.supersedes_contribution_id
            candidate_id = job.candidate_concept_version_id or ""
            resume_publication = bool(
                candidate_id
                and concept.active_version_id == candidate_id
                and job.status == "PUBLISHING"
            )
            current = session.get(ConceptVersion, expected_concept_version_id)
            spec: Any = {
                "key": concept.stable_key,
                "stable_key": concept.stable_key,
                "title": concept.canonical_name,
                "parent_key": "",
                "kind": "COMPOSITE",
                "source_prefixes": [],
                "summary_hint": current.summary if current else concept.canonical_name,
            }
            job.status = "PUBLISHING" if resume_publication else "RUNNING"
            job.error_code = ""
            job.error_detail = ""

        published = resume_publication
        try:
            if not resume_publication:
                included = {contribution_id} if operation in {"ADD", "REPLACE"} else set()
                excluded = (
                    {contribution_id}
                    if operation == "REMOVE"
                    else {supersedes_contribution_id}
                    if operation == "REPLACE" and supersedes_contribution_id
                    else set()
                )
                prepared = self.owner._prepared_from_contributions(
                    spec,
                    include_contribution_ids=included,
                    exclude_contribution_ids=excluded,
                )
                prepared["modules"] = [
                    item for item in prepared["modules"] if item["block_key"] == module_key
                ]
                if len(prepared["modules"]) != 1:
                    raise ValueError(
                        f"Approved contribution did not produce target module input: {module_key}"
                    )
                result = self.owner._compile_module_update_candidate(
                    prepared,
                    concept_id=concept_id,
                    base_version_id=expected_concept_version_id,
                    job_id=job_id,
                )
                candidate_id = result["concept_version_id"]
                with self.owner.db.session() as session:
                    concept = session.get(Concept, concept_id)
                    contribution = session.get(ConceptContribution, contribution_id)
                    job = session.get(ConceptUpdateJob, job_id)
                    candidate = session.get(ConceptVersion, candidate_id)
                    note_version = session.get(NoteVersion, note_version_id)
                    note = session.get(AtomicNote, note_version.note_id) if note_version else None
                    if concept is None or concept.active_version_id != expected_concept_version_id:
                        raise ValueError(
                            "Concept version conflict: candidate was not published; reload and retry"
                        )
                    expected_status = "RETRACT_PENDING" if operation == "REMOVE" else "APPROVED"
                    superseded = (
                        session.get(ConceptContribution, supersedes_contribution_id)
                        if supersedes_contribution_id
                        else None
                    )
                    if (
                        contribution is None
                        or contribution.decision_status != expected_status
                        or candidate is None
                        or candidate.concept_id != concept.id
                        or candidate.status not in {"READY", "PUBLISHED"}
                        or note_version is None
                        or note is None
                        or (
                            operation != "REMOVE"
                            and (
                                note.visibility != "ACTIVE"
                                or note.active_version_id != note_version.id
                            )
                        )
                        or (
                            operation == "REPLACE"
                            and (
                                superseded is None
                                or superseded.decision_status != "SUPERSEDE_PENDING"
                            )
                        )
                    ):
                        raise ValueError("Concept candidate publication preconditions changed")
                    candidate.status = "PUBLISHED"
                    concept.active_version_id = candidate.id
                    concept.status = "PUBLISHED"
                    contribution.decision_status = (
                        "RETRACTED" if operation == "REMOVE" else "ACCEPTED"
                    )
                    contribution.decided_at = utcnow()
                    if superseded is not None:
                        superseded.decision_status = "SUPERSEDED"
                        superseded.decided_at = utcnow()
                    job.status = "PUBLISHING"
                    job.candidate_concept_version_id = candidate.id
                published = True
            else:
                result = {"reused": True}

            refreshed = self.owner._refresh_synthesis_releases(
                {concept_id: candidate_id},
                source_version_id=source_version_id,
            )
            response = {
                "status": "UPDATED",
                "concept_id": concept_id,
                "concept_version_id": candidate_id,
                "candidate_concept_version_id": candidate_id,
                "contribution_id": contribution_id,
                "update_job_id": job_id,
                "note_version_id": note_version_id,
                "module_key": module_key,
                "operation": operation,
                "reused": bool(result.get("reused")),
                "synthesis_versions": refreshed,
            }
            with self.owner.db.session() as session:
                job = session.get(ConceptUpdateJob, job_id)
                job.status = "PUBLISHED"
                job.result_json = _json(response)
                job.completed_at = utcnow()
            return response
        except Exception as exc:
            error_code = (
                "VERSION_CONFLICT"
                if "version conflict" in str(exc).casefold()
                else "CANDIDATE_REJECTED"
                if "candidate" in str(exc).casefold() or "judge" in str(exc).casefold()
                else "BUILD_FAILED"
            )
            if published:
                with self.owner.db.session() as session:
                    concept = session.get(Concept, concept_id)
                    contribution = session.get(ConceptContribution, contribution_id)
                    superseded = (
                        session.get(ConceptContribution, supersedes_contribution_id)
                        if supersedes_contribution_id
                        else None
                    )
                    candidate = session.get(ConceptVersion, candidate_id)
                    if concept is not None and concept.active_version_id == candidate_id:
                        concept.active_version_id = expected_concept_version_id
                    if contribution is not None:
                        contribution.decision_status = (
                            "RETRACT_PENDING" if operation == "REMOVE" else "APPROVED"
                        )
                    if superseded is not None:
                        superseded.decision_status = "SUPERSEDE_PENDING"
                    if candidate is not None:
                        candidate.status = "READY"
            with self.owner.db.session() as session:
                job = session.get(ConceptUpdateJob, job_id)
                if job is not None and job.status != "PUBLISHED":
                    job.status = "FAILED"
                    job.candidate_concept_version_id = candidate_id or None
                    job.error_code = error_code
                    job.error_detail = str(exc)[:8_000]
                    job.completed_at = utcnow()
            message = (
                "Concept update publication was compensated after Synthesis refresh failed"
                if published
                else str(exc)
            )
            raise ConceptUpdateError(message, job_id=job_id, error_code=error_code) from exc
