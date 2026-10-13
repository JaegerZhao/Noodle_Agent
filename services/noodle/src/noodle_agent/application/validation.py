from __future__ import annotations

from typing import Any

from ..composition import Component
from ..domain import (
    ConceptCoverage,
    KnowledgeMap,
    KnowledgePlan,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedDocument,
    PlanIssue,
    SourceNoteOutlineNodeSpec,
    SourceNoteOutlinePlan,
)
from ..quality import (
    deduplicate_asset_placements,
    required_asset_ids,
    used_asset_ids,
)
from .common import (
    _sha256_text,
)


class ValidationModule(Component):
    @staticmethod
    def _deterministic_note_set_review(
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
    ) -> NoteSetReview:
        """Build complete concept coverage without a redundant approval call."""

        notes_by_concept: dict[str, list[str]] = {}
        for spec in note_set.notes:
            for concept_key in spec.concept_keys:
                notes_by_concept.setdefault(concept_key, []).append(spec.stable_key)
        skipped = {
            str(item.get("concept_key")): str(item.get("reason") or "")
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        coverage: list[ConceptCoverage] = []
        issues: list[PlanIssue] = []
        for concept in knowledge_map.concepts:
            note_keys = list(dict.fromkeys(notes_by_concept.get(concept.stable_key, [])))
            if note_keys:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="COVERED",
                        note_keys=note_keys,
                        reason="由规划结果确定性映射。",
                    )
                )
            elif concept.stable_key in skipped:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="SKIPPED",
                        reason=skipped[concept.stable_key],
                    )
                )
            else:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="MISSING",
                        reason="规划未覆盖且未说明跳过原因。",
                    )
                )
                issues.append(
                    PlanIssue(
                        code="UNACCOUNTED_CONCEPT",
                        detail=f"知识概念 {concept.stable_key} 未被规划覆盖。",
                        concept_keys=[concept.stable_key],
                    )
                )
        return NoteSetReview(
            passed=not issues,
            issues=issues,
            coverage=coverage,
        )

    @staticmethod
    def _normalize_combined_plan_contracts(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        """Repair mechanical planner mistakes without a second global LLM call.

        Semantic grouping remains owned by the combined planner.  This pass only
        enforces facts already present in the planner inputs: valid block ids,
        complete concept accounting, and placement on an outline leaf.
        """

        repairs: list[dict[str, Any]] = []
        known_blocks = {block.block_id for block in document.blocks}
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        concept_order = {
            concept.stable_key: index for index, concept in enumerate(knowledge_map.concepts)
        }
        notes = [spec.model_copy(deep=True) for spec in note_set.notes]

        for spec in notes:
            original_concepts = list(spec.concept_keys)
            spec.concept_keys = list(
                dict.fromkeys(key for key in spec.concept_keys if key in concepts)
            )
            spec.evidence_block_ids = list(
                dict.fromkeys(key for key in spec.evidence_block_ids if key in known_blocks)
            )
            spec.asset_block_ids = list(
                dict.fromkeys(key for key in spec.asset_block_ids if key in known_blocks)
            )
            if spec.concept_keys != original_concepts:
                repairs.append(
                    {
                        "type": "DROP_UNKNOWN_CONCEPT",
                        "note_key": spec.stable_key,
                    }
                )

        covered = {key for spec in notes for key in spec.concept_keys}
        skipped = {
            str(item.get("concept_key"))
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        for missing_key in [key for key in concepts if key not in covered and key not in skipped]:
            missing = concepts[missing_key]

            def note_score(
                spec: NoteSpec,
                missing_concept: Any = missing,
                missing_concept_key: str = missing_key,
            ) -> tuple[int, int, int]:
                evidence_overlap = len(
                    set(spec.evidence_block_ids) & set(missing_concept.evidence_block_ids)
                ) + len(set(spec.asset_block_ids) & set(missing_concept.asset_block_ids))
                section_overlap = len(
                    {item.casefold() for item in spec.source_sections}
                    & {item.casefold() for item in missing_concept.source_sections}
                )
                positions = [concept_order[key] for key in spec.concept_keys]
                distance = min(
                    (abs(concept_order[missing_concept_key] - position) for position in positions),
                    default=len(concepts),
                )
                return (evidence_overlap, section_overlap, -distance)

            if not notes:
                break
            target = max(notes, key=note_score)
            target.concept_keys.append(missing_key)
            target.evidence_block_ids = list(
                dict.fromkeys([*target.evidence_block_ids, *missing.evidence_block_ids])
            )
            target.asset_block_ids = list(
                dict.fromkeys([*target.asset_block_ids, *missing.asset_block_ids])
            )
            target.source_sections = list(
                dict.fromkeys([*target.source_sections, *missing.source_sections])
            )
            repairs.append(
                {
                    "type": "MERGE_UNACCOUNTED_CONCEPT",
                    "concept_key": missing_key,
                    "note_key": target.stable_key,
                }
            )

        nodes = [node.model_copy(deep=True) for node in outline.nodes]
        node_by_key = {node.stable_key: node for node in nodes}
        parent_keys = {node.parent_key for node in nodes if node.parent_key}
        used_node_keys = set(node_by_key)
        for spec in notes:
            node = node_by_key.get(spec.outline_node_key)
            if node is None:
                leaves = [item for item in nodes if item.stable_key not in parent_keys]
                if leaves:
                    spec.outline_node_key = leaves[0].stable_key
                    repairs.append(
                        {
                            "type": "REBIND_UNKNOWN_OUTLINE_NODE",
                            "note_key": spec.stable_key,
                            "outline_key": spec.outline_node_key,
                        }
                    )
                continue
            if node.stable_key not in parent_keys:
                continue
            if node.level >= 3:
                continue
            base_key = f"{node.stable_key}--{spec.stable_key}"
            leaf_key = base_key
            suffix = 2
            while leaf_key in used_node_keys:
                leaf_key = f"{base_key}-{suffix}"
                suffix += 1
            leaf = SourceNoteOutlineNodeSpec(
                stable_key=leaf_key,
                title=spec.title,
                level=node.level + 1,
                parent_key=node.stable_key,
                learning_goal="",
                source_sections=spec.source_sections,
            )
            nodes.append(leaf)
            node_by_key[leaf_key] = leaf
            used_node_keys.add(leaf_key)
            spec.outline_node_key = leaf_key
            repairs.append(
                {
                    "type": "CREATE_NOTE_LEAF",
                    "note_key": spec.stable_key,
                    "outline_key": leaf_key,
                }
            )

        return (
            outline.model_copy(update={"nodes": nodes}),
            note_set.model_copy(update={"notes": notes}),
            repairs,
        )

    @staticmethod
    def _normalize_minimum_outline_depth(
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        """Make a model-produced flat outline satisfy the persisted hierarchy.

        The SourceNote title is already the document root.  When a model returns
        only topic nodes, wrap them in one structural root that the Markdown
        renderer deterministically elides.  This preserves the visible outline
        and note bindings while giving persistence a stable two-level tree.

        An empty outline can still be recovered when the same planning response
        contains NoteSpecs: each note becomes a level-two topic.  Semantic
        regrouping remains a planner concern; this pass only repairs structure.
        """

        nodes = [node.model_copy(deep=True) for node in outline.nodes]
        notes = [spec.model_copy(deep=True) for spec in note_set.notes]
        repairs: list[dict[str, Any]] = []
        if nodes and any(node.level >= 2 for node in nodes):
            return outline, note_set, repairs
        if nodes and any(node.level != 1 or node.parent_key for node in nodes):
            return outline, note_set, repairs
        if not nodes and not notes:
            return outline, note_set, repairs

        used_keys = {node.stable_key for node in nodes}
        root_key = "source-note-root"
        suffix = 2
        while root_key in used_keys:
            root_key = f"source-note-root-{suffix}"
            suffix += 1

        source_sections = list(
            dict.fromkeys(
                section for item in [*nodes, *notes] for section in item.source_sections if section
            )
        )
        normalized_title = outline.title.strip() or note_set.document_summary.strip() or "知识笔记"
        root = SourceNoteOutlineNodeSpec(
            stable_key=root_key,
            title=normalized_title,
            level=1,
            parent_key=None,
            learning_goal="",
            source_sections=source_sections,
        )

        if nodes:
            wrapped = [
                node.model_copy(update={"level": 2, "parent_key": root_key}) for node in nodes
            ]
            repairs.append(
                {
                    "type": "WRAP_SINGLE_LEVEL_OUTLINE",
                    "root_key": root_key,
                    "topic_count": len(wrapped),
                }
            )
        else:
            wrapped = []
            used_keys = {root_key}
            for spec in notes:
                base_key = spec.outline_node_key.strip() or f"topic--{spec.stable_key}"
                leaf_key = base_key
                suffix = 2
                while leaf_key in used_keys:
                    leaf_key = f"{base_key}-{suffix}"
                    suffix += 1
                used_keys.add(leaf_key)
                wrapped.append(
                    SourceNoteOutlineNodeSpec(
                        stable_key=leaf_key,
                        title=spec.title,
                        level=2,
                        parent_key=root_key,
                        learning_goal="",
                        source_sections=spec.source_sections,
                    )
                )
                spec.outline_node_key = leaf_key
            repairs.append(
                {
                    "type": "BUILD_OUTLINE_FROM_NOTE_SET",
                    "root_key": root_key,
                    "topic_count": len(wrapped),
                }
            )

        return (
            outline.model_copy(update={"title": normalized_title, "nodes": [root, *wrapped]}),
            note_set.model_copy(update={"notes": notes}),
            repairs,
        )

    @staticmethod
    def _prune_unbound_outline_leaves(
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> SourceNoteOutlinePlan:
        """Remove planner-created empty branches without another approval call."""

        nodes = list(outline.nodes)
        referenced = {spec.outline_node_key for spec in note_set.notes if spec.outline_node_key}
        while nodes:
            parent_keys = {node.parent_key for node in nodes if node.parent_key}
            removable = {
                node.stable_key
                for node in nodes
                if node.stable_key not in referenced and node.stable_key not in parent_keys
            }
            if not removable:
                break
            nodes = [node for node in nodes if node.stable_key not in removable]
        return outline.model_copy(update={"nodes": nodes})

    @staticmethod
    def _validate_source_note_outline(
        outline: SourceNoteOutlinePlan,
    ) -> list[str]:
        errors: list[str] = []
        nodes = {node.stable_key: node for node in outline.nodes}
        if not outline.title.strip() or not outline.nodes:
            errors.append("EMPTY_SOURCE_NOTE_OUTLINE")
            return errors
        if len(nodes) != len(outline.nodes):
            errors.append("DUPLICATE_OUTLINE_KEY")
        child_counts = {key: 0 for key in nodes}
        for node in outline.nodes:
            if node.level == 1:
                if node.parent_key is not None:
                    errors.append(f"ROOT_HAS_PARENT:{node.stable_key}")
                continue
            parent = nodes.get(node.parent_key or "")
            if parent is None:
                errors.append(f"UNKNOWN_OUTLINE_PARENT:{node.stable_key}")
                continue
            if parent.level != node.level - 1:
                errors.append(f"INVALID_OUTLINE_LEVEL:{node.stable_key}")
            child_counts[parent.stable_key] += 1
        if not any(node.level >= 2 for node in outline.nodes):
            errors.append("OUTLINE_REQUIRES_TWO_LEVELS")
        return errors

    @staticmethod
    def _validate_note_set(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        review: NoteSetReview,
        source_note_outline: SourceNoteOutlinePlan | None = None,
    ) -> list[str]:
        errors: list[str] = []
        known_blocks = {block.block_id for block in document.blocks}
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        seen_notes: set[str] = set()
        covered: set[str] = set()
        concept_use_counts: dict[str, int] = {}
        outline_nodes = {
            node.stable_key: node
            for node in (source_note_outline.nodes if source_note_outline else [])
        }
        non_leaf_outline_keys = {
            node.parent_key for node in outline_nodes.values() if node.parent_key is not None
        }
        for spec in note_set.notes:
            if spec.stable_key in seen_notes:
                errors.append(f"DUPLICATE_NOTE_KEY:{spec.stable_key}")
            seen_notes.add(spec.stable_key)
            if not spec.question_answered.strip() or not spec.concept_keys:
                errors.append(f"INCOMPLETE_NOTE_SPEC:{spec.stable_key}")
            if source_note_outline:
                if spec.outline_node_key not in outline_nodes:
                    errors.append(
                        f"UNKNOWN_NOTE_OUTLINE_NODE:{spec.stable_key}:{spec.outline_node_key}"
                    )
                elif spec.outline_node_key in non_leaf_outline_keys:
                    errors.append(
                        f"NOTE_NOT_ON_OUTLINE_LEAF:{spec.stable_key}:{spec.outline_node_key}"
                    )
            unknown_concepts = set(spec.concept_keys) - set(concepts)
            if unknown_concepts:
                errors.append(
                    f"UNKNOWN_CONCEPT:{spec.stable_key}:{','.join(sorted(unknown_concepts))}"
                )
            covered.update(set(spec.concept_keys) & set(concepts))
            for concept_key in set(spec.concept_keys) & set(concepts):
                concept_use_counts[concept_key] = concept_use_counts.get(concept_key, 0) + 1
            allowed_assets = {
                asset_id
                for concept_key in spec.concept_keys
                if concept_key in concepts
                for asset_id in concepts[concept_key].asset_block_ids
            }
            unbound_assets = set(spec.asset_block_ids) - allowed_assets
            if unbound_assets:
                errors.append(f"UNBOUND_ASSET:{spec.stable_key}:{','.join(sorted(unbound_assets))}")
            unknown_blocks = (
                set(spec.evidence_block_ids) | set(spec.asset_block_ids)
            ) - known_blocks
            if unknown_blocks:
                errors.append(f"UNKNOWN_BLOCK:{spec.stable_key}:{','.join(sorted(unknown_blocks))}")
        skipped = {
            str(item.get("concept_key"))
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        for key, concept in concepts.items():
            if concept.importance == "CORE" and key not in covered:
                errors.append(f"UNCOVERED_CORE_CONCEPT:{key}")
            if key not in covered and key not in skipped:
                errors.append(f"UNACCOUNTED_CONCEPT:{key}")
            if concept_use_counts.get(key, 0) > 1:
                errors.append(f"DUPLICATE_CANDIDATE_ASSIGNMENT:{key}")
        review_keys = {item.concept_key for item in review.coverage}
        if review_keys != set(concepts):
            missing = set(concepts) - review_keys
            unknown = review_keys - set(concepts)
            if missing:
                errors.append(f"INCOMPLETE_REVIEW_COVERAGE:{','.join(sorted(missing))}")
            if unknown:
                errors.append(f"UNKNOWN_REVIEW_CONCEPT:{','.join(sorted(unknown))}")
        return errors

    @staticmethod
    def _normalize_table_shapes(
        plan: KnowledgePlan,
    ) -> tuple[KnowledgePlan, list[dict[str, Any]]]:
        """Make rectangular, headerless matrices renderable without losing cells.

        Models correctly use TABLE for small matrices but may omit semantic
        headers because the source itself has none. Markdown still requires a
        header row. Empty presentation headers preserve every source value and
        avoid an unnecessary repair call or dropped note. Ragged tables remain
        invalid and are handled by the normal repair path.
        """

        normalized = plan.model_copy(deep=True)
        repairs: list[dict[str, Any]] = []
        for note in normalized.notes:
            for index, block in enumerate(note.blocks):
                if block.block_type.upper() != "TABLE" or block.table_headers:
                    continue
                widths = {len(row) for row in block.table_rows if row}
                if len(widths) != 1:
                    continue
                width = next(iter(widths), 0)
                if width <= 0:
                    continue
                block.table_headers = [""] * width
                repairs.append(
                    {
                        "note_key": note.stable_key,
                        "block_index": index,
                        "type": "ADD_EMPTY_PRESENTATION_HEADERS",
                        "column_count": width,
                    }
                )
        return normalized, repairs

    @staticmethod
    def _validate_plan(
        document: ParsedDocument,
        plan: KnowledgePlan,
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list, list[dict[str, Any]]]:
        known = {block.block_id for block in document.blocks}
        block_types = {block.block_id: block.block_type.upper() for block in document.blocks}
        renderable_assets = {
            block.block_id
            for block in document.blocks
            if block.asset_path or block.block_type.upper() in {"IMAGE", "FIGURE", "CHART"}
        }
        specs = {spec.stable_key: spec for spec in (note_set.notes if note_set else [])}
        passed = []
        failures = []
        seen_keys: set[str] = set()
        for raw_note in plan.notes:
            note = deduplicate_asset_placements(raw_note)
            reasons = []
            seen_asset_ids: set[str] = set()
            if not note.title.strip() or not note.thesis.strip() or not note.blocks:
                reasons.append("EMPTY_NOTE")
            if any(character in f"{note.title}\n{note.thesis}" for character in "\ufffc\ufffd\x00"):
                reasons.append("INVALID_TEXT_CHARACTER")
            if note.stable_key in seen_keys:
                reasons.append("DUPLICATE_STABLE_KEY")
            allowed_block_types = {
                "PARAGRAPH",
                "BULLETED_LIST",
                "NUMBERED_LIST",
                "TABLE",
                "FORMULA",
                "CODE",
                "EXAMPLE",
                "CALLOUT",
                "IMAGE",
            }
            for block in note.blocks:
                block_type = block.block_type.upper()
                block_text = block.plain_text()
                if block_type not in allowed_block_types:
                    reasons.append(f"UNKNOWN_BLOCK_TYPE:{block.block_type}")
                if not block_text and not block.asset_block_ids:
                    reasons.append("EMPTY_BLOCK")
                if any(character in block_text for character in "\ufffc\ufffd\x00"):
                    reasons.append("INVALID_TEXT_CHARACTER")
                if block_type == "TABLE":
                    width = len(block.table_headers)
                    if not width or any(len(row) != width for row in block.table_rows):
                        reasons.append("INVALID_TABLE_SHAPE")
                if block_type in {"BULLETED_LIST", "NUMBERED_LIST"} and not any(
                    item.strip() for item in block.items
                ):
                    reasons.append("EMPTY_LIST")
                if block_type == "IMAGE" and not block.asset_block_ids:
                    reasons.append("IMAGE_WITHOUT_ASSET")
                if block_type != "IMAGE" and "核心结论" in block_text:
                    reasons.append("VISIBLE_THESIS_LABEL")
                if not block.evidence_block_ids and not block.asset_block_ids:
                    reasons.append("MISSING_EVIDENCE")
                if unknown := set(block.evidence_block_ids) - known:
                    reasons.append(f"UNKNOWN_EVIDENCE:{','.join(sorted(unknown))}")
                if unknown_assets := set(block.asset_block_ids) - known:
                    reasons.append(f"UNKNOWN_ASSET:{','.join(sorted(unknown_assets))}")
                if non_visual_assets := {
                    block_id
                    for block_id in block.asset_block_ids
                    if block_id in block_types and block_id not in renderable_assets
                }:
                    reasons.append(f"NON_VISUAL_ASSET:{','.join(sorted(non_visual_assets))}")
                if duplicate_assets := set(block.asset_block_ids) & seen_asset_ids:
                    reasons.append(
                        f"DUPLICATE_ASSET_PLACEMENT:{','.join(sorted(duplicate_assets))}"
                    )
                seen_asset_ids.update(block.asset_block_ids)
            spec = specs.get(note.stable_key)
            if spec is not None:
                missing_assets = set(required_asset_ids(spec, vision_context)) - used_asset_ids(
                    note
                )
                if missing_assets:
                    reasons.append(f"MISSING_REQUIRED_ASSET:{','.join(sorted(missing_assets))}")
            if reasons:
                failures.append(
                    {
                        "code": "CANDIDATE_VALIDATION_FAILED",
                        "candidate": note.stable_key,
                        "detail": sorted(set(reasons)),
                    }
                )
            else:
                passed.append(note)
                seen_keys.add(note.stable_key)
        return passed, failures

    @staticmethod
    def _fallback_source_note_plan(
        source_title: str, plan: KnowledgePlan
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan]:
        root_key = "core-content"
        nodes = [
            SourceNoteOutlineNodeSpec(
                stable_key=root_key,
                title="核心内容",
                level=1,
                learning_goal="按来源主题系统掌握关键知识",
            )
        ]
        section_keys: dict[str, str] = {}
        specs: list[NoteSpec] = []
        for note in plan.notes:
            section = note.source_sections[0] if note.source_sections else "知识要点"
            if section not in section_keys:
                key = f"section-{_sha256_text(section)[:12]}"
                section_keys[section] = key
                nodes.append(
                    SourceNoteOutlineNodeSpec(
                        stable_key=key,
                        title=section,
                        level=2,
                        parent_key=root_key,
                        source_sections=[section],
                    )
                )
            evidence_ids = list(
                dict.fromkeys(
                    block_id for block in note.blocks for block_id in block.evidence_block_ids
                )
            )
            asset_ids = list(
                dict.fromkeys(
                    block_id for block in note.blocks for block_id in block.asset_block_ids
                )
            )
            specs.append(
                NoteSpec(
                    stable_key=note.stable_key,
                    title=note.title,
                    question_answered=note.title,
                    target_thesis=note.thesis,
                    outline_node_key=section_keys[section],
                    concept_keys=[],
                    evidence_block_ids=evidence_ids,
                    asset_block_ids=asset_ids,
                    tags=note.tags,
                    source_sections=note.source_sections,
                    rationale="由兼容生成路径按来源章节放置",
                )
            )
        return (
            SourceNoteOutlinePlan(
                title=source_title,
                summary=plan.document_summary,
                learning_goal="",
                nodes=nodes,
            ),
            NoteSetPlan(
                document_summary=plan.document_summary,
                notes=specs,
            ),
        )
