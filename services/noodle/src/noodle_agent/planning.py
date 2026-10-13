from __future__ import annotations

from .domain import KnowledgeConcept, KnowledgeMap, NoteSetPlan, NoteSpec, ParsedDocument


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _spec_from_concepts(
    *,
    base: NoteSpec,
    concepts: list[KnowledgeConcept],
    stable_key: str,
    fallback_title: str,
) -> NoteSpec:
    first = concepts[0]
    evidence = _ordered_unique(
        [block_id for concept in concepts for block_id in concept.evidence_block_ids]
    )
    assets = _ordered_unique(
        [block_id for concept in concepts for block_id in concept.asset_block_ids]
    )
    tags = _ordered_unique([tag for concept in concepts for tag in concept.tags])
    sections = _ordered_unique(
        [section for concept in concepts for section in concept.source_sections]
    )
    if len(concepts) == 1:
        title = first.title
        question = first.question_answered
        thesis = first.core_claim
    else:
        title = fallback_title
        question = f"{fallback_title}包含哪些关键发展里程碑？"
        thesis = "；".join(concept.core_claim for concept in concepts)
    return NoteSpec(
        stable_key=stable_key,
        title=title,
        question_answered=question,
        target_thesis=thesis,
        outline_node_key=base.outline_node_key,
        concept_keys=[concept.stable_key for concept in concepts],
        evidence_block_ids=evidence,
        asset_block_ids=assets,
        tags=tags or base.tags,
        source_sections=sections or base.source_sections,
        rationale="由确定性边界规范化将可独立检索的历史脉络从定义/机制笔记中拆出。",
        note_kind="CONCEPT",
        editorial_intent="简洁保留来源中的人物、时间与里程碑，不重复概念定义。",
        preferred_block_types=["PARAGRAPH", "BULLETED_LIST"],
        visual_requirements=[],
    )


def normalize_note_set_boundaries(
    note_set: NoteSetPlan,
    knowledge_map: KnowledgeMap,
) -> tuple[NoteSetPlan, dict[str, list[str]]]:
    """Split history from a mixed definition/mechanism retrieval intent.

    History is useful long-term knowledge but answers a different query from
    "what is X". LLM planning can miss that boundary in large documents. This
    normalization is deliberately narrow: it only acts when the KnowledgeMap
    already labels at least one concept HISTORY and at least one non-HISTORY.
    """

    concepts_by_key = {concept.stable_key: concept for concept in knowledge_map.concepts}
    normalized = note_set.model_copy(deep=True)
    output: list[NoteSpec] = []
    changes: dict[str, list[str]] = {}
    existing_keys = {spec.stable_key for spec in normalized.notes}
    for spec in normalized.notes:
        concepts = [
            concepts_by_key[key] for key in spec.concept_keys if key in concepts_by_key
        ]
        history = [
            concept for concept in concepts if concept.concept_type.upper() == "HISTORY"
        ]
        primary = [
            concept for concept in concepts if concept.concept_type.upper() != "HISTORY"
        ]
        if not history or not primary:
            output.append(spec)
            continue

        main = _spec_from_concepts(
            base=spec,
            concepts=primary,
            stable_key=spec.stable_key,
            fallback_title=spec.title,
        )
        history_key = f"{spec.stable_key}-history"
        suffix = 2
        while history_key in existing_keys:
            history_key = f"{spec.stable_key}-history-{suffix}"
            suffix += 1
        history_spec = _spec_from_concepts(
            base=spec,
            concepts=history,
            stable_key=history_key,
            fallback_title=f"{main.title}的发展历史",
        )
        output.extend([main, history_spec])
        existing_keys.add(history_key)
        changes[spec.stable_key] = [main.stable_key, history_spec.stable_key]
    normalized.notes = output
    return normalized, changes


def normalize_note_set_assets(
    document: ParsedDocument,
    note_set: NoteSetPlan,
) -> tuple[NoteSetPlan, dict[str, dict[str, list[str]]]]:
    """Keep visual IDs and textual evidence IDs in their correct channels.

    MinerU can emit OCR text on top of a large image. A planner may then put
    both IDs into ``asset_block_ids`` even though only the IMAGE block can be
    rendered. Reclassifying the OCR block as evidence preserves its content
    without manufacturing an invalid IMAGE block during deterministic repair.
    """

    blocks = {block.block_id: block for block in document.blocks}
    normalized = note_set.model_copy(deep=True)
    changes: dict[str, dict[str, list[str]]] = {}
    for spec in normalized.notes:
        visual_ids: list[str] = []
        evidence_ids = list(spec.evidence_block_ids)
        reclassified: list[str] = []
        for block_id in spec.asset_block_ids:
            block = blocks.get(block_id)
            if (
                block is not None
                and block.block_type.upper()
                in {"TABLE", "FORMULA", "EQUATION", "CODE"}
                and block.text.strip()
            ):
                reclassified.append(block_id)
                evidence_ids.append(block_id)
                continue
            if (
                block is None
                or bool(block.asset_path)
                or block.block_type.upper() in {"IMAGE", "FIGURE", "CHART"}
            ):
                visual_ids.append(block_id)
                continue
            reclassified.append(block_id)
            if block.text.strip():
                evidence_ids.append(block_id)
        if not reclassified:
            continue
        spec.asset_block_ids = _ordered_unique(visual_ids)
        spec.evidence_block_ids = _ordered_unique(evidence_ids)
        changes[spec.stable_key] = {
            "reclassified_as_evidence": reclassified,
            "remaining_visuals": list(spec.asset_block_ids),
        }
    return normalized, changes
