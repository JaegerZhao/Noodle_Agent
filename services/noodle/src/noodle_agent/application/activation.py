from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from ..composition import Component
from ..db import (
    Job,
    KnowledgeMapRecord,
    NoteCandidate,
    Source,
    SourceVersion,
)
from ..domain import (
    JobStage,
    JobStatus,
    KnowledgePlan,
    ParsedDocument,
)
from ..release import apply_outline_title_patches, build_release_manifest
from .common import (
    _has_material_warnings,
    _json,
    _parse_json,
)


class ActivationModule(Component):
    def _generate_and_activate(self, job_id: str) -> None:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            state = _parse_json(job.state_json, {})
            normalized_path = Path(state["normalized_path"])
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
            job.stage = JobStage.GENERATING.value
            job.progress = 0.5
        self.owner.telemetry.transition_stage(job_id, JobStage.GENERATING.value, 0.5)
        document = ParsedDocument.model_validate_json(normalized_path.read_text(encoding="utf-8"))
        resumable_methods = (
            "profile_document",
            "build_knowledge_map",
            "plan_source_note_bundle",
            "repair_note_set",
            "draft_note",
            "evaluate_plan",
        )
        if all(callable(getattr(self.owner.agent, name, None)) for name in resumable_methods):
            generation = self.owner._build_generation_resumable(job_id, document, normalized_path)
        else:
            build_generation = getattr(self.owner.agent, "build_generation", None)
            generation = build_generation(document) if callable(build_generation) else None
        plan = (
            generation.final_plan
            if generation is not None
            else self.owner.agent.build_plan(document)
        )
        if generation is not None and generation.knowledge_map.concepts and not plan.notes:
            raise RuntimeError(
                "The note-set planner returned no notes for a non-empty knowledge map"
            )
        map_payload = (
            generation.model_dump_json() if generation is not None else plan.model_dump_json()
        )
        with self.owner.db.session() as session:
            existing_map = session.scalar(
                select(KnowledgeMapRecord).where(
                    KnowledgeMapRecord.source_version_id == source_version.id,
                    KnowledgeMapRecord.parse_run_id == job.parse_run_id,
                )
            )
            if existing_map is None:
                existing_map = KnowledgeMapRecord(
                    source_version_id=source_version.id,
                    parse_run_id=job.parse_run_id,
                    summary=plan.document_summary,
                    map_json=map_payload,
                )
                session.add(existing_map)
                session.flush()
            for note in plan.notes:
                candidate = session.scalar(
                    select(NoteCandidate).where(
                        NoteCandidate.knowledge_map_id == existing_map.id,
                        NoteCandidate.stable_key == note.stable_key,
                    )
                )
                if candidate is None:
                    session.add(
                        NoteCandidate(
                            knowledge_map_id=existing_map.id,
                            stable_key=note.stable_key,
                            payload_json=note.model_dump_json(),
                        )
                    )
            knowledge_map_id = existing_map.id
            job = session.get(Job, job_id)
            job.stage = JobStage.VALIDATING_NOTES.value
            job.progress = 0.94
        self.owner.telemetry.transition_stage(job_id, JobStage.VALIDATING_NOTES.value, 0.94)

        visual_context = (
            dict(
                self.owner._read_generation_artifact(
                    normalized_path.parent / "generation-v4.json"
                ).get("visual_understandings")
                or {}
            )
            if generation is not None
            else {}
        )
        passed, failures = self.owner._validate_plan(
            document,
            plan,
            generation.note_set_plan if generation is not None else None,
            visual_context,
        )
        quality_failures = []
        if generation is not None:
            quality_failures = [
                {
                    "code": "CANDIDATE_QUALITY_FAILED",
                    "candidate": evaluation.stable_key,
                    "detail": evaluation.issues,
                }
                for evaluation in generation.evaluations
                if not evaluation.passed
            ]
            quality_failed_keys = {str(item["candidate"]) for item in quality_failures}
            passed = [note for note in passed if note.stable_key not in quality_failed_keys]
            existing_failure_keys = {str(item.get("candidate")) for item in failures}
            failures.extend(
                item
                for item in quality_failures
                if str(item["candidate"]) not in existing_failure_keys
            )
        repaired_count = 0
        repair_attempted_keys: set[str] = set()
        final_evaluations_by_key = {
            evaluation.stable_key: evaluation
            for evaluation in (generation.evaluations if generation is not None else [])
        }
        repair = getattr(self.owner.agent, "repair_plan", None)
        if failures and callable(repair):
            original_failures = {str(item.get("candidate")): item for item in failures}
            repair_attempted_keys = set(original_failures)
            repaired_plan = self.owner._invoke_agent_with_task_retries(
                job_id,
                JobStage.VALIDATING_NOTES,
                repair,
                document,
                plan,
                failures,
                **({"note_set": generation.note_set_plan} if generation is not None else {}),
                **({"vision_context": visual_context} if generation is not None else {}),
                note_key="targeted-repair",
            )
            repaired, remaining_failures = self.owner._validate_plan(
                document,
                repaired_plan,
                generation.note_set_plan if generation is not None else None,
                visual_context,
            )
            evaluate = getattr(self.owner.agent, "evaluate_plan", None)
            if generation is not None and repaired and callable(evaluate):
                repaired_evaluations = self.owner._invoke_agent(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    evaluate,
                    document,
                    generation.note_set_plan,
                    KnowledgePlan(
                        document_summary=plan.document_summary,
                        notes=repaired,
                        skipped_sections=[],
                    ),
                    generation.knowledge_map,
                    visual_context,
                    note_key="targeted-repair-judge",
                )
                repaired_quality_failures = [
                    {
                        "code": "CANDIDATE_QUALITY_FAILED",
                        "candidate": evaluation.stable_key,
                        "detail": evaluation.issues,
                    }
                    for evaluation in repaired_evaluations
                    if not evaluation.passed
                ]
                final_evaluations_by_key.update(
                    {evaluation.stable_key: evaluation for evaluation in repaired_evaluations}
                )
                repaired_failed_keys = {
                    str(item["candidate"]) for item in repaired_quality_failures
                }
                repaired = [
                    note for note in repaired if note.stable_key not in repaired_failed_keys
                ]
                remaining_failures.extend(repaired_quality_failures)
            existing_keys = {note.stable_key for note in passed}
            repaired = [
                note
                for note in repaired
                if note.stable_key in repair_attempted_keys and note.stable_key not in existing_keys
            ]
            passed.extend(repaired)
            repaired_count = len(repaired)
            if generation is not None:
                specs_by_key = {spec.stable_key: spec for spec in generation.note_set_plan.notes}
                for repaired_note in repaired:
                    spec = specs_by_key.get(repaired_note.stable_key)
                    if spec is None:
                        continue
                    if spec.title != repaired_note.title:
                        spec.title = repaired_note.title
                        spec.question_answered = f"来源材料如何说明{repaired_note.title}？"
                    spec.target_thesis = repaired_note.thesis
            returned_keys = {note.stable_key for note in repaired_plan.notes}
            remaining_failures.extend(
                failure for key, failure in original_failures.items() if key not in returned_keys
            )
            failures = remaining_failures

        source_note_outline = (
            generation.source_note_outline
            if generation is not None
            else self.owner._fallback_source_note_plan(source.title, plan)[0]
        )
        note_specs = (
            generation.note_set_plan
            if generation is not None
            else self.owner._fallback_source_note_plan(source.title, plan)[1]
        )
        release_review = None
        release_problem_keys: set[str] = set()
        review_release = getattr(self.owner.agent, "review_source_release", None)
        # Source-level LLM approval duplicated the per-note Judge and could turn
        # a local repair into a global rewrite. MVP publication is therefore
        # deterministic; whole-source review remains an explicit CLI action.
        requires_release_review = False
        if (
            generation is not None
            and passed
            and requires_release_review
            and callable(review_release)
        ):
            release_review = self.owner._invoke_agent(
                job_id,
                JobStage.VALIDATING_NOTES,
                review_release,
                document,
                source_note_outline,
                note_specs,
                passed,
                note_key="source-release-review",
            )
            source_note_outline = apply_outline_title_patches(source_note_outline, release_review)
            release_problem_keys = {item.stable_key for item in release_review.note_issues}
            if release_problem_keys and callable(repair):
                release_failures = [
                    {
                        "code": "SOURCE_RELEASE_QUALITY_FAILED",
                        "candidate": item.stable_key,
                        "detail": item.issues,
                    }
                    for item in release_review.note_issues
                ]
                release_repair_plan = self.owner._invoke_agent_with_task_retries(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    repair,
                    document,
                    KnowledgePlan(
                        document_summary=plan.document_summary,
                        notes=passed,
                    ),
                    release_failures,
                    note_set=note_specs,
                    vision_context=visual_context,
                    note_key="source-release-repair",
                )
                release_repaired, release_validation_failures = self.owner._validate_plan(
                    document,
                    release_repair_plan,
                    note_specs,
                    visual_context,
                )
                release_evaluations = (
                    self.owner._invoke_agent(
                        job_id,
                        JobStage.VALIDATING_NOTES,
                        self.owner.agent.evaluate_plan,
                        document,
                        note_specs,
                        KnowledgePlan(
                            document_summary=plan.document_summary,
                            notes=release_repaired,
                        ),
                        generation.knowledge_map,
                        visual_context,
                        note_key="source-release-repair-judge",
                    )
                    if release_repaired
                    else []
                )
                final_evaluations_by_key.update(
                    {evaluation.stable_key: evaluation for evaluation in release_evaluations}
                )
                failed_release_keys = {
                    str(item.get("candidate")) for item in release_validation_failures
                } | {
                    evaluation.stable_key
                    for evaluation in release_evaluations
                    if not evaluation.passed
                }
                replacements = {
                    note.stable_key: note
                    for note in release_repaired
                    if note.stable_key in release_problem_keys
                    and note.stable_key not in failed_release_keys
                }
                release_issues_by_key = {
                    item.stable_key: item.issues for item in release_review.note_issues
                }
                specs_by_key = {spec.stable_key: spec for spec in note_specs.notes}
                for key, replacement in replacements.items():
                    spec = specs_by_key.get(key)
                    if spec is None:
                        continue
                    spec.title = replacement.title
                    spec.target_thesis = replacement.thesis
                    if any(
                        "TITLE_BODY_ALIGNMENT" in issue
                        for issue in release_issues_by_key.get(key, [])
                    ):
                        # The planner's original question can itself contain an
                        # unsupported relation (for example, asking how two
                        # merely adjacent strategies "combine"). Once a
                        # grounded release repair is accepted, keep retrieval
                        # metadata aligned with the corrected note instead of
                        # letting stale planning text fail every recheck.
                        spec.question_answered = f"来源材料如何说明{replacement.title}？"
                passed = [
                    replacements.get(note.stable_key, note)
                    for note in passed
                    if note.stable_key not in release_problem_keys
                    or note.stable_key in replacements
                ]
                repaired_count += len(replacements)
                unresolved_release_keys = release_problem_keys - set(replacements)
                failures.extend(release_validation_failures)
                failures.extend(
                    {
                        "code": "SOURCE_RELEASE_QUALITY_FAILED",
                        "candidate": key,
                        "detail": next(
                            item.issues
                            for item in release_review.note_issues
                            if item.stable_key == key
                        ),
                    }
                    for key in unresolved_release_keys
                )
                release_review = self.owner._invoke_agent(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    review_release,
                    document,
                    source_note_outline,
                    note_specs,
                    passed,
                    note_key="source-release-recheck",
                )
                source_note_outline = apply_outline_title_patches(
                    source_note_outline, release_review
                )
                still_bad = {item.stable_key for item in release_review.note_issues}
                if still_bad:
                    passed = [note for note in passed if note.stable_key not in still_bad]
                    failures.extend(
                        {
                            "code": "SOURCE_RELEASE_QUALITY_FAILED",
                            "candidate": item.stable_key,
                            "detail": item.issues,
                        }
                        for item in release_review.note_issues
                    )
            if release_review.global_issues:
                failures.append(
                    {
                        "code": "SOURCE_RELEASE_GLOBAL_WARNING",
                        "candidate": "__source__",
                        "detail": release_review.global_issues,
                    }
                )

        repaired_or_released_keys = repair_attempted_keys | {
            note.stable_key for note in passed if note.stable_key in release_problem_keys
        }
        specs_by_key = {spec.stable_key: spec for spec in note_specs.notes}
        for note in passed:
            if note.stable_key not in repaired_or_released_keys:
                continue
            spec = specs_by_key.get(note.stable_key)
            if spec is None:
                continue
            if spec.title != note.title:
                spec.title = note.title
                spec.question_answered = f"来源材料如何说明{note.title}？"
            spec.target_thesis = note.thesis

        passed_keys = {note.stable_key for note in passed}
        released_note_set = note_specs.model_copy(
            update={"notes": [spec for spec in note_specs.notes if spec.stable_key in passed_keys]}
        )
        source_note_outline = self.owner._prune_unbound_outline_leaves(
            source_note_outline, released_note_set
        )

        final_evaluations = list(final_evaluations_by_key.values())
        release_manifest = build_release_manifest(
            outline=source_note_outline,
            note_set=note_specs,
            notes=passed,
            evaluations=final_evaluations,
            failures=failures,
            review=release_review,
        )
        release_path = normalized_path.parent / "release-v1.json"
        self.owner._write_generation_artifact(release_path, release_manifest)
        generation_path = normalized_path.parent / "generation-v4.json"
        generation_payload = self.owner._read_generation_artifact(generation_path)
        generation_payload["note_set_plan"] = note_specs.model_dump()
        persisted_drafts = dict(generation_payload.get("drafts") or {})
        for note in passed:
            persisted_drafts[note.stable_key] = note.model_dump()
        generation_payload["drafts"] = persisted_drafts
        generation_payload["final_release"] = release_manifest
        generation_payload["evaluations"] = [item.model_dump() for item in final_evaluations]
        self.owner._write_generation_artifact(generation_path, generation_payload)
        with self.owner.db.session() as session:
            knowledge_map_record = session.get(KnowledgeMapRecord, knowledge_map_id)
            knowledge_map_record.summary = note_specs.document_summary

        self.owner._record_candidate_results(
            knowledge_map_id,
            passed=passed,
            failures=failures,
            repair_attempted_keys=repair_attempted_keys,
        )
        self.owner._record_quality_evaluations(
            knowledge_map_id,
            final_evaluations,
        )
        if not passed and plan.notes:
            raise RuntimeError("All planned notes failed local validation")
        activated = self.owner._persist_notes(job_id, source.id, source_version.id, passed)
        source_note_id = self.owner._persist_source_note(
            job_id=job_id,
            source_id=source.id,
            source_version_id=source_version.id,
            outline=source_note_outline,
            note_set=note_specs,
            passed_notes=passed,
        )
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            generation_warning_codes = {
                "CANDIDATE_VALIDATION_FAILED",
                "CANDIDATE_QUALITY_FAILED",
                "SOURCE_RELEASE_QUALITY_FAILED",
                "SOURCE_RELEASE_GLOBAL_WARNING",
                "SKIPPED_SECTION",
            }
            warnings = [
                item
                for item in _parse_json(job.warning_json, [])
                if item.get("code") not in generation_warning_codes
            ]
            warnings.extend(failures)
            warnings.extend(
                {
                    "code": "SKIPPED_SECTION",
                    "section": item.get("section"),
                    "detail": item.get("reason"),
                }
                for item in plan.skipped_sections
            )
            job.warning_json = _json(warnings)
            job.stage = JobStage.COMPLETED.value
            job.progress = 1.0
            job.next_attempt_at = None
            job.status = (
                JobStatus.COMPLETED_WITH_WARNINGS.value
                if _has_material_warnings(warnings)
                else JobStatus.COMPLETED.value
            )
            job.state_json = _json(
                {
                    **_parse_json(job.state_json, {}),
                    "activated_note_ids": activated,
                    "source_note_id": source_note_id,
                    "generation_schema": (
                        generation.schema_version if generation is not None else "legacy"
                    ),
                    "prompt_version": (
                        generation.prompt_version if generation is not None else "legacy"
                    ),
                    "planned_note_count": len(plan.notes),
                    "quality_failure_count": len(quality_failures),
                    "repaired_candidate_count": repaired_count,
                    "failed_candidate_count": len(failures),
                }
            )
        try:
            exports = self.owner.export_markdown(source_version.id)
        except Exception as exc:  # noqa: BLE001
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                warnings = _parse_json(job.warning_json, [])
                warnings.append(
                    {
                        "code": "MARKDOWN_EXPORT_FAILED",
                        "severity": "WARNING",
                        "detail": str(exc),
                    }
                )
                job.warning_json = _json(warnings)
                job.status = JobStatus.COMPLETED_WITH_WARNINGS.value
        else:
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                state = _parse_json(job.state_json, {})
                state["markdown_exports"] = exports
                job.state_json = _json(state)
        final_job = self.owner.get_job(job_id)
        self.owner.telemetry.finish_stage(
            job_id,
            status="COMPLETED",
            detail={"job_status": final_job["status"], "warning_count": len(final_job["warnings"])},
        )
