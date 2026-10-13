from __future__ import annotations

from threading import RLock
from typing import Any

from sqlalchemy import select

from ...composition import Component
from ...db import (
    AtomicNote,
    BackboneSection,
    Claim,
    ClaimEvidence,
    Concept,
    ConceptContribution,
    ConceptModule,
    EvidenceSpan,
    KnowledgeContributionSelection,
    NoteAsset,
    NoteBlock,
    NoteVersion,
    Source,
    SourcePage,
)
from ..blueprints import MACHINE_LEARNING
from ..machine_learning_blueprint import ConceptSpec
from .common import (
    MODULE_ROLE_ORDER,
    _canonical_module_role,
    _collapsed_module,
    _loads,
    _module_title_hint,
    _prefix_depth,
    _prefix_matches,
    _semantic_role,
    _slug,
)


class InputsModule(Component):
    def _concept_lock(self, stable_key: str) -> RLock:
        with self.owner._concept_locks_guard:
            return self.owner._concept_locks.setdefault(stable_key, RLock())

    def _global_spec(self, spec: ConceptSpec) -> dict[str, Any]:
        """Remove source-specific wording from a reusable Concept build spec."""

        stable_key = self.owner.blueprint.stable_concept_key(spec)
        title = next(
            (
                item["title"]
                for item in MACHINE_LEARNING.concepts
                if MACHINE_LEARNING.stable_concept_key(item) == stable_key
            ),
            spec["title"],
        )
        return {
            "key": stable_key,
            "stable_key": stable_key,
            "title": title,
            "parent_key": "",
            "kind": "COMPOSITE",
            "source_prefixes": [],
            "summary_hint": f"{title}的定义、直觉、机制、关键性质、应用与边界。",
        }

    def _owner(self, number: str) -> tuple[ConceptSpec, str] | None:
        blueprint = self.owner.blueprint
        if not number or any(
            _prefix_matches(number, item) for item in blueprint.excluded_source_prefixes
        ):
            return None
        exercise_owner = blueprint.exercise_owner_by_section.get(number)
        if exercise_owner:
            return next(
                item for item in blueprint.concepts if item["key"] == exercise_owner
            ), number
        matches: list[tuple[tuple[int, int], ConceptSpec, str]] = []
        for spec in blueprint.concepts:
            for prefix in spec["source_prefixes"]:
                if _prefix_matches(number, prefix):
                    matches.append((_prefix_depth(prefix), spec, prefix))
        if not matches:
            return None
        _depth, spec, prefix = max(matches, key=lambda item: item[0])
        return spec, prefix

    @staticmethod
    def _source_text(note: dict[str, Any], *, max_chars: int = 6_000) -> str:
        body = "\n\n".join(
            [note["title"], note["thesis"], *[item["text"] for item in note["blocks"]]]
        )
        return body[:max_chars]

    def _prepare_inputs(
        self, corpus_id: str, source_version_id: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        with self.owner.db.session() as session:
            sections = session.scalars(
                select(BackboneSection)
                .where(BackboneSection.corpus_id == corpus_id)
                .order_by(BackboneSection.sort_order)
            ).all()
            notes = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            note_by_stable = {note.stable_key: (note, version) for note, version in notes}
            version_ids = [version.id for _note, version in notes]
            block_rows = (
                session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id.in_(version_ids))
                    .order_by(NoteBlock.note_version_id, NoteBlock.order_index)
                ).all()
                if version_ids
                else []
            )
            blocks_by_version: dict[str, list[NoteBlock]] = {}
            for block in block_rows:
                blocks_by_version.setdefault(block.note_version_id, []).append(block)

            source_notes: dict[str, dict[str, Any]] = {}
            section_key_by_note_version: dict[str, str] = {}
            for section in sections:
                pair = note_by_stable.get(
                    f"{self.owner.blueprint.source_note_prefix}:{section.stable_key}"
                )
                if pair is None:
                    continue
                note, version = pair
                source_notes[section.id] = {
                    "note_id": note.id,
                    "note_version_id": version.id,
                    "content_sha256": version.content_sha256,
                    "title": version.title,
                    "thesis": version.thesis,
                    "blocks": [
                        {
                            "block_key": item.block_key,
                            "type": item.block_type,
                            "text": item.content_text,
                        }
                        for item in blocks_by_version.get(version.id, [])
                    ],
                }
                section_key_by_note_version[version.id] = section.stable_key

            by_number = {item.number: item for item in sections if item.number}
            prepared: dict[str, dict[str, Any]] = {
                spec["key"]: {"spec": spec, "modules": {}} for spec in self.owner.blueprint.concepts
            }
            for section in sections:
                if section.node_kind != "CONCEPT" or section.id not in source_notes:
                    continue
                if section.title in self.owner.blueprint.excluded_title_terms:
                    continue
                owner = self.owner._owner(section.number)
                if owner is None:
                    continue
                spec, matched_prefix = owner
                primary_prefix = spec["source_prefixes"][0]
                base_section = by_number.get(matched_prefix, section)
                multiple_prefixes = len(spec["source_prefixes"]) > 1
                nested_prefixes = any(
                    prefix != primary_prefix and primary_prefix.startswith(prefix + ".")
                    for prefix in spec["source_prefixes"]
                )
                collapse_by_prefix = (
                    multiple_prefixes
                    and not nested_prefixes
                    and (matched_prefix != primary_prefix or base_section.level >= 3)
                )
                collapse_micro_sections = (
                    not collapse_by_prefix
                    and base_section.level >= 3
                    and section.number != matched_prefix
                )
                is_exercise = section.number in self.owner.blueprint.exercise_owner_by_section
                if is_exercise:
                    module_key = "exercises"
                    module_title = "习题"
                    module_role = "EXERCISE"
                elif collapse_by_prefix:
                    module_key = _slug(base_section.title)
                    module_title = base_section.title
                    module_role = _semantic_role(module_title, module_key)
                elif collapse_micro_sections:
                    module_key, module_title, module_role = _collapsed_module(section.title)
                elif section.number == matched_prefix:
                    if matched_prefix == primary_prefix or primary_prefix.startswith(
                        matched_prefix + "."
                    ):
                        module_key = "overview"
                        module_title = "概述"
                    else:
                        module_key = _slug(section.title)
                        module_title = section.title
                    module_role = _semantic_role(module_title, module_key)
                else:
                    prefix_parts = matched_prefix.split(".")
                    number_parts = section.number.split(".")
                    direct_number = ".".join(number_parts[: len(prefix_parts) + 1])
                    direct = by_number.get(direct_number, section)
                    if direct.title == "Introduction":
                        module_key = "overview"
                        module_title = "概述"
                    else:
                        module_key = _slug(direct.title)
                        module_title = direct.title
                    module_role = _semantic_role(module_title, module_key)
                module = prepared[spec["key"]]["modules"].setdefault(
                    module_key,
                    {
                        "block_key": module_key,
                        "title": module_title,
                        "semantic_role": module_role,
                        "sources": [],
                        "sort_order": section.sort_order,
                    },
                )
                module["sort_order"] = min(module["sort_order"], section.sort_order)
                module["sources"].append(
                    {
                        **source_notes[section.id],
                        "section_number": section.number,
                        "section_title": section.title,
                        "pages": [section.start_page, section.end_page],
                        "source_ref": self.owner._source_ref(section),
                    }
                )

            for item in prepared.values():
                item["modules"] = sorted(
                    item["modules"].values(), key=lambda module: module["sort_order"]
                )
            return prepared, section_key_by_note_version

    def _source_ref(self, section: BackboneSection) -> str:
        """Preserve native coordinates instead of inventing PDF pages."""

        locator = _loads(section.source_locator_json, {})
        source_path = str(locator.get("path") or "").strip()
        if source_path:
            return f"{self.owner.blueprint.source_ref_prefix}:{section.number}:{source_path}"
        return (
            f"{self.owner.blueprint.source_ref_prefix}:{section.number}:"
            f"p{section.start_page}-p{section.end_page}"
        )

    def _register_foundation_contributions(self, prepared: dict[str, dict[str, Any]]) -> None:
        """Register trusted backbone mappings before Concept generation."""

        with self.owner.db.session() as session:
            for item in prepared.values():
                spec: ConceptSpec = item["spec"]
                concept = session.scalar(
                    select(Concept).where(
                        Concept.stable_key == self.owner.blueprint.stable_concept_key(spec)
                    )
                )
                if concept is None:
                    raise ValueError(
                        f"Concept was not registered: {self.owner.blueprint.stable_concept_key(spec)}"
                    )
                for seed in item["modules"]:
                    module = session.scalar(
                        select(ConceptModule).where(
                            ConceptModule.concept_id == concept.id,
                            ConceptModule.module_key == seed["block_key"],
                        )
                    )
                    if module is None:
                        module = ConceptModule(
                            concept_id=concept.id,
                            module_key=seed["block_key"],
                            semantic_role=_canonical_module_role(seed["block_key"]),
                        )
                        session.add(module)
                        session.flush()
                    else:
                        module.visibility = "ACTIVE"
                        module.semantic_role = _canonical_module_role(seed["block_key"])
                    for source in seed["sources"]:
                        contribution = session.scalar(
                            select(ConceptContribution).where(
                                ConceptContribution.module_id == module.id,
                                ConceptContribution.atomic_note_version_id
                                == source["note_version_id"],
                                ConceptContribution.contribution_role == "FOUNDATION",
                            )
                        )
                        if contribution is None:
                            contribution = ConceptContribution(
                                module_id=module.id,
                                atomic_note_version_id=source["note_version_id"],
                                source_role="ATOMIC_NOTE",
                                contribution_role="FOUNDATION",
                                assigned_by="semantic-backbone-v3",
                            )
                            session.add(contribution)
                        contribution.source_ref = source["source_ref"]
                        contribution.decision_status = "ACCEPTED"
                        contribution.rationale = "由受信任 Backbone 的确定性章节映射归入知识模块"

    def _prepared_from_contributions(
        self,
        spec: ConceptSpec,
        *,
        include_contribution_ids: set[str] | None = None,
        exclude_contribution_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Build deterministic input from accepted contributions plus one approved update."""

        included = include_contribution_ids or set()
        excluded = exclude_contribution_ids or set()

        with self.owner.db.session() as session:
            concept = session.scalar(
                select(Concept).where(
                    Concept.stable_key == self.owner.blueprint.stable_concept_key(spec)
                )
            )
            if concept is None:
                raise ValueError(
                    f"Unknown Concept: {self.owner.blueprint.stable_concept_key(spec)}"
                )
            modules: list[dict[str, Any]] = []
            for module in session.scalars(
                select(ConceptModule)
                .where(ConceptModule.concept_id == concept.id)
                .order_by(ConceptModule.created_at, ConceptModule.module_key)
            ):
                contributions = [
                    contribution
                    for contribution in session.scalars(
                        select(ConceptContribution)
                        .where(
                            ConceptContribution.module_id == module.id,
                            ConceptContribution.atomic_note_version_id.is_not(None),
                        )
                        .order_by(
                            ConceptContribution.source_ref,
                            ConceptContribution.atomic_note_version_id,
                        )
                    ).all()
                    if contribution.id not in excluded
                    and (contribution.decision_status == "ACCEPTED" or contribution.id in included)
                ]
                if not contributions:
                    continue
                module.visibility = "ACTIVE"
                sources: list[dict[str, Any]] = []
                seen_versions: set[str] = set()
                for contribution in contributions:
                    note_version_id = str(contribution.atomic_note_version_id)
                    if note_version_id in seen_versions:
                        continue
                    seen_versions.add(note_version_id)
                    version = session.get(NoteVersion, note_version_id)
                    if version is None:
                        continue
                    note = session.get(AtomicNote, version.note_id)
                    source_owner = session.get(Source, note.source_id) if note else None
                    if (
                        not note
                        or not source_owner
                        or note.visibility != "ACTIVE"
                        or source_owner.visibility != "ACTIVE"
                        or note.active_version_id != version.id
                        or source_owner.current_version_id != version.source_version_id
                    ):
                        continue
                    blocks = session.scalars(
                        select(NoteBlock)
                        .where(NoteBlock.note_version_id == version.id)
                        .order_by(NoteBlock.order_index)
                    ).all()
                    selection = session.get(KnowledgeContributionSelection, contribution.id)
                    if selection:
                        selected_ids = set(_loads(selection.block_ids_json, []))
                        blocks = [block for block in blocks if block.id in selected_ids]
                    if not blocks:
                        continue
                    evidence_rows = session.execute(
                        select(Claim, ClaimEvidence, EvidenceSpan, SourcePage)
                        .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
                        .join(EvidenceSpan, EvidenceSpan.id == ClaimEvidence.span_id)
                        .join(SourcePage, SourcePage.id == EvidenceSpan.page_id)
                        .where(Claim.note_version_id == version.id)
                    ).all()
                    evidence_by_block: dict[str, list[dict[str, Any]]] = {}
                    for claim, _link, span, page in evidence_rows:
                        evidence_by_block.setdefault(claim.note_block_id, []).append(
                            {
                                "evidence_span_id": span.id,
                                "page": page.page_index + 1,
                                "quote": span.quoted_text[:800],
                            }
                        )
                    asset_rows = session.scalars(
                        select(NoteAsset)
                        .where(NoteAsset.note_version_id == version.id)
                        .order_by(NoteAsset.order_index)
                    ).all()
                    assets_by_block: dict[str, list[str]] = {}
                    for asset in asset_rows:
                        if asset.note_block_id:
                            assets_by_block.setdefault(asset.note_block_id, []).append(
                                asset.visual_asset_id
                            )
                    sources.append(
                        {
                            "note_id": version.note_id,
                            "note_version_id": version.id,
                            "content_sha256": version.content_sha256,
                            "title": version.title,
                            "thesis": version.thesis if selection is None else "",
                            "blocks": [
                                {
                                    "block_key": block.block_key,
                                    "note_block_id": block.id,
                                    "type": block.block_type,
                                    "text": block.content_text,
                                    "evidence": evidence_by_block.get(block.id, []),
                                    "asset_ids": assets_by_block.get(block.id, []),
                                }
                                for block in blocks
                            ],
                            "section_number": "",
                            "section_title": version.title,
                            "pages": [],
                            "source_ref": contribution.source_ref,
                            "contribution_id": contribution.id,
                            "contribution_role": contribution.contribution_role,
                            "selected_block_ids": _loads(selection.block_ids_json, [])
                            if selection
                            else [],
                        }
                    )
                if not sources:
                    continue
                modules.append(
                    {
                        "block_key": module.module_key,
                        "title": _module_title_hint(module.module_key),
                        "semantic_role": module.semantic_role,
                        "sources": sources,
                        "sort_order": MODULE_ROLE_ORDER.get(module.semantic_role, 50),
                    }
                )
            modules.sort(key=lambda item: (item["sort_order"], item["block_key"]))
            return {"spec": spec, "modules": modules}
