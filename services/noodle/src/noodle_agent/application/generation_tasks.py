from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..adapters.vision import resilient_vision_batch
from ..composition import Component
from ..db import (
    BlobObject,
    DocumentBlock,
    Job,
    SourceVersion,
    VisualAsset,
    VisualUnderstandingRecord,
)
from ..domain import (
    DocumentProfile,
    JobStage,
    JobStatus,
    KnowledgeMap,
    KnowledgePlan,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSpec,
    ParsedDocument,
    VisualUnderstanding,
)
from ..quality import (
    note_content_sha256,
)
from ..visual_routing import (
    batch_by_page_affinity,
    route_planned_visuals,
)
from .common import (
    GENERATION_SCHEMA_VERSION,
    _json,
    _parse_json,
    _sha256_text,
    _write_text_atomic,
)


class GenerationTasksModule(Component):
    def _update_generation_stage(
        self,
        job_id: str,
        stage: JobStage,
        progress: float,
        **state_updates: Any,
    ) -> None:
        should_transition = False
        effective_progress = progress
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            state = _parse_json(job.state_json, {})
            state.update(state_updates)
            job.state_json = _json(state)
            current_progress = float(job.progress or 0.0)
            effective_progress = max(current_progress, progress)
            should_transition = progress >= current_progress
            if should_transition:
                job.stage = stage.value
            job.status = JobStatus.RUNNING.value
            job.progress = effective_progress
            job.error_code = None
            job.error_detail = None
        # Parallel visual and drafting work may report completion out of order.
        # Ignore a late callback from an earlier phase instead of moving the
        # public timeline backwards or reopening an earlier stage.
        if should_transition:
            self.owner.telemetry.transition_stage(
                job_id, stage.value, effective_progress, state_updates
            )

    def _invoke_agent(
        self,
        job_id: str,
        stage: JobStage,
        method,
        *args,
        note_key: str | None = None,
        **kwargs,
    ):
        with self.owner.telemetry.bind(
            job_id=job_id,
            stage=stage.value,
            note_key=note_key,
        ):
            return method(*args, **kwargs)

    def _invoke_agent_with_task_retries(
        self,
        job_id: str,
        stage: JobStage,
        method,
        *args,
        note_key: str | None = None,
        **kwargs,
    ):
        """Retry high-value structured tasks that may return an empty body."""

        last_error: Exception | None = None
        for attempt in range(self.owner.settings.generation_task_retries + 1):
            try:
                return self.owner._invoke_agent(
                    job_id,
                    stage,
                    method,
                    *args,
                    note_key=note_key,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.owner.settings.generation_task_retries:
                    time.sleep(min(2.0**attempt, 3.0))
        raise RuntimeError(
            f"Agent task {note_key or method.__name__} failed after retries: {last_error}"
        ) from last_error

    @staticmethod
    def _read_generation_artifact(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"schema_version": GENERATION_SCHEMA_VERSION, "drafts": {}}
        return _parse_json(path.read_text(encoding="utf-8"), {})

    @staticmethod
    def _write_generation_artifact(path: Path, payload: dict[str, Any]) -> None:
        _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _analyze_planned_visuals(
        self,
        job_id: str,
        document: ParsedDocument,
        note_set: NoteSetPlan,
    ) -> dict[str, dict[str, Any]]:
        # A resumed visual stage must replace, rather than accumulate, warnings
        # from an earlier attempt. Current failures are appended below and are
        # preserved by final publication.
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.warning_json = _json(
                    [
                        item
                        for item in _parse_json(job.warning_json, [])
                        if item.get("code") != "VISION_ANALYSIS_FAILED"
                    ]
                )
        if not getattr(self.owner.vision, "enabled", False):
            return {}
        planned_ids = list(
            dict.fromkeys(block_id for spec in note_set.notes for block_id in spec.asset_block_ids)
        )
        blocks_by_id = {block.block_id: block for block in document.blocks}
        planned = [
            blocks_by_id[block_id]
            for block_id in planned_ids
            if block_id in blocks_by_id
            and blocks_by_id[block_id].asset_path
            and Path(str(blocks_by_id[block_id].asset_path)).is_file()
        ]
        if not planned:
            return {}

        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            rows = session.execute(
                select(DocumentBlock.external_block_id, VisualAsset.id)
                .join(VisualAsset, VisualAsset.document_block_id == DocumentBlock.id)
                .where(DocumentBlock.parse_run_id == job.parse_run_id)
            ).all()
            visual_ids = {external_id: visual_id for external_id, visual_id in rows}
            cached_rows = session.scalars(
                select(VisualUnderstandingRecord).where(
                    VisualUnderstandingRecord.model == self.owner.vision.model_name,
                    VisualUnderstandingRecord.prompt_version == self.owner.vision.prompt_version,
                    VisualUnderstandingRecord.status == "COMPLETED",
                )
            ).all()
            cached_by_id = {
                row.visual_asset_id: VisualUnderstanding.model_validate_json(row.result_json)
                for row in cached_rows
            }
            cached_by_hash = {
                row.image_sha256: VisualUnderstanding.model_validate_json(row.result_json)
                for row in cached_rows
            }

        results: dict[str, VisualUnderstanding] = {}
        pending: list[dict[str, Any]] = []
        block_positions = {block.block_id: index for index, block in enumerate(document.blocks)}
        routes = route_planned_visuals(document, planned)
        local_decorative_count = 0
        local_metadata_count = 0
        cached_count = 0
        seen_pending_hash: dict[str, str] = {}
        duplicate_aliases: dict[str, list[tuple[Any, str]]] = {}
        for block in planned:
            visual_id = visual_ids.get(block.block_id, "")
            if not visual_id:
                continue
            route = routes[block.block_id]
            image_sha256 = route.image_sha256
            cached_result = cached_by_id.get(visual_id) or cached_by_hash.get(image_sha256)
            if cached_result is not None:
                results[block.block_id] = cached_result.model_copy(
                    update={
                        "visual_asset_id": visual_id,
                        "external_block_id": block.block_id,
                        "image_sha256": image_sha256,
                    }
                )
                cached_count += 1
                continue
            if route.route == "LOCAL_DECORATIVE":
                results[block.block_id] = VisualUnderstanding(
                    visual_asset_id=visual_id,
                    external_block_id=block.block_id,
                    image_sha256=image_sha256,
                    model="local-visual-router",
                    prompt_version="local-visual-router-v1",
                    asset_type="DECORATIVE",
                    is_decorative=True,
                    caption="",
                    teaching_role="",
                    confidence=0.99,
                    analysis_tier="LOCAL_DECORATIVE",
                    route_reason=route.reason,
                    is_fragment=False,
                    group_id=route.affinity_group,
                    group_type=route.group_type,
                    related_asset_ids=list(route.related_block_ids),
                )
                local_decorative_count += 1
                continue
            if route.route == "LOCAL_METADATA":
                results[block.block_id] = VisualUnderstanding(
                    visual_asset_id=visual_id,
                    external_block_id=block.block_id,
                    image_sha256=image_sha256,
                    model="mineru-metadata",
                    prompt_version="local-visual-router-v1",
                    asset_type="OTHER",
                    is_decorative=False,
                    essential=False,
                    caption=route.source_caption,
                    teaching_role="来源已提供图片说明",
                    confidence=0.9,
                    analysis_tier="LOCAL_METADATA",
                    route_reason=route.reason,
                    is_fragment=False,
                    group_id=route.affinity_group,
                    group_type=route.group_type,
                    related_asset_ids=list(route.related_block_ids),
                )
                local_metadata_count += 1
                continue
            representative = seen_pending_hash.get(image_sha256)
            if representative:
                duplicate_aliases.setdefault(representative, []).append((block, visual_id))
                continue
            seen_pending_hash[image_sha256] = block.block_id
            position = block_positions[block.block_id]
            neighbors = document.blocks[max(0, position - 3) : position + 4]
            context = {
                "document_title": document.title,
                "page": block.page_label,
                "bbox": block.bbox,
                "affinity_group": route.affinity_group,
                "group_type": route.group_type,
                "related_asset_ids": list(route.related_block_ids),
                "same_page_assets": [
                    {"asset_id": item.block_id, "bbox": item.bbox}
                    for item in planned
                    if item.page_index == block.page_index
                ],
                "neighbor_text": [
                    {
                        "type": item.block_type,
                        "text": item.text[:600],
                    }
                    for item in neighbors
                    if item.text
                ][:4],
                "planned_notes": [
                    {"stable_key": spec.stable_key, "title": spec.title}
                    for spec in note_set.notes
                    if block.block_id in spec.asset_block_ids
                ],
            }
            pending.append({"block": block, "visual_asset_id": visual_id, "context": context})

        if pending:
            # Crops can omit panel labels. Supply a shared original-page view
            # when a selected page contains multiple visual fragments.
            from collections import Counter

            import pymupdf as fitz

            counts = Counter(item["block"].page_index for item in pending)
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                version = session.get(SourceVersion, job.source_version_id)
                blob = session.get(BlobObject, version.original_blob_id)
                pdf_path = self.owner.blobs.path_for(blob.storage_key)
                preview_dir = self.owner.settings.artifact_dir / job.parse_run_id / "vision-context"
            preview_dir.mkdir(parents=True, exist_ok=True)
            with fitz.open(pdf_path) as pdf:
                for page_index in sorted(counts):
                    if counts[page_index] < 2:
                        continue
                    preview = preview_dir / f"page-{page_index + 1}.png"
                    if not preview.exists():
                        pdf[page_index].get_pixmap(matrix=fitz.Matrix(1, 1)).save(preview)
                    for item in pending:
                        if item["block"].page_index == page_index:
                            item["page_context_path"] = preview
            batches = batch_by_page_affinity(pending, self.owner.settings.vision_batch_size)
            self.owner._update_generation_stage(
                job_id,
                JobStage.ANALYZING_VISUALS,
                0.63,
                generation_step="understand_planned_visuals",
                planned_visual_count=len(planned),
                cached_visual_count=cached_count,
                local_decorative_count=local_decorative_count,
                local_metadata_count=local_metadata_count,
                api_visual_count=len(pending),
                api_batch_count=len(batches),
            )

            def analyze_batch(batch: list[dict[str, Any]]):
                items = [
                    {
                        "image_path": Path(str(item["block"].asset_path)),
                        "external_block_id": item["block"].block_id,
                        "visual_asset_id": item["visual_asset_id"],
                        "context": item["context"],
                        "page_context_path": item.get("page_context_path"),
                    }
                    for item in batch
                ]

                def call(part):
                    with self.owner.telemetry.bind(
                        job_id=job_id,
                        stage=JobStage.ANALYZING_VISUALS.value,
                        note_key="+".join(item["external_block_id"] for item in part),
                    ):
                        return self.owner.vision.analyze_batch(part)

                return resilient_vision_batch(
                    items, call, max_retries=self.owner.settings.vision_max_retries
                )

            failures: list[dict[str, str]] = []
            with ThreadPoolExecutor(
                max_workers=min(self.owner.settings.vision_concurrency, len(batches)),
                thread_name_prefix="noodle-vision",
            ) as executor:
                future_map = {executor.submit(analyze_batch, batch): batch for batch in batches}
                for future in as_completed(future_map):
                    batch = future_map[future]
                    try:
                        batch_results, batch_failures = future.result()
                        failures.extend(batch_failures)
                    except Exception as exc:  # noqa: BLE001
                        failures.extend(
                            {
                                "block_id": item["block"].block_id,
                                "error": str(exc)[:500],
                            }
                            for item in batch
                        )
                        continue
                    expanded: list[VisualUnderstanding] = list(batch_results.values())
                    for understanding in list(expanded):
                        for alias_block, alias_visual_id in duplicate_aliases.get(
                            understanding.external_block_id, []
                        ):
                            expanded.append(
                                understanding.model_copy(
                                    update={
                                        "visual_asset_id": alias_visual_id,
                                        "external_block_id": alias_block.block_id,
                                        "route_reason": "exact-image duplicate reused in batch",
                                    }
                                )
                            )
                    for understanding in expanded:
                        results[understanding.external_block_id] = understanding
                        result_json = understanding.model_dump_json()
                        with self.owner.db.session() as session:
                            existing = session.scalar(
                                select(VisualUnderstandingRecord).where(
                                    VisualUnderstandingRecord.visual_asset_id
                                    == understanding.visual_asset_id,
                                    VisualUnderstandingRecord.model == understanding.model,
                                    VisualUnderstandingRecord.prompt_version
                                    == understanding.prompt_version,
                                )
                            )
                            if existing is None:
                                session.add(
                                    VisualUnderstandingRecord(
                                        visual_asset_id=understanding.visual_asset_id,
                                        image_sha256=understanding.image_sha256,
                                        model=understanding.model,
                                        prompt_version=understanding.prompt_version,
                                        result_json=result_json,
                                        result_sha256=_sha256_text(result_json),
                                        status="COMPLETED",
                                    )
                                )
            if failures:
                with self.owner.db.session() as session:
                    job = session.get(Job, job_id)
                    warnings = _parse_json(job.warning_json, [])
                    warnings.extend(
                        {
                            "code": "VISION_ANALYSIS_FAILED",
                            "asset": item["block_id"],
                            "detail": item["error"],
                        }
                        for item in failures
                    )
                    job.warning_json = _json(warnings)
        return {key: item.model_dump() for key, item in results.items()}

    def _draft_specs_concurrently(
        self,
        *,
        job_id: str,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        specs: list[NoteSpec],
        vision_context: dict[str, dict[str, Any]],
        artifact_path: Path,
        payload: dict[str, Any],
        drafts: dict[str, Any],
    ) -> dict[str, Any]:
        """Draft a bounded subset; callers may overlap text-only work with vision."""

        if not specs:
            return drafts
        total = max(1, len(note_set.notes))
        self.owner._update_generation_stage(
            job_id,
            JobStage.DRAFTING_NOTES,
            0.66,
            generation_artifact=str(artifact_path),
            generation_step="draft_notes_concurrently",
            drafted_note_count=len(drafts),
            planned_note_count=len(note_set.notes),
            draft_concurrency=min(self.owner.settings.draft_concurrency, len(specs)),
        )

        def draft_one(spec: NoteSpec) -> NoteDraft:
            last_error: Exception | None = None
            for attempt in range(self.owner.settings.generation_task_retries + 1):
                try:
                    return self.owner._invoke_agent(
                        job_id,
                        JobStage.DRAFTING_NOTES,
                        self.owner.agent.draft_note,
                        document,
                        profile,
                        knowledge_map,
                        note_set,
                        spec,
                        vision_context=vision_context,
                        note_key=spec.stable_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < self.owner.settings.generation_task_retries:
                        time.sleep(min(2.0**attempt, 3.0))
            raise RuntimeError(
                f"Draft {spec.stable_key} failed after retries: {last_error}"
            ) from last_error

        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.owner.settings.draft_concurrency, len(specs)),
            thread_name_prefix="noodle-draft",
        ) as executor:
            future_map = {executor.submit(draft_one, spec): spec for spec in specs}
            for future in as_completed(future_map):
                spec = future_map[future]
                try:
                    draft = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append({"stable_key": spec.stable_key, "error": str(exc)[:500]})
                    continue
                drafts[spec.stable_key] = draft.model_dump()
                payload["drafts"] = drafts
                self.owner._write_generation_artifact(artifact_path, payload)
                self.owner._update_generation_stage(
                    job_id,
                    JobStage.DRAFTING_NOTES,
                    0.66 + 0.16 * len(drafts) / total,
                    generation_artifact=str(artifact_path),
                    generation_step="draft_notes_concurrently",
                    current_note_key=spec.stable_key,
                    drafted_note_count=len(drafts),
                    planned_note_count=len(note_set.notes),
                )
        if failures:
            raise RuntimeError(
                "Draft failures after retries: "
                + "; ".join(f"{item['stable_key']}={item['error']}" for item in failures)
            )
        return drafts

    def _evaluate_notes_batched(
        self,
        job_id: str,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        """Judge small, bounded batches without changing final note order."""
        specs_by_key = {item.stable_key: item for item in note_set.notes}
        notes = list(plan.notes)
        batch_size = self.owner.settings.judge_batch_size
        complex_kinds = {"FORMULA", "COMPARISON", "CASE", "PROCEDURE", "EXAMPLE"}

        def is_complex(note: NoteDraft) -> bool:
            spec = specs_by_key.get(note.stable_key)
            return bool(
                spec
                and (
                    spec.note_kind.upper() in complex_kinds
                    or spec.asset_block_ids
                    or len(spec.concept_keys) >= 3
                    or len(spec.evidence_block_ids) >= 10
                )
            )

        simple_notes = [note for note in notes if not is_complex(note)]
        complex_notes = [note for note in notes if is_complex(note)]
        batches = [
            simple_notes[index : index + batch_size]
            for index in range(0, len(simple_notes), batch_size)
        ]
        # Dense evidence and vision catalogs make cross-note omissions easy to
        # hallucinate. Complex notes are cheap to judge independently once
        # DeepSeek thinking is disabled, and can still run concurrently.
        batches.extend([[note] for note in complex_notes])
        if not batches:
            return []

        def judge_batch(batch_index: int, batch_notes: list[NoteDraft]):
            batch_keys = {item.stable_key for item in batch_notes}
            batch_specs = [specs_by_key[key] for key in batch_keys if key in specs_by_key]
            concept_keys = {
                concept_key for spec in batch_specs for concept_key in spec.concept_keys
            }
            batch_map = KnowledgeMap(
                document_summary=knowledge_map.document_summary,
                concepts=[
                    concept
                    for concept in knowledge_map.concepts
                    if concept.stable_key in concept_keys
                ],
                skipped_sections=[],
            )
            batch_set = NoteSetPlan(
                document_summary=note_set.document_summary,
                notes=batch_specs,
            )
            batch_plan = KnowledgePlan(
                document_summary=plan.document_summary,
                notes=batch_notes,
            )
            last_error: Exception | None = None
            for attempt in range(self.owner.settings.generation_task_retries + 1):
                try:
                    return self.owner._invoke_agent(
                        job_id,
                        JobStage.EVALUATING_NOTES,
                        self.owner.agent.evaluate_plan,
                        document,
                        batch_set,
                        batch_plan,
                        batch_map,
                        vision_context,
                        note_key=f"judge-batch-{batch_index + 1}",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < self.owner.settings.generation_task_retries:
                        time.sleep(min(2.0**attempt, 3.0))
            raise RuntimeError(
                f"Judge batch {batch_index + 1} failed after retries: {last_error}"
            ) from last_error

        by_key: dict[str, NoteEvaluation] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.owner.settings.judge_concurrency, len(batches)),
            thread_name_prefix="noodle-judge",
        ) as executor:
            futures = {
                executor.submit(judge_batch, index, batch): index
                for index, batch in enumerate(batches)
            }
            failures: list[str] = []
            for completed, future in enumerate(as_completed(futures), start=1):
                batch_index = futures[future]
                try:
                    batch_evaluations = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"batch-{batch_index + 1}: {exc}")
                    batch_evaluations = []
                for evaluation in batch_evaluations:
                    by_key[evaluation.stable_key] = evaluation
                self.owner._update_generation_stage(
                    job_id,
                    JobStage.EVALUATING_NOTES,
                    0.84 + 0.08 * completed / len(batches),
                    generation_step="evaluate_note_batches",
                    completed_judge_batches=completed,
                    judge_batch_count=len(batches),
                )
        if failures:
            raise RuntimeError("; ".join(failures))
        return [
            by_key.get(
                note.stable_key,
                NoteEvaluation(
                    stable_key=note.stable_key,
                    passed=False,
                    issues=["JUDGE_MISSING_RESULT: 审校批次没有返回该候选"],
                    content_sha256=note_content_sha256(note),
                    evaluator_model=str(
                        getattr(self.owner.agent, "pro_model", self.owner.agent.name)
                    ),
                    prompt_version="note-judge-v12",
                ),
            )
            for note in notes
        ]
