from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..composition import Component
from ..db import (
    AuditEvent,
    Job,
    KnowledgeMapRecord,
    LlmCallEvent,
    ParseRun,
    Source,
    SourceNote,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    SourceVersion,
    StageEvent,
    VisionCallEvent,
    utcnow,
)
from ..domain import (
    JobStage,
    JobStatus,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    ParsedDocument,
    SourceNoteOutlinePlan,
)
from ..release import apply_outline_title_patches, build_release_manifest
from .common import (
    GENERATION_SCHEMA_VERSION,
    TERMINAL_STATUSES,
    _json,
    _merged_event_duration_ms,
    _normalize_warnings,
    _parse_json,
    _public_job_status,
    _utc_iso,
)


class JobsModule(Component):
    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            return self.owner._job_dict(job)

    def get_job_result(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            warnings = _normalize_warnings(_parse_json(job.warning_json, []))
            public_status = _public_job_status(job.status, warnings)
            payload: dict[str, Any] = {
                "job_id": job.id,
                "status": public_status,
                "stage": job.stage,
                "ready": public_status
                in {
                    JobStatus.COMPLETED.value,
                    JobStatus.COMPLETED_WITH_WARNINGS.value,
                },
                "source_version_id": job.source_version_id,
                "warnings": warnings,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
            }
            source_version = session.get(SourceVersion, job.source_version_id)
            if source_version is None:
                return payload
            payload["source_id"] = source_version.source_id
            row = session.execute(
                select(SourceNote, SourceNoteVersion)
                .join(SourceNoteVersion, SourceNoteVersion.source_note_id == SourceNote.id)
                .where(
                    SourceNote.source_id == source_version.source_id,
                    SourceNote.active_version_id == SourceNoteVersion.id,
                    SourceNoteVersion.source_version_id == source_version.id,
                )
            ).first()
            if row is None:
                return payload
            source_note, source_note_version = row
            payload.update(
                {
                    "source_note_id": source_note.id,
                    "source_note_version_id": source_note_version.id,
                    "title": source_note_version.title,
                    "note_count": session.scalar(
                        select(func.count(SourceNoteUnitPlacement.id)).where(
                            SourceNoteUnitPlacement.source_note_version_id == source_note_version.id
                        )
                    )
                    or 0,
                }
            )
            return payload

    def review_existing_release(self, job_id: str) -> dict[str, Any]:
        """Re-run only Source-level editorial review on the canonical release."""

        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
        release_path = normalized_path.parent / "release-v1.json"
        generation_path = normalized_path.parent / "generation-v4.json"
        if not normalized_path.is_file() or not release_path.is_file():
            raise ValueError("Canonical release is unavailable")
        manifest = self.owner._read_generation_artifact(release_path)
        outline = SourceNoteOutlinePlan.model_validate(manifest["outline"])
        note_set = NoteSetPlan.model_validate(manifest["note_set_plan"])
        notes = [
            NoteDraft.model_validate(
                {key: value for key, value in raw.items() if key != "content_sha256"}
            )
            for raw in manifest.get("notes", [])
        ]
        evaluations = [
            NoteEvaluation.model_validate(raw) for raw in manifest.get("evaluations", [])
        ]
        review_release = getattr(self.owner.agent, "review_source_release", None)
        if not callable(review_release):
            raise TypeError("The active knowledge agent cannot review Source releases")
        document = ParsedDocument.model_validate_json(normalized_path.read_text(encoding="utf-8"))
        review = self.owner._invoke_agent(
            job_id,
            JobStage.VALIDATING_NOTES,
            review_release,
            document,
            outline,
            note_set,
            notes,
            note_key="source-release-review-only",
        )
        if review.note_issues or review.global_issues:
            return {
                "job_id": job_id,
                "source_note_id": None,
                "review": review.model_dump(),
                "exports": [],
                "requires_validation_restart": True,
            }
        outline = apply_outline_title_patches(outline, review)
        updated = build_release_manifest(
            outline=outline,
            note_set=note_set,
            notes=notes,
            evaluations=evaluations,
            failures=list(manifest.get("rejected") or []),
            review=review,
        )
        self.owner._write_generation_artifact(release_path, updated)
        generation_payload = self.owner._read_generation_artifact(generation_path)
        generation_payload["final_release"] = updated
        self.owner._write_generation_artifact(generation_path, generation_payload)
        with self.owner.db.session() as session:
            knowledge_map = session.scalar(
                select(KnowledgeMapRecord).where(
                    KnowledgeMapRecord.source_version_id == source_version.id,
                    KnowledgeMapRecord.parse_run_id == job.parse_run_id,
                )
            )
            if knowledge_map is not None:
                knowledge_map.map_json = _json(updated)
                knowledge_map.summary = outline.summary
        source_note_id = self.owner._persist_source_note(
            job_id=job_id,
            source_id=source.id,
            source_version_id=source_version.id,
            outline=outline,
            note_set=note_set,
            passed_notes=notes,
        )
        exports = self.owner.export_markdown(source_version.id)
        return {
            "job_id": job_id,
            "source_note_id": source_note_id,
            "review": review.model_dump(),
            "exports": exports,
        }

    def list_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            jobs = session.scalars(
                select(Job).order_by(Job.updated_at.desc()).limit(max(1, min(limit, 50)))
            ).all()
            return [self.owner._job_dict(job) for job in jobs]

    def get_job_timeline(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            stages = session.scalars(
                select(StageEvent)
                .where(StageEvent.job_id == job_id)
                .order_by(StageEvent.started_at)
            ).all()
            llm_calls = session.scalars(
                select(LlmCallEvent)
                .where(LlmCallEvent.job_id == job_id)
                .order_by(LlmCallEvent.started_at)
            ).all()
            vision_calls = session.scalars(
                select(VisionCallEvent)
                .where(VisionCallEvent.job_id == job_id)
                .order_by(VisionCallEvent.started_at)
            ).all()

            def stamp(value):
                return _utc_iso(value)

            model_calls = [*llm_calls, *vision_calls]
            model_cumulative_duration_ms = sum(item.duration_ms or 0 for item in model_calls)

            return {
                "job": self.owner._job_dict(job),
                "summary": {
                    "stage_count": len(stages),
                    "llm_call_count": len(llm_calls),
                    "vision_call_count": len(vision_calls),
                    "llm_duration_ms": sum(item.duration_ms or 0 for item in llm_calls),
                    "vision_duration_ms": sum(item.duration_ms or 0 for item in vision_calls),
                    "model_cumulative_duration_ms": model_cumulative_duration_ms,
                    "model_wall_duration_ms": _merged_event_duration_ms(model_calls),
                    "total_tokens": sum(item.total_tokens or 0 for item in llm_calls)
                    + sum(item.total_tokens or 0 for item in vision_calls),
                },
                "stages": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "status": item.status,
                        "progress": item.progress,
                        "detail": _parse_json(item.detail_json, {}),
                        "started_at": stamp(item.started_at),
                        "completed_at": stamp(item.completed_at),
                        "duration_ms": item.duration_ms,
                    }
                    for item in stages
                ],
                "llm_calls": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "note_key": item.note_key,
                        "operation": item.operation,
                        "model": item.model,
                        "prompt_version": item.prompt_version,
                        "status": item.status,
                        "duration_ms": item.duration_ms,
                        "input_sha256": item.input_sha256,
                        "output_sha256": item.output_sha256,
                        "input_chars": item.input_chars,
                        "output_chars": item.output_chars,
                        "prompt_tokens": item.prompt_tokens,
                        "completion_tokens": item.completion_tokens,
                        "total_tokens": item.total_tokens,
                        "response_id": item.response_id,
                        "error_code": item.error_code,
                        "started_at": stamp(item.started_at),
                    }
                    for item in llm_calls
                ],
                "vision_calls": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "note_key": item.note_key,
                        "visual_asset_id": item.visual_asset_id,
                        "operation": item.operation,
                        "model": item.model,
                        "status": item.status,
                        "duration_ms": item.duration_ms,
                        "image_sha256": item.image_sha256,
                        "total_tokens": item.total_tokens,
                        "response_id": item.response_id,
                        "error_code": item.error_code,
                        "started_at": stamp(item.started_at),
                    }
                    for item in vision_calls
                ],
            }

    def resume_job(self, job_id: str) -> dict[str, Any]:
        resumable_stages = {
            JobStage.MAPPING.value,
            JobStage.GENERATING.value,
            JobStage.PROFILING_DOCUMENT.value,
            JobStage.BUILDING_KNOWLEDGE_MAP.value,
            JobStage.PLANNING_SOURCE_NOTE_OUTLINE.value,
            JobStage.PLANNING_NOTE_SET.value,
            JobStage.REVIEWING_NOTE_SET.value,
            JobStage.REPAIRING_NOTE_SET.value,
            JobStage.ANALYZING_VISUALS.value,
            JobStage.DRAFTING_NOTES.value,
            JobStage.EVALUATING_NOTES.value,
            JobStage.VALIDATING_NOTES.value,
        }
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            if job.status != JobStatus.FAILED.value:
                raise ValueError(f"Only FAILED jobs can be resumed; current status is {job.status}")
            parse_run = session.get(ParseRun, job.parse_run_id) if job.parse_run_id else None
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            can_resume_checkpoint = (
                job.stage not in resumable_stages
                or parse_run is None
                or parse_run.status != "SUCCEEDED"
                or not normalized_path.is_file()
            ) is False
            if not can_resume_checkpoint:
                replacement = Job(source_version_id=job.source_version_id)
                session.add(replacement)
                session.flush()
                session.add(
                    AuditEvent(
                        actor="local-user",
                        action="RETRY_JOB_FROM_ORIGINAL",
                        risk_level="L1",
                        target_type="Job",
                        target_id=replacement.id,
                        detail_json=_json(
                            {
                                "failed_job_id": job.id,
                                "reason": "no_safe_parse_checkpoint",
                            }
                        ),
                    )
                )
                session.flush()
                payload = self.owner._job_dict(replacement)
                payload["retry_mode"] = "from_original"
                payload["replaces_job_id"] = job.id
                return payload
            job.status = JobStatus.RUNNING.value
            job.error_code = None
            job.error_detail = None
            job.next_attempt_at = utcnow()
            session.add(
                AuditEvent(
                    actor="local-user",
                    action="RESUME_JOB",
                    risk_level="L1",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json({"stage": job.stage}),
                )
            )
            session.flush()
            payload = self.owner._job_dict(job)
            payload["retry_mode"] = "checkpoint"
            payload["resumed_from_stage"] = job.stage
            return payload

    def restart_generation(
        self,
        job_id: str,
        from_step: str,
        *,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        keep_keys = {
            "profile": {"schema_version"},
            "knowledge-map": {"schema_version", "profile"},
            "outline": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
            },
            "note-set": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "source_note_outline",
            },
            "vision": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
            },
            "drafts": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
            },
            "evaluation": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
                "drafts",
            },
            "validation": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
                "drafts",
                "evaluations",
                "note_boundary_changes",
                "note_asset_reclassifications",
                "visual_sequence_additions",
            },
        }
        if from_step not in keep_keys:
            raise ValueError(f"Unknown generation restart step: {from_step}")
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            restartable_statuses = {
                JobStatus.FAILED.value,
                JobStatus.COMPLETED.value,
                JobStatus.COMPLETED_WITH_WARNINGS.value,
            }
            if job.status not in restartable_statuses:
                raise ValueError(
                    "Generation can only be restarted from a terminal generated job; "
                    f"current status is {job.status}"
                )
            parse_run = session.get(ParseRun, job.parse_run_id) if job.parse_run_id else None
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            artifact_path = normalized_path.parent / "generation-v4.json"
            if (
                parse_run is None
                or parse_run.status != "SUCCEEDED"
                or not normalized_path.is_file()
                or not artifact_path.is_file()
            ):
                raise ValueError("Generation artifact is unavailable")
            payload = self.owner._read_generation_artifact(artifact_path)
            retained = {key: value for key, value in payload.items() if key in keep_keys[from_step]}
            for contract_key in (
                "knowledge_map_contract_version",
                "planning_contract_version",
                "writing_contract_version",
                "judge_contract_version",
            ):
                if contract_key in payload:
                    retained[contract_key] = payload[contract_key]
            retained["schema_version"] = GENERATION_SCHEMA_VERSION
            retained.setdefault("drafts", {})
            self.owner._write_generation_artifact(artifact_path, retained)
            stage_by_step = {
                "profile": JobStage.PROFILING_DOCUMENT,
                "knowledge-map": JobStage.BUILDING_KNOWLEDGE_MAP,
                "outline": JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                "note-set": JobStage.PLANNING_NOTE_SET,
                "vision": JobStage.ANALYZING_VISUALS,
                "drafts": JobStage.DRAFTING_NOTES,
                "evaluation": JobStage.EVALUATING_NOTES,
                "validation": JobStage.VALIDATING_NOTES,
            }
            job.stage = stage_by_step[from_step].value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.46
            job.error_code = None
            job.error_detail = None
            job.next_attempt_at = utcnow()
            for key in (
                "generation_step",
                "current_note_key",
                "current_window_key",
                "drafted_note_count",
                "completed_window_count",
            ):
                state.pop(key, None)
            state["generation_artifact"] = str(artifact_path)
            state["restart_from"] = from_step
            job.state_json = _json(state)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="RESTART_GENERATION",
                    risk_level="L1",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json({"from_step": from_step}),
                )
            )
            session.flush()
            return self.owner._job_dict(job)

    @staticmethod
    def _job_dict(job: Job) -> dict[str, Any]:
        state = _parse_json(job.state_json, {})
        warnings = _normalize_warnings(_parse_json(job.warning_json, []))
        end = job.updated_at if job.status in TERMINAL_STATUSES else utcnow()
        return {
            "id": job.id,
            "source_version_id": job.source_version_id,
            "parse_run_id": job.parse_run_id,
            "status": _public_job_status(job.status, warnings),
            "stage": job.stage,
            "progress": job.progress,
            "warnings": warnings,
            "error_code": job.error_code,
            "error_detail": job.error_detail,
            "next_attempt_at": _utc_iso(job.next_attempt_at),
            "created_at": _utc_iso(job.created_at),
            "updated_at": _utc_iso(job.updated_at),
            "duration_ms": max(0, int((end - job.created_at).total_seconds() * 1000)),
            "harness": state.get("harness", {}),
        }

    def link_job_to_harness(
        self,
        job_id: str,
        *,
        session_id: str | None,
        tool_call_id: str | None,
    ) -> None:
        """Persist the originating Harness identities without coupling schemas."""
        if not session_id and not tool_call_id:
            return
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            state = _parse_json(job.state_json, {})
            state["harness"] = {
                "session_id": session_id or "",
                "tool_call_id": tool_call_id or "",
                "linked_at": _utc_iso(utcnow()),
            }
            job.state_json = _json(state)
            session.add(
                AuditEvent(
                    actor=(f"harness-session:{session_id}" if session_id else "harness"),
                    action="LINK_HARNESS_JOB",
                    risk_level="L0",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json(state["harness"]),
                )
            )

    def run_job_once(self, job_id: str, *, force_poll: bool = False) -> dict[str, Any]:
        try:
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise ValueError(f"Unknown job_id: {job_id}")
                if job.status in TERMINAL_STATUSES:
                    return self.owner._job_dict(job)
                if (
                    job.status == JobStatus.WAITING_EXTERNAL.value
                    and job.next_attempt_at
                    and job.next_attempt_at > utcnow()
                    and not force_poll
                ):
                    return self.owner._job_dict(job)
                stage = job.stage

            if stage == JobStage.CREATED.value:
                self.owner._submit_parse(job_id)
            self.owner._poll_or_normalize(job_id)
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                generation_stages = {
                    JobStage.MAPPING.value,
                    JobStage.GENERATING.value,
                    JobStage.PROFILING_DOCUMENT.value,
                    JobStage.BUILDING_KNOWLEDGE_MAP.value,
                    JobStage.PLANNING_SOURCE_NOTE_OUTLINE.value,
                    JobStage.PLANNING_NOTE_SET.value,
                    JobStage.REVIEWING_NOTE_SET.value,
                    JobStage.REPAIRING_NOTE_SET.value,
                    JobStage.ANALYZING_VISUALS.value,
                    JobStage.DRAFTING_NOTES.value,
                    JobStage.EVALUATING_NOTES.value,
                    JobStage.VALIDATING_NOTES.value,
                }
                if job is not None and job.stage in generation_stages:
                    pass
                else:
                    return self.owner._job_dict(job)
            self.owner._generate_and_activate(job_id)
        # The persisted job boundary must convert every adapter/domain failure
        # into a diagnosable terminal state instead of losing the exception.
        except Exception as exc:  # noqa: BLE001
            self.owner._fail_job(job_id, exc)
        return self.owner.get_job(job_id)

    def run_job_until_terminal(
        self, job_id: str, *, timeout_seconds: float = 1800.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self.owner.run_job_once(job_id, force_poll=True)
            if result["status"] in TERMINAL_STATUSES:
                return result
            wait_for = max(0.2, self.owner.settings.mineru_poll_seconds)
            time.sleep(wait_for)
        raise TimeoutError(f"Job {job_id} did not finish in {timeout_seconds} seconds")

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = JobStatus.FAILED.value
            job.error_code = exc.__class__.__name__
            job.error_detail = str(exc)
            job.attempt_count += 1
            if job.parse_run_id:
                parse_run = session.get(ParseRun, job.parse_run_id)
                if parse_run and parse_run.status != "SUCCEEDED":
                    parse_run.status = "FAILED"
                    parse_run.error_code = exc.__class__.__name__
                    parse_run.error_detail = str(exc)
        self.owner.telemetry.finish_stage(
            job_id,
            status="FAILED",
            detail={"error_code": exc.__class__.__name__, "error_detail": str(exc)[:2_000]},
        )
