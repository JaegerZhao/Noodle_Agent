from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any

from ...composition import Component
from ...domain import (
    DocumentProfile,
    GenerationBundle,
    KnowledgeMap,
    KnowledgePlan,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedDocument,
    SourceNoteOutlinePlan,
    SourceReleaseReview,
)
from ...quality import (
    deduplicate_asset_placements,
    ensure_required_visual_blocks,
    normalize_note_blocks,
    note_content_sha256,
    public_visual_context,
)
from .common import (
    _block_catalog,
    _canonical_tags,
    _prompt,
)


class WritingModule(Component):
    @staticmethod
    def _evidence_for_spec(document: ParsedDocument, spec: NoteSpec) -> list[dict[str, Any]]:
        wanted = set(spec.evidence_block_ids) | set(spec.asset_block_ids)
        return _block_catalog(
            [block for block in document.blocks if block.block_id in wanted],
            max_text_chars=2_400,
        )

    @staticmethod
    def _nearby_note_boundaries(note_set: NoteSetPlan, spec: NoteSpec) -> list[dict[str, str]]:
        """Return only boundaries that can realistically overlap this note."""

        ordered = list(note_set.notes)
        current_index = next(
            (index for index, item in enumerate(ordered) if item.stable_key == spec.stable_key),
            0,
        )
        related_concepts = set(spec.concept_keys)
        candidates: list[NoteSpec] = []
        for index, item in enumerate(ordered):
            if item.stable_key == spec.stable_key:
                continue
            is_neighbor = abs(index - current_index) <= 2
            same_leaf = item.outline_node_key == spec.outline_node_key
            concept_overlap = bool(related_concepts & set(item.concept_keys))
            if is_neighbor or same_leaf or concept_overlap:
                candidates.append(item)
        candidates.sort(
            key=lambda item: (
                item.outline_node_key != spec.outline_node_key,
                not bool(related_concepts & set(item.concept_keys)),
                abs(ordered.index(item) - current_index),
            )
        )
        return [
            {
                "stable_key": item.stable_key,
                "title": item.title,
                "question": item.question_answered,
            }
            for item in candidates[:8]
        ]

    def draft_notes(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
    ) -> KnowledgePlan:
        notes: list[NoteDraft] = []
        for spec in note_set.notes:
            notes.append(self.owner.draft_note(document, profile, knowledge_map, note_set, spec))
        return KnowledgePlan(
            document_summary=note_set.document_summary,
            notes=notes,
            skipped_sections=knowledge_map.skipped_sections,
        )

    def draft_note(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        spec: NoteSpec,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> NoteDraft:
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        related_concepts = [
            concepts[key].model_dump(
                include={
                    "stable_key",
                    "title",
                    "core_claim",
                }
            )
            for key in spec.concept_keys
            if key in concepts
        ]
        # Flash handles the many compact, evidence-bounded notes.  Reserve the
        # slower model for structures where cross-checking variables or
        # comparison axes materially improves correctness; the pro Judge still
        # reviews every batch.
        writer_model = (
            self.owner.pro_model
            if len(related_concepts) >= 3
            or spec.note_kind in {"FORMULA", "COMPARISON", "CASE"}
            or any(
                concept.concept_type in {"FORMULA", "COMPARISON"}
                for concept in (concepts[key] for key in spec.concept_keys if key in concepts)
            )
            else self.owner.fast_model
        )
        writer_system = (
            f"{_prompt('note_writer.v5.md')}\n"
            f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
        )
        writer_input = {
            "document": document.title,
            "document_type": profile.document_type,
            "note_spec": {
                "key": spec.stable_key,
                "title": spec.title,
                "question": spec.question_answered,
                "candidate_ids": spec.concept_keys,
                "asset_ids": spec.asset_block_ids,
            },
            "candidates": related_concepts,
            "evidence_catalog": self.owner._evidence_for_spec(document, spec),
            "vision_catalog": [
                public_visual_context(vision_context[block_id])
                for block_id in spec.asset_block_ids
                if vision_context and block_id in vision_context
            ],
            "other_note_boundaries": self.owner._nearby_note_boundaries(note_set, spec),
        }
        try:
            result = self.owner._json_call(
                system=writer_system,
                user=writer_input,
                model=writer_model,
                operation="draft_note_v6",
                prompt_version="note-writer-v12",
            )
        except RuntimeError as exc:
            # Some OpenAI-compatible flash routes occasionally return a 200
            # response with an empty message for particular evidence payloads.
            # Escalate only that note to the pro route; completed notes and
            # normal flash calls remain cached in the generation artifact.
            if writer_model == self.owner.pro_model or "empty structured response" not in str(exc):
                raise
            result = self.owner._json_call(
                system=writer_system,
                user=writer_input,
                model=self.owner.pro_model,
                operation="draft_note_v6_fallback",
                prompt_version="note-writer-v12",
            )
        result["stable_key"] = spec.stable_key
        result.setdefault("title", spec.title)
        result.setdefault("thesis", spec.target_thesis)
        result.setdefault("tags", spec.tags)
        result.setdefault("source_sections", spec.source_sections)
        draft = NoteDraft.model_validate(result)
        draft.tags = _canonical_tags(draft.tags)
        for block in draft.blocks:
            block.evidence_block_ids = list(dict.fromkeys(block.evidence_block_ids))
            block.asset_block_ids = list(dict.fromkeys(block.asset_block_ids))
        return deduplicate_asset_placements(
            ensure_required_visual_blocks(normalize_note_blocks(draft), spec, vision_context)
        )

    def evaluate_plan(
        self,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        specs = {spec.stable_key: spec for spec in note_set.notes}
        relevant_concept_keys = {
            concept_key
            for note in plan.notes
            if note.stable_key in specs
            for concept_key in specs[note.stable_key].concept_keys
        }
        payload = []
        for note in plan.notes:
            spec = specs.get(note.stable_key)
            evidence_ids = {
                block_id
                for block in note.blocks
                for block_id in (*block.evidence_block_ids, *block.asset_block_ids)
            }
            if spec is not None:
                evidence_ids.update(spec.evidence_block_ids)
                evidence_ids.update(spec.asset_block_ids)
            catalog = _block_catalog(
                [block for block in document.blocks if block.block_id in evidence_ids]
            )
            for item in catalog:
                item["text"] = str(item.get("text") or "")[:900]
            payload.append(
                {
                    "note_spec": spec.model_dump() if spec else None,
                    "note": note.model_dump(),
                    "evidence_catalog": catalog,
                    "vision_catalog": [
                        public_visual_context(vision_context[block_id])
                        for block_id in (spec.asset_block_ids if spec else [])
                        if vision_context and block_id in vision_context
                    ],
                }
            )
        judge_system = (
            f"{_prompt('note_judge.v3.md')}\n"
            f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
        )
        judge_user = {
            "document": document.title,
            "document_summary": (
                knowledge_map.document_summary if knowledge_map else note_set.document_summary
            ),
            "concepts": [
                concept.model_dump(
                    include={"stable_key", "title", "question_answered", "core_claim"}
                )
                for concept in (knowledge_map.concepts if knowledge_map else [])
                if concept.stable_key in relevant_concept_keys
            ],
            "notes": payload,
        }
        complex_kinds = {"FORMULA", "COMPARISON", "CASE", "PROCEDURE", "EXAMPLE"}
        use_pro = any(
            (spec := specs.get(note.stable_key)) is not None
            and (
                spec.note_kind.upper() in complex_kinds
                or bool(spec.asset_block_ids)
                or len(spec.concept_keys) >= 3
                or len(spec.evidence_block_ids) >= 10
            )
            for note in plan.notes
        )
        evaluator_model = self.owner.pro_model if use_pro else self.owner.fast_model
        result = self.owner._json_call(
            system=judge_system,
            user=judge_user,
            model=evaluator_model,
            operation="judge_note_batch_v11",
            prompt_version="note-judge-v12",
        )
        by_key = {note.stable_key for note in plan.notes}
        note_by_key = {note.stable_key: note for note in plan.notes}

        def parse_evaluations(raw_items: list[dict[str, Any]]) -> list[NoteEvaluation]:
            return [
                NoteEvaluation(
                    **NoteEvaluation.model_validate(raw).model_dump(
                        exclude={"content_sha256", "evaluator_model", "prompt_version"}
                    ),
                    content_sha256=note_content_sha256(note_by_key[str(raw.get("stable_key"))]),
                    evaluator_model=evaluator_model,
                    prompt_version="note-judge-v12",
                )
                for raw in raw_items
                if raw.get("stable_key") in by_key
            ]

        evaluations = parse_evaluations(result.get("evaluations") or [])
        returned = {item.stable_key for item in evaluations}
        missing = by_key - returned
        if missing:
            retry_result = self.owner._json_call(
                system=judge_system,
                user={
                    **judge_user,
                    "notes": [
                        item
                        for item in payload
                        if (item.get("note") or {}).get("stable_key") in missing
                    ],
                    "retry_instruction": (
                        "上一次审校漏掉了候选。必须对本次 notes 中的每个 stable_key "
                        "恰好返回一条 evaluation，不得省略。"
                    ),
                },
                model=evaluator_model,
                operation="judge_note_missing_retry_v2",
                prompt_version="note-judge-v12",
            )
            retry_evaluations = parse_evaluations(retry_result.get("evaluations") or [])
            retry_by_key = {item.stable_key: item for item in retry_evaluations}
            evaluations.extend(retry_by_key[key] for key in sorted(missing & retry_by_key.keys()))
            returned.update(retry_by_key)
        evaluations.extend(
            NoteEvaluation(
                stable_key=key,
                passed=False,
                issues=["JUDGE_MISSING_RESULT: 两次审校都未返回该候选的评价"],
                content_sha256=note_content_sha256(note_by_key[key]),
                evaluator_model=evaluator_model,
                prompt_version="note-judge-v12",
            )
            for key in by_key - returned
        )
        return evaluations

    def build_generation(self, document: ParsedDocument) -> GenerationBundle:
        profile = self.owner.profile_document(document)
        knowledge_map = self.owner.build_knowledge_map(document, profile)
        planning_bundle = self.owner.plan_source_note_bundle(document, profile, knowledge_map)
        source_note_outline = planning_bundle.source_note_outline
        note_set = planning_bundle.note_set_plan
        notes_by_concept = {
            concept.stable_key: [
                spec.stable_key
                for spec in note_set.notes
                if concept.stable_key in spec.concept_keys
            ]
            for concept in knowledge_map.concepts
        }
        note_set_review = NoteSetReview(
            passed=all(notes_by_concept.values()),
            coverage=[
                {
                    "concept_key": key,
                    "status": "COVERED" if note_keys else "MISSING",
                    "note_keys": note_keys,
                    "reason": "由联合规划结果确定性映射。",
                }
                for key, note_keys in notes_by_concept.items()
            ],
        )
        final_plan = self.owner.draft_notes(document, profile, knowledge_map, note_set)
        evaluations = self.owner.evaluate_plan(document, note_set, final_plan, knowledge_map)
        return GenerationBundle(
            profile=profile,
            knowledge_map=knowledge_map,
            source_note_outline=source_note_outline,
            note_set_plan=note_set,
            note_set_review=note_set_review,
            final_plan=final_plan,
            evaluations=evaluations,
        )

    def repair_plan(
        self,
        document: ParsedDocument,
        original: KnowledgePlan,
        failures: list[dict[str, Any]],
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> KnowledgePlan:
        failed_keys = {str(item.get("candidate")) for item in failures}
        specs = {spec.stable_key: spec for spec in (note_set.notes if note_set is not None else [])}
        notes_by_key = {note.stable_key: note for note in original.notes}
        failures_by_key = {
            str(item.get("candidate")): item for item in failures if item.get("candidate")
        }
        repair_system = (
            f"{_prompt('note_repair.v2.md')}\n"
            f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
        )

        def repair_one(key: str) -> NoteDraft | None:
            note = notes_by_key.get(key)
            spec = specs.get(key)
            if note is None:
                return None
            referenced_ids = {
                block_id
                for block in note.blocks
                for block_id in (*block.evidence_block_ids, *block.asset_block_ids)
            }
            if spec is not None:
                referenced_ids.update(spec.evidence_block_ids)
                referenced_ids.update(spec.asset_block_ids)
            evidence_catalog = [
                {
                    "block_id": block.block_id,
                    "type": block.block_type,
                    "page": block.page_label,
                    "text": block.text,
                    "asset_available": bool(block.asset_path),
                }
                for block in document.blocks
                if block.block_id in referenced_ids
            ]
            user = {
                "document": document.title,
                "failed_notes": [note.model_dump()],
                "failed_note_specs": [spec.model_dump()] if spec is not None else [],
                "failures": [failures_by_key[key]] if key in failures_by_key else [],
                "evidence_catalog": evidence_catalog,
                "vision_catalog": [
                    public_visual_context(vision_context[block_id])
                    for block_id in (spec.asset_block_ids if spec is not None else [])
                    if vision_context and block_id in vision_context
                ],
            }
            # A failed repair must not cause every successful sibling to be
            # regenerated. Keep retries local and let the caller retain a
            # precise remaining-failure record for an omitted key.
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    result = self.owner._json_call(
                        system=repair_system,
                        user=user,
                        model=self.owner.pro_model,
                        operation="repair_note_v5",
                        prompt_version="note-repair-v7",
                    )
                    candidates = result.get("notes") or []
                    raw = next(
                        (item for item in candidates if str(item.get("stable_key") or "") == key),
                        candidates[0] if len(candidates) == 1 else None,
                    )
                    if raw is None:
                        continue
                    raw["stable_key"] = key
                    return normalize_note_blocks(NoteDraft.model_validate(raw))
                except Exception as exc:  # noqa: BLE001 - per-note retry boundary
                    last_error = exc
            if last_error is not None:
                return None
            return None

        repaired_by_key: dict[str, NoteDraft] = {}
        workers = min(2, len(failed_keys))
        if workers:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="noodle-repair"
            ) as executor:
                future_map = {
                    executor.submit(copy_context().run, repair_one, key): key
                    for key in sorted(failed_keys)
                }
                for future in as_completed(future_map):
                    repaired_note = future.result()
                    if repaired_note is not None:
                        repaired_by_key[repaired_note.stable_key] = repaired_note
        repaired = [repaired_by_key[key] for key in sorted(repaired_by_key)]
        for note in repaired:
            spec = specs.get(note.stable_key)
            if spec is None:
                continue
            for block in note.blocks:
                if not block.evidence_block_ids and not block.asset_block_ids:
                    # A repaired statement still goes through the pro Judge;
                    # bind the allowed source catalog so it can assess the
                    # claim instead of failing before semantic review.
                    block.evidence_block_ids = list(spec.evidence_block_ids)
            ensure_required_visual_blocks(note, spec, vision_context)
        return KnowledgePlan(
            document_summary=original.document_summary,
            notes=repaired,
            skipped_sections=[],
        )

    def review_source_release(
        self,
        document: ParsedDocument,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        notes: list[NoteDraft],
    ) -> SourceReleaseReview:
        """Run one compact, document-level editorial consistency check."""

        specs = {item.stable_key: item for item in note_set.notes}
        outline_order = {node.stable_key: index for index, node in enumerate(outline.nodes)}
        note_order = {item.stable_key: index for index, item in enumerate(note_set.notes)}
        ordered_notes = sorted(
            notes,
            key=lambda note: (
                outline_order.get(
                    specs[note.stable_key].outline_node_key if note.stable_key in specs else "",
                    len(outline_order),
                ),
                note_order.get(note.stable_key, len(note_order)),
            ),
        )
        result = self.owner._json_call(
            system=(f"{_prompt('source_release_judge.v2.md')}\n{self.owner._language_instruction}"),
            user={
                "document": document.title,
                "source_note": {
                    "title": outline.title,
                    "summary": outline.summary,
                    "outline": [
                        {
                            "stable_key": node.stable_key,
                            "title": node.title,
                            "level": node.level,
                            "parent_key": node.parent_key,
                        }
                        for node in outline.nodes
                    ],
                },
                "notes": [
                    {
                        "stable_key": note.stable_key,
                        "outline_node_key": (
                            specs[note.stable_key].outline_node_key
                            if note.stable_key in specs
                            else ""
                        ),
                        "planned_title": (
                            specs[note.stable_key].title if note.stable_key in specs else ""
                        ),
                        "question": (
                            specs[note.stable_key].question_answered
                            if note.stable_key in specs
                            else ""
                        ),
                        "target_thesis": (
                            specs[note.stable_key].target_thesis if note.stable_key in specs else ""
                        ),
                        "title": note.title,
                        "thesis": note.thesis,
                        "body": "\n".join(
                            block.plain_text() for block in note.blocks if block.plain_text()
                        )[:1_200],
                    }
                    for note in ordered_notes
                ],
            },
            model=self.owner.pro_model,
            operation="judge_source_release_v2",
            prompt_version="source-release-judge-v2",
        )
        review = SourceReleaseReview.model_validate(result)
        # Providers occasionally emit passed=true while also returning blocking
        # note/global issues. The issue lists are authoritative for release safety.
        review.passed = not review.note_issues and not review.global_issues
        return review
