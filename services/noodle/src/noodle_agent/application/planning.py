from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..composition import Component
from ..domain import (
    DocumentProfile,
    GenerationBundle,
    JobStage,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteDraft,
    NoteEvaluation,
    ParsedDocument,
    SourceNotePlanningBundle,
)
from ..planning import normalize_note_set_assets
from ..quality import (
    note_content_sha256,
)
from ..visual_routing import (
    enrich_planned_visual_sequences,
)
from .common import (
    GENERATION_SCHEMA_VERSION,
    JUDGE_CONTRACT_VERSION,
    KNOWLEDGE_MAP_CONTRACT_VERSION,
    PLANNING_CONTRACT_VERSION,
    WRITING_CONTRACT_VERSION,
)


class PlanningModule(Component):
    def _build_generation_resumable(
        self,
        job_id: str,
        document: ParsedDocument,
        normalized_path: Path,
    ) -> GenerationBundle:
        artifact_path = normalized_path.parent / "generation-v4.json"
        payload = self.owner._read_generation_artifact(artifact_path)

        knowledge_dependents = {
            "local_concepts",
            "knowledge_skipped",
            "knowledge_map",
            "planning_bundle",
            "source_note_outline",
            "initial_note_set_plan",
            "initial_note_set_review",
            "note_set_plan",
            "note_set_review",
            "note_set_repaired",
            "note_boundary_changes",
            "note_asset_reclassifications",
            "visual_sequence_additions",
            "visual_understandings",
            "visual_analysis_complete",
            "drafts",
            "evaluations",
            "final_release",
        }
        planning_dependents = {
            "planning_bundle",
            "source_note_outline",
            "initial_note_set_plan",
            "initial_note_set_review",
            "note_set_plan",
            "note_set_review",
            "note_set_repaired",
            "note_boundary_changes",
            "note_asset_reclassifications",
            "visual_sequence_additions",
            "visual_understandings",
            "visual_analysis_complete",
            "drafts",
            "evaluations",
            "final_release",
        }
        writing_dependents = {"drafts", "evaluations", "final_release"}
        contracts_changed = False
        if payload.get("knowledge_map_contract_version") != KNOWLEDGE_MAP_CONTRACT_VERSION:
            for key in knowledge_dependents:
                payload.pop(key, None)
            payload["knowledge_map_contract_version"] = KNOWLEDGE_MAP_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("planning_contract_version") != PLANNING_CONTRACT_VERSION:
            for key in planning_dependents:
                payload.pop(key, None)
            payload["planning_contract_version"] = PLANNING_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("writing_contract_version") != WRITING_CONTRACT_VERSION:
            for key in writing_dependents:
                payload.pop(key, None)
            payload["writing_contract_version"] = WRITING_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("judge_contract_version") != JUDGE_CONTRACT_VERSION:
            payload.pop("evaluations", None)
            payload.pop("final_release", None)
            payload["judge_contract_version"] = JUDGE_CONTRACT_VERSION
            contracts_changed = True
        payload["schema_version"] = GENERATION_SCHEMA_VERSION
        if contracts_changed:
            self.owner._write_generation_artifact(artifact_path, payload)

        if not payload.get("profile"):
            self.owner._update_generation_stage(
                job_id,
                JobStage.PROFILING_DOCUMENT,
                0.46,
                generation_artifact=str(artifact_path),
                generation_step="profile_document",
            )
            profile = self.owner._invoke_agent(
                job_id, JobStage.PROFILING_DOCUMENT, self.owner.agent.profile_document, document
            )
            payload["profile"] = profile.model_dump()
            self.owner._write_generation_artifact(artifact_path, payload)
        else:
            profile = DocumentProfile.model_validate(payload["profile"])

        if not payload.get("knowledge_map"):
            window_builder = getattr(self.owner.agent, "knowledge_windows", None)
            window_extractor = getattr(self.owner.agent, "extract_knowledge_window", None)
            map_merger = getattr(self.owner.agent, "merge_knowledge_map", None)
            if all(callable(item) for item in (window_builder, window_extractor, map_merger)):
                windows = [
                    KnowledgeWindow.model_validate(item)
                    for item in (
                        payload.get("knowledge_windows")
                        or [item.model_dump() for item in window_builder(document, profile)]
                    )
                ]
                payload["knowledge_windows"] = [item.model_dump() for item in windows]
                local_by_window = dict(payload.get("local_concepts") or {})
                skipped = list(payload.get("knowledge_skipped") or [])
                total_windows = max(1, len(windows))
                pending_windows = [
                    window for window in windows if window.stable_key not in local_by_window
                ]

                def extract_one(window: KnowledgeWindow):
                    last_error: Exception | None = None
                    for attempt in range(self.owner.settings.generation_task_retries + 1):
                        try:
                            return self.owner._invoke_agent(
                                job_id,
                                JobStage.BUILDING_KNOWLEDGE_MAP,
                                window_extractor,
                                document,
                                profile,
                                window,
                                note_key=window.stable_key,
                            )
                        except Exception as exc:  # noqa: BLE001
                            last_error = exc
                            if attempt < self.owner.settings.generation_task_retries:
                                time.sleep(min(2.0**attempt, 8.0))
                    raise RuntimeError(
                        f"Knowledge window {window.stable_key} failed after retries: {last_error}"
                    ) from last_error

                if pending_windows:
                    self.owner._update_generation_stage(
                        job_id,
                        JobStage.BUILDING_KNOWLEDGE_MAP,
                        0.47,
                        generation_artifact=str(artifact_path),
                        generation_step="extract_knowledge_windows_concurrently",
                        completed_window_count=len(local_by_window),
                        knowledge_window_count=len(windows),
                        knowledge_concurrency=min(
                            self.owner.settings.knowledge_concurrency, len(pending_windows)
                        ),
                    )
                    with ThreadPoolExecutor(
                        max_workers=min(
                            self.owner.settings.knowledge_concurrency, len(pending_windows)
                        ),
                        thread_name_prefix="noodle-knowledge",
                    ) as executor:
                        futures = {
                            executor.submit(extract_one, window): window
                            for window in pending_windows
                        }
                        extraction_failures: list[str] = []
                        for future in as_completed(futures):
                            window = futures[future]
                            try:
                                concepts, skip_reason = future.result()
                            except Exception as exc:  # noqa: BLE001
                                extraction_failures.append(f"{window.stable_key}: {str(exc)[:500]}")
                                continue
                            local_by_window[window.stable_key] = [
                                item.model_dump() for item in concepts
                            ]
                            if not concepts:
                                skipped.append(
                                    {
                                        "section": window.segment_title,
                                        "reason": str(skip_reason or "LOW_INFORMATION"),
                                    }
                                )
                            payload["local_concepts"] = local_by_window
                            payload["knowledge_skipped"] = skipped
                            self.owner._write_generation_artifact(artifact_path, payload)
                            self.owner._update_generation_stage(
                                job_id,
                                JobStage.BUILDING_KNOWLEDGE_MAP,
                                0.47 + 0.07 * len(local_by_window) / total_windows,
                                generation_artifact=str(artifact_path),
                                generation_step="extract_knowledge_windows_concurrently",
                                current_window_key=window.stable_key,
                                completed_window_count=len(local_by_window),
                                knowledge_window_count=len(windows),
                            )
                    if extraction_failures:
                        raise RuntimeError(
                            "Knowledge extraction failures: " + "; ".join(extraction_failures)
                        )
                self.owner._update_generation_stage(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    0.55,
                    generation_artifact=str(artifact_path),
                    generation_step="materialize_candidate_map",
                    current_window_key=None,
                )
                local_concepts = [
                    KnowledgeConceptCandidate.model_validate(item)
                    for window in windows
                    for item in local_by_window.get(window.stable_key, [])
                ]
                knowledge_map = map_merger(document, profile, local_concepts, skipped)
            else:
                self.owner._update_generation_stage(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    0.5,
                    generation_artifact=str(artifact_path),
                    generation_step="build_knowledge_map",
                )
                knowledge_map = self.owner._invoke_agent(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    self.owner.agent.build_knowledge_map,
                    document,
                    profile,
                )
            payload["knowledge_map"] = knowledge_map.model_dump()
            self.owner._write_generation_artifact(artifact_path, payload)
        else:
            knowledge_map = KnowledgeMap.model_validate(payload["knowledge_map"])

        if not payload.get("planning_bundle"):
            self.owner._update_generation_stage(
                job_id,
                JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                0.56,
                generation_artifact=str(artifact_path),
                generation_step="plan_source_v2",
            )
            planning_bundle = self.owner._invoke_agent_with_task_retries(
                job_id,
                JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                self.owner.agent.plan_source_note_bundle,
                document,
                profile,
                knowledge_map,
                note_key="source-note-planning",
            )
            source_note_outline = planning_bundle.source_note_outline
            initial_note_set = planning_bundle.note_set_plan
            payload["planning_bundle"] = planning_bundle.model_dump()
            payload["source_note_outline"] = source_note_outline.model_dump()
            payload["initial_note_set_plan"] = initial_note_set.model_dump()
            self.owner._write_generation_artifact(artifact_path, payload)
        else:
            planning_bundle = SourceNotePlanningBundle.model_validate(payload["planning_bundle"])
            source_note_outline = planning_bundle.source_note_outline
            initial_note_set = planning_bundle.note_set_plan

        source_note_outline, initial_note_set, outline_depth_repairs = (
            self.owner._normalize_minimum_outline_depth(
                source_note_outline,
                initial_note_set,
            )
        )
        planning_bundle.source_note_outline = source_note_outline
        planning_bundle.note_set_plan = initial_note_set
        payload["planning_bundle"] = planning_bundle.model_dump()
        payload["source_note_outline"] = source_note_outline.model_dump()
        payload["initial_note_set_plan"] = initial_note_set.model_dump()
        if outline_depth_repairs:
            payload["deterministic_outline_depth_repairs"] = outline_depth_repairs
        self.owner._write_generation_artifact(artifact_path, payload)

        # A knowledge unit may now contain several tightly related concepts.
        # Do not automatically split HISTORY from its parent topic; the combined
        # planner owns semantic grouping. Only deterministic asset corrections
        # and missing visual-sequence recovery remain mandatory.
        note_set = initial_note_set
        note_set, asset_reclassifications = normalize_note_set_assets(document, note_set)
        if asset_reclassifications:
            payload["note_set_plan"] = note_set.model_dump()
            payload["note_asset_reclassifications"] = asset_reclassifications
            payload.pop("evaluations", None)
            self.owner._write_generation_artifact(artifact_path, payload)
        note_set, visual_additions = enrich_planned_visual_sequences(document, note_set)
        if visual_additions:
            concepts_by_key = {concept.stable_key: concept for concept in knowledge_map.concepts}
            specs_by_key = {spec.stable_key: spec for spec in note_set.notes}
            for note_key, added_ids in visual_additions.items():
                spec = specs_by_key.get(note_key)
                if spec is None or not spec.concept_keys:
                    continue
                added = set(added_ids)
                planned_anchors = set(spec.asset_block_ids) - added
                owners = [
                    concepts_by_key[key]
                    for key in spec.concept_keys
                    if key in concepts_by_key
                    and planned_anchors.intersection(concepts_by_key[key].asset_block_ids)
                ]
                if not owners:
                    first = concepts_by_key.get(spec.concept_keys[0])
                    owners = [first] if first is not None else []
                if owners:
                    owners[0].asset_block_ids = list(
                        dict.fromkeys([*owners[0].asset_block_ids, *added_ids])
                    )
            payload["note_set_plan"] = note_set.model_dump()
            # Fragment restoration is a deterministic expansion of a
            # planner-selected visual sequence. Persist it in the owning
            # Candidate so subsequent validation, writing and provenance all
            # share one explicit asset contract.
            payload["knowledge_map"] = knowledge_map.model_dump()
            payload["visual_sequence_additions"] = visual_additions
            self.owner._write_generation_artifact(artifact_path, payload)

        source_note_outline, note_set, contract_repairs = (
            self.owner._normalize_combined_plan_contracts(
                document,
                knowledge_map,
                source_note_outline,
                note_set,
            )
        )
        if contract_repairs:
            planning_bundle.source_note_outline = source_note_outline
            planning_bundle.note_set_plan = note_set
            payload["planning_bundle"] = planning_bundle.model_dump()
            payload["source_note_outline"] = source_note_outline.model_dump()
            payload["note_set_plan"] = note_set.model_dump()
            payload["deterministic_plan_repairs"] = contract_repairs
            self.owner._write_generation_artifact(artifact_path, payload)

        source_note_outline = self.owner._prune_unbound_outline_leaves(
            source_note_outline, note_set
        )
        planning_bundle.source_note_outline = source_note_outline
        planning_bundle.note_set_plan = note_set
        payload["planning_bundle"] = planning_bundle.model_dump()
        payload["source_note_outline"] = source_note_outline.model_dump()
        payload["note_set_plan"] = note_set.model_dump()
        self.owner._write_generation_artifact(artifact_path, payload)

        outline_errors = self.owner._validate_source_note_outline(source_note_outline)
        if outline_errors:
            raise RuntimeError(
                "Source Note planning bundle failed outline validation: "
                + "; ".join(outline_errors)
            )

        initial_review = self.owner._deterministic_note_set_review(knowledge_map, note_set)
        plan_errors = self.owner._validate_note_set(
            document,
            knowledge_map,
            note_set,
            initial_review,
            source_note_outline,
        )
        note_set_review = initial_review

        payload["initial_note_set_review"] = initial_review.model_dump()
        payload["note_set_plan"] = note_set.model_dump()
        payload["note_set_review"] = note_set_review.model_dump()
        self.owner._write_generation_artifact(artifact_path, payload)
        if plan_errors:
            raise RuntimeError(
                "Final note-set plan failed deterministic validation: " + "; ".join(plan_errors)
            )

        if knowledge_map.concepts and not note_set.notes:
            raise RuntimeError(
                "The note-set planner returned no notes for a non-empty knowledge map"
            )

        drafts = dict(payload.get("drafts") or {})
        visual_context = dict(payload.get("visual_understandings") or {})
        if not payload.get("visual_analysis_complete"):
            # Text-only notes do not depend on image captions. Draft them while
            # the external vision provider works, instead of imposing a global
            # visual barrier on the whole document.
            text_only_specs = [
                spec
                for spec in note_set.notes
                if not spec.asset_block_ids and spec.stable_key not in drafts
            ]
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="noodle-visual-stage"
            ) as executor:
                visual_future = executor.submit(
                    self.owner._analyze_planned_visuals, job_id, document, note_set
                )
                drafts = self.owner._draft_specs_concurrently(
                    job_id=job_id,
                    document=document,
                    profile=profile,
                    knowledge_map=knowledge_map,
                    note_set=note_set,
                    specs=text_only_specs,
                    vision_context={},
                    artifact_path=artifact_path,
                    payload=payload,
                    drafts=drafts,
                )
                visual_context = visual_future.result()
            payload["visual_understandings"] = visual_context
            payload["visual_analysis_complete"] = True
            payload["schema_version"] = GENERATION_SCHEMA_VERSION
            self.owner._write_generation_artifact(artifact_path, payload)

        pending_specs = [spec for spec in note_set.notes if spec.stable_key not in drafts]
        drafts = self.owner._draft_specs_concurrently(
            job_id=job_id,
            document=document,
            profile=profile,
            knowledge_map=knowledge_map,
            note_set=note_set,
            specs=pending_specs,
            vision_context=visual_context,
            artifact_path=artifact_path,
            payload=payload,
            drafts=drafts,
        )

        final_plan = KnowledgePlan(
            document_summary=note_set.document_summary,
            notes=[NoteDraft.model_validate(drafts[spec.stable_key]) for spec in note_set.notes],
            skipped_sections=knowledge_map.skipped_sections,
        )
        final_plan, table_shape_repairs = self.owner._normalize_table_shapes(final_plan)
        if table_shape_repairs:
            drafts.update({note.stable_key: note.model_dump() for note in final_plan.notes})
            payload["drafts"] = drafts
            payload["deterministic_table_shape_repairs"] = table_shape_repairs
            self.owner._write_generation_artifact(artifact_path, payload)
        prevalidated_notes, deterministic_failures = self.owner._validate_plan(
            document,
            final_plan,
            note_set,
            visual_context,
        )
        payload["prejudge_validation_failures"] = deterministic_failures
        judge_plan = KnowledgePlan(
            document_summary=final_plan.document_summary,
            notes=prevalidated_notes,
            skipped_sections=final_plan.skipped_sections,
        )
        if not payload.get("evaluations"):
            self.owner._update_generation_stage(
                job_id,
                JobStage.EVALUATING_NOTES,
                0.82,
                generation_artifact=str(artifact_path),
                generation_step="evaluate_plan",
                current_note_key=None,
                drafted_note_count=len(drafts),
            )
            evaluations = self.owner._evaluate_notes_batched(
                job_id,
                document,
                note_set,
                judge_plan,
                knowledge_map,
                visual_context,
            )
            valid_keys = {note.stable_key for note in prevalidated_notes}
            failures_by_key = {
                str(failure.get("candidate")): failure for failure in deterministic_failures
            }
            evaluations.extend(
                NoteEvaluation(
                    stable_key=note.stable_key,
                    passed=False,
                    issues=[
                        "DETERMINISTIC_VALIDATION_FAILED: "
                        + "; ".join(
                            str(item)
                            for item in (
                                failures_by_key.get(note.stable_key, {}).get("detail") or []
                            )
                        )
                    ],
                    content_sha256=note_content_sha256(note),
                    evaluator_model="deterministic",
                    prompt_version=JUDGE_CONTRACT_VERSION,
                )
                for note in final_plan.notes
                if note.stable_key not in valid_keys
            )
            payload["evaluations"] = [item.model_dump() for item in evaluations]
            self.owner._write_generation_artifact(artifact_path, payload)
        else:
            cached_evaluations = [
                NoteEvaluation.model_validate(item) for item in payload["evaluations"]
            ]
            cached_by_key = {item.stable_key: item for item in cached_evaluations}
            stale_notes = [
                note
                for note in final_plan.notes
                if note.stable_key not in cached_by_key
                or cached_by_key[note.stable_key].content_sha256 != note_content_sha256(note)
                or cached_by_key[note.stable_key].prompt_version != JUDGE_CONTRACT_VERSION
            ]
            if stale_notes:
                self.owner._update_generation_stage(
                    job_id,
                    JobStage.EVALUATING_NOTES,
                    0.82,
                    generation_artifact=str(artifact_path),
                    generation_step="reevaluate_stale_hashes",
                    current_note_key=None,
                    stale_evaluation_count=len(stale_notes),
                )
                refreshed = self.owner._evaluate_notes_batched(
                    job_id,
                    document,
                    note_set,
                    KnowledgePlan(
                        document_summary=final_plan.document_summary,
                        notes=stale_notes,
                        skipped_sections=final_plan.skipped_sections,
                    ),
                    knowledge_map,
                    visual_context,
                )
                cached_by_key.update({item.stable_key: item for item in refreshed})
            evaluations = [
                cached_by_key[note.stable_key]
                for note in final_plan.notes
                if note.stable_key in cached_by_key
            ]
            payload["evaluations"] = [item.model_dump() for item in evaluations]
            self.owner._write_generation_artifact(artifact_path, payload)

        return GenerationBundle(
            profile=profile,
            knowledge_map=knowledge_map,
            source_note_outline=source_note_outline,
            note_set_plan=note_set,
            note_set_review=note_set_review,
            final_plan=final_plan,
            evaluations=evaluations,
        )
