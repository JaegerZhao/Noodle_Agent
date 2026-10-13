from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any

from ...composition import Component
from ...domain import (
    DocumentProfile,
    KnowledgeConcept,
    KnowledgeMap,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedDocument,
    SourceNoteOutlineNodeSpec,
    SourceNoteOutlinePlan,
    SourceNotePlanningBundle,
)
from .common import (
    _canonical_tags,
    _compact_knowledge_map,
    _compact_planning_knowledge_map,
    _compact_profile,
    _prompt,
    _slug,
)


class PlanningModule(Component):
    def plan_source_note_bundle(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        """Create one global outline and NoteSpec list from compact Candidates."""

        max_note_count = max(
            1,
            len(profile.segments),
            (len(knowledge_map.concepts) + 1) // 2,
        )
        result = self.owner._json_call(
            system=f"{_prompt('source_plan.v2.md')}\n{self.owner._language_instruction}",
            user={
                "document": {
                    "title": document.title,
                    "document_type": profile.document_type,
                },
                "sections": [
                    {
                        "key": item.stable_key,
                        "title": item.title,
                        "start_page": item.start_page_index,
                        "end_page": item.end_page_index,
                    }
                    for item in profile.segments
                ],
                "candidates": [
                    {
                        "id": item.stable_key,
                        "title": item.title,
                        "claim": item.core_claim,
                        "section_key": (item.source_sections[0] if item.source_sections else ""),
                        "asset_ids": item.asset_block_ids,
                    }
                    for item in knowledge_map.concepts
                ],
                "max_note_count": max_note_count,
            },
            model=self.owner.pro_model,
            operation="plan_source_v2",
            prompt_version="source-plan-v2",
        )
        candidate_ids = {item.stable_key for item in knowledge_map.concepts}

        def normalize_raw_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
            normalized_notes: list[dict[str, Any]] = []
            for raw in payload.get("notes") or []:
                bound = list(
                    dict.fromkeys(
                        str(item)
                        for item in raw.get("candidate_ids") or []
                        if str(item) in candidate_ids
                    )
                )
                # A model may mirror an empty profile segment into the outline
                # and invent a note for it. It has no grounded unit and must not
                # reach drafting.
                if not bound:
                    continue
                normalized = dict(raw)
                normalized["candidate_ids"] = bound
                normalized_notes.append(normalized)
            return normalized_notes

        raw_notes = normalize_raw_notes(result)
        if len(raw_notes) > max_note_count:
            initial_result = result
            initial_notes = list(raw_notes)
            candidate_outline = {
                candidate_id: str(raw.get("outline_key") or "")
                for raw in initial_notes
                for candidate_id in raw.get("candidate_ids") or []
            }
            grouped = self.owner._json_call(
                system=(
                    f"{_prompt('source_plan_grouper.v1.md')}\n{self.owner._language_instruction}"
                ),
                user={
                    "document": document.title,
                    "max_group_count": max_note_count,
                    "candidates": [
                        {
                            "id": item.stable_key,
                            "title": item.title,
                            "claim": item.core_claim,
                            "section": (item.source_sections[0] if item.source_sections else ""),
                            "outline_key": candidate_outline.get(item.stable_key, ""),
                        }
                        for item in knowledge_map.concepts
                    ],
                },
                model=self.owner.pro_model,
                operation="group_source_candidates_v1",
                prompt_version="source-plan-grouper-v1",
            )
            concept_assets = {
                item.stable_key: item.asset_block_ids for item in knowledge_map.concepts
            }
            grouped_notes: list[dict[str, Any]] = []
            for index, raw in enumerate(grouped.get("groups") or []):
                bound = list(
                    dict.fromkeys(
                        str(item)
                        for item in raw.get("candidate_ids") or []
                        if str(item) in candidate_ids
                    )
                )
                if not bound:
                    continue
                outline_key = str(raw.get("outline_key") or "")
                if outline_key not in {candidate_outline.get(item, "") for item in bound}:
                    outline_key = candidate_outline.get(bound[0], "")
                grouped_notes.append(
                    {
                        "key": raw.get("key") or f"knowledge-unit-{index + 1}",
                        "title": raw.get("title") or "知识单元",
                        "question": raw.get("question") or raw.get("title"),
                        "outline_key": outline_key,
                        "candidate_ids": bound,
                        "asset_ids": list(
                            dict.fromkeys(
                                asset_id
                                for candidate_id in bound
                                for asset_id in concept_assets.get(candidate_id, [])
                            )
                        ),
                    }
                )
            flattened = [
                candidate_id for raw in grouped_notes for candidate_id in raw["candidate_ids"]
            ]
            grouping_is_valid = (
                len(grouped_notes) <= max_note_count
                and len(flattened) == len(candidate_ids)
                and set(flattened) == candidate_ids
            )
            if not grouping_is_valid:
                # Reliable final guard: pair adjacent notes from the original
                # reading-order plan. This path is deterministic and only runs
                # when the focused grouping call violates its tiny contract.
                grouped_notes = []
                for index in range(0, len(initial_notes), 2):
                    pair = initial_notes[index : index + 2]
                    bound = list(
                        dict.fromkeys(
                            candidate_id
                            for raw in pair
                            for candidate_id in raw.get("candidate_ids") or []
                        )
                    )
                    first = pair[0]
                    title = str(first.get("title") or "知识单元")
                    if len(pair) > 1:
                        title = f"{title}与相关机制"
                    grouped_notes.append(
                        {
                            "key": first.get("key") or f"knowledge-unit-{index // 2 + 1}",
                            "title": title,
                            "question": f"{title}包含哪些关键概念与机制？",
                            "outline_key": first.get("outline_key"),
                            "candidate_ids": bound,
                            "asset_ids": list(
                                dict.fromkeys(
                                    asset_id
                                    for candidate_id in bound
                                    for asset_id in concept_assets.get(candidate_id, [])
                                )
                            ),
                        }
                    )
            result = {
                "outline": initial_result.get("outline") or [],
                "notes": grouped_notes,
            }
            raw_notes = normalize_raw_notes(result)
        if len(raw_notes) > max_note_count:
            raise RuntimeError(
                "SourcePlan remains over-fragmented after early repair: "
                f"{len(raw_notes)} notes for {len(knowledge_map.concepts)} candidates "
                f"(max {max_note_count})"
            )
        note_set = self.owner._note_set_from_result(
            {
                "notes": [
                    {
                        "stable_key": raw.get("key"),
                        "title": raw.get("title"),
                        "question_answered": raw.get("question"),
                        "target_thesis": raw.get("question") or raw.get("title"),
                        "outline_node_key": raw.get("outline_key"),
                        "concept_keys": raw.get("candidate_ids") or [],
                        "asset_block_ids": raw.get("asset_ids") or [],
                    }
                    for raw in raw_notes
                ],
                "skipped_concepts": [],
            },
            document.title,
        )
        note_set = self.owner._hydrate_note_set_from_concepts(note_set, knowledge_map)

        raw_nodes = list(result.get("outline") or [])
        parent_candidates = {
            str(raw.get("key") or ""): (
                str(raw.get("parent_key")) if raw.get("parent_key") else None
            )
            for raw in raw_nodes
        }
        used_outline_keys = {
            str(raw.get("outline_key")) for raw in raw_notes if raw.get("outline_key")
        }
        retained_outline_keys = set(used_outline_keys)
        for key in list(used_outline_keys):
            parent = parent_candidates.get(key)
            seen: set[str] = set()
            while parent and parent not in seen:
                seen.add(parent)
                retained_outline_keys.add(parent)
                parent = parent_candidates.get(parent)
        raw_nodes = [raw for raw in raw_nodes if str(raw.get("key") or "") in retained_outline_keys]
        parent_by_key = {str(raw.get("key") or ""): raw.get("parent_key") for raw in raw_nodes}

        def node_level(key: str) -> int:
            level = 1
            seen: set[str] = set()
            parent = parent_by_key.get(key)
            while parent and parent not in seen and level < 3:
                seen.add(str(parent))
                level += 1
                parent = parent_by_key.get(str(parent))
            return level

        nodes = [
            SourceNoteOutlineNodeSpec(
                stable_key=str(raw.get("key") or _slug(str(raw.get("title") or "主题"))),
                title=str(raw.get("title") or "知识主题"),
                level=node_level(str(raw.get("key") or "")),
                parent_key=(str(raw.get("parent_key")) if raw.get("parent_key") else None),
                learning_goal="",
                source_sections=[],
            )
            for raw in raw_nodes
        ]
        summary_titles = list(
            dict.fromkeys(node.title for node in nodes if node.parent_key is None)
        ) or [spec.title for spec in note_set.notes[:6]]
        outline = SourceNoteOutlinePlan(
            title=document.title,
            summary=(
                "主要内容包括：" + "、".join(summary_titles) if summary_titles else document.title
            ),
            learning_goal="",
            nodes=nodes,
        )
        note_set = self.owner._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            schema_version="source-plan-v2",
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _plan_source_note_bundle_single(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        result = self.owner._json_call(
            system=(
                f"{_prompt('source_note_planner.v3.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
            },
            model=self.owner.pro_model,
            operation="plan_source_note_bundle_v3",
            prompt_version="source-note-planner-v4-nonthinking",
        )
        outline_raw = result.get("outline") or {}
        note_set_raw = result.get("note_set") or {}

        outline = self.owner._outline_from_result(
            outline_raw, document.title, knowledge_map.document_summary
        )
        note_set = self.owner._note_set_from_result(note_set_raw, knowledge_map.document_summary)
        note_set = self.owner._hydrate_note_set_from_concepts(note_set, knowledge_map)
        note_set = self.owner._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _plan_source_note_bundle_sharded(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        """Use one global outline and bounded note shards for large knowledge maps."""

        outline_result = self.owner._json_call(
            system=(
                f"{_prompt('source_note_outline_router.v1.md')}\n{self.owner._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_planning_knowledge_map(knowledge_map),
            },
            model=self.owner.pro_model,
            operation="plan_source_note_outline_v3",
            prompt_version="source-note-outline-router-v1-nonthinking",
        )
        outline = self.owner._outline_from_result(
            outline_result, document.title, knowledge_map.document_summary
        )
        shards = self.owner._planning_concept_shards(knowledge_map.concepts)

        def plan_shard(
            shard_index: int, concepts: list[KnowledgeConcept]
        ) -> tuple[int, NoteSetPlan]:
            last_error: Exception | None = None
            for _attempt in range(self.owner.planning_task_retries + 1):
                try:
                    user = {
                        "document": document.title,
                        "batch": {
                            "index": shard_index + 1,
                            "count": len(shards),
                            "concept_count": len(concepts),
                            "max_notes": max(1, math.ceil(len(concepts) / 2)),
                        },
                        "source_note_outline": outline.model_dump(),
                        "concepts": [
                            concept.model_dump(
                                include={
                                    "stable_key",
                                    "title",
                                    "question_answered",
                                    "core_claim",
                                    "concept_type",
                                    "importance",
                                    "source_sections",
                                }
                            )
                            for concept in concepts
                        ],
                    }
                    if self.owner.telemetry is None:
                        result = self.owner._json_call(
                            system=(
                                f"{_prompt('note_set_planner_shard.v1.md')}\n"
                                f"{_prompt('writing_policy.v2.md')}\n"
                                f"{self.owner._language_instruction}"
                            ),
                            user=user,
                            model=self.owner.pro_model,
                            operation="plan_note_set_shard_v1",
                            prompt_version="note-set-planner-shard-v1-nonthinking",
                        )
                    else:
                        with self.owner.telemetry.bind(
                            note_key=f"source-note-planning-shard-{shard_index + 1}"
                        ):
                            result = self.owner._json_call(
                                system=(
                                    f"{_prompt('note_set_planner_shard.v1.md')}\n"
                                    f"{_prompt('writing_policy.v2.md')}\n"
                                    f"{self.owner._language_instruction}"
                                ),
                                user=user,
                                model=self.owner.pro_model,
                                operation="plan_note_set_shard_v1",
                                prompt_version="note-set-planner-shard-v1-nonthinking",
                            )
                    partial_map = KnowledgeMap(
                        document_summary=knowledge_map.document_summary,
                        concepts=concepts,
                    )
                    planned = self.owner._note_set_from_result(
                        result, knowledge_map.document_summary
                    )
                    planned = self.owner._hydrate_note_set_from_concepts(planned, partial_map)
                    return shard_index, planned
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            raise RuntimeError(
                f"Planning shard {shard_index + 1}/{len(shards)} failed after retries: {last_error}"
            ) from last_error

        planned_by_index: dict[int, NoteSetPlan] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.owner.planning_concurrency, len(shards)),
            thread_name_prefix="noodle-planning",
        ) as executor:
            futures = {
                executor.submit(copy_context().run, plan_shard, index, shard): index
                for index, shard in enumerate(shards)
            }
            for future in as_completed(futures):
                shard_index, planned = future.result()
                planned_by_index[shard_index] = planned

        notes: list[NoteSpec] = []
        skipped_concepts: list[dict[str, str]] = []
        used_note_keys: set[str] = set()
        for shard_index in range(len(shards)):
            planned = planned_by_index[shard_index]
            for spec in planned.notes:
                base_key = spec.stable_key
                stable_key = base_key
                suffix = 2
                while stable_key in used_note_keys:
                    stable_key = f"{base_key}-{suffix}"
                    suffix += 1
                if stable_key != spec.stable_key:
                    spec = spec.model_copy(update={"stable_key": stable_key})
                used_note_keys.add(stable_key)
                notes.append(spec)
            skipped_concepts.extend(planned.skipped_concepts)

        note_set = NoteSetPlan(
            document_summary=knowledge_map.document_summary,
            notes=notes,
            skipped_concepts=skipped_concepts,
        )
        note_set = self.owner._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            schema_version="source-note-planning-v5-sharded",
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _planning_concept_shards(
        self, concepts: list[KnowledgeConcept]
    ) -> list[list[KnowledgeConcept]]:
        """Keep source-adjacent concepts together while bounding response size."""

        if not concepts:
            return []
        target = max(2, self.owner.planning_shard_size)
        minimum = max(2, target // 2)
        hard_limit = target + 2
        shards: list[list[KnowledgeConcept]] = []
        current: list[KnowledgeConcept] = []
        current_sections: set[str] = set()
        for concept in concepts:
            sections = {item.casefold() for item in concept.source_sections if item}
            has_overlap = bool(current_sections & sections)
            crosses_topic = bool(current) and not has_overlap
            if current and (
                len(current) >= hard_limit or (len(current) >= minimum and crosses_topic)
            ):
                shards.append(current)
                current = []
                current_sections = set()
            current.append(concept)
            current_sections.update(sections)
        if current:
            shards.append(current)
        return shards

    @staticmethod
    def _outline_from_result(
        outline_raw: dict[str, Any], document_title: str, default_summary: str
    ) -> SourceNoteOutlinePlan:

        nodes: list[SourceNoteOutlineNodeSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(outline_raw.get("nodes") or []):
            title = str(raw.get("title") or f"知识主题 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            raw["learning_goal"] = ""
            node = SourceNoteOutlineNodeSpec.model_validate(raw)
            if node.stable_key in seen:
                continue
            nodes.append(node)
            seen.add(node.stable_key)

        outline = SourceNoteOutlinePlan(
            title=str(outline_raw.get("title") or document_title),
            summary=str(outline_raw.get("summary") or default_summary),
            learning_goal="",
            nodes=nodes,
        )
        return outline

    @staticmethod
    def _hydrate_note_set_from_concepts(
        note_set: NoteSetPlan, knowledge_map: KnowledgeMap
    ) -> NoteSetPlan:
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        hydrated: list[NoteSpec] = []
        for spec in note_set.notes:
            concept_keys = list(dict.fromkeys(key for key in spec.concept_keys if key in concepts))
            evidence: list[str] = []
            allowed_assets: list[str] = []
            sections: list[str] = []
            for key in concept_keys:
                concept = concepts[key]
                evidence.extend(concept.evidence_block_ids)
                allowed_assets.extend(concept.asset_block_ids)
                sections.extend(concept.source_sections)
            allowed_asset_set = set(allowed_assets)
            assets = [
                asset_id
                for asset_id in dict.fromkeys(spec.asset_block_ids)
                if asset_id in allowed_asset_set
            ]
            hydrated.append(
                spec.model_copy(
                    update={
                        "concept_keys": concept_keys,
                        "evidence_block_ids": list(dict.fromkeys(evidence)),
                        "asset_block_ids": list(dict.fromkeys(assets)),
                        "source_sections": list(dict.fromkeys(sections)),
                        "tags": _canonical_tags(spec.tags),
                    }
                )
            )
        return note_set.model_copy(update={"notes": hydrated})

    @staticmethod
    def _bind_notes_to_outline_leaves(
        note_set: NoteSetPlan, outline: SourceNoteOutlinePlan
    ) -> NoteSetPlan:
        parent_keys = {node.parent_key for node in outline.nodes if node.parent_key}
        leaves = [node for node in outline.nodes if node.stable_key not in parent_keys]
        if not leaves:
            return note_set
        leaf_keys = {node.stable_key for node in leaves}
        rebound: list[NoteSpec] = []
        for spec in note_set.notes:
            if spec.outline_node_key in leaf_keys:
                rebound.append(spec)
                continue
            wanted_sections = {item.casefold() for item in spec.source_sections}
            best = max(
                leaves,
                key=lambda node: len(
                    wanted_sections & {item.casefold() for item in node.source_sections}
                ),
            )
            rebound.append(spec.model_copy(update={"outline_node_key": best.stable_key}))
        return note_set.model_copy(update={"notes": rebound})

    def plan_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
    ) -> NoteSetPlan:
        result = self.owner._json_call(
            system=(
                f"{_prompt('note_set_planner.v2.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
            },
            model=self.owner.fast_model,
        )
        return self.owner._note_set_from_result(result, knowledge_map.document_summary)

    def plan_source_note_outline(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNoteOutlinePlan:
        result = self.owner._json_call(
            system=(
                f"{_prompt('source_note_outline_planner.v2.md')}\n"
                f"{self.owner._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
            },
            model=self.owner.pro_model,
        )
        nodes: list[SourceNoteOutlineNodeSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(result.get("nodes") or []):
            title = str(raw.get("title") or f"知识主题 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            node = SourceNoteOutlineNodeSpec.model_validate(raw)
            if node.stable_key in seen:
                continue
            nodes.append(node)
            seen.add(node.stable_key)
        return SourceNoteOutlinePlan(
            title=str(result.get("title") or document.title),
            summary=str(result.get("summary") or knowledge_map.document_summary),
            learning_goal="",
            nodes=nodes,
        )

    @staticmethod
    def _note_set_from_result(result: dict[str, Any], default_summary: str) -> NoteSetPlan:
        notes: list[NoteSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(result.get("notes") or []):
            title = str(raw.get("title") or f"原子笔记 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            raw.setdefault("question_answered", title)
            raw.setdefault("target_thesis", title)
            raw.setdefault("concept_keys", [])
            spec = NoteSpec.model_validate(raw)
            if spec.stable_key in seen:
                continue
            notes.append(spec)
            seen.add(spec.stable_key)
        return NoteSetPlan(
            document_summary=str(result.get("document_summary") or default_summary),
            notes=notes,
            skipped_concepts=result.get("skipped_concepts") or [],
        )

    def review_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> NoteSetReview:
        result = self.owner._json_call(
            system=f"{_prompt('note_set_judge.v2.md')}\n{self.owner._language_instruction}",
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
                "note_set_plan": note_set.model_dump(),
            },
            model=self.owner.pro_model,
        )
        return NoteSetReview.model_validate(result)

    def repair_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        review: NoteSetReview,
    ) -> NoteSetPlan:
        result = self.owner._json_call(
            system=(
                f"{_prompt('note_set_repair.v2.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self.owner._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
                "original_note_set": note_set.model_dump(),
                "review": review.model_dump(),
            },
            model=self.owner.pro_model,
        )
        return self.owner._note_set_from_result(result, knowledge_map.document_summary)
