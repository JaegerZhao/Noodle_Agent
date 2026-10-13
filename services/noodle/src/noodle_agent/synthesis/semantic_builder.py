from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, RLock
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select

from ..db import (
    AtomicNote,
    BackboneCorpus,
    BackboneSection,
    BlobObject,
    Claim,
    ClaimEvidence,
    Concept,
    ConceptBuildRecord,
    ConceptContentEvidence,
    ConceptContribution,
    ConceptModule,
    ConceptModuleAsset,
    ConceptModuleJudgeRecord,
    ConceptModuleVersion,
    ConceptRelation,
    ConceptReleaseModule,
    ConceptUpdateJob,
    ConceptVersion,
    Database,
    EvidenceSpan,
    KnowledgeContributionSelection,
    NoteAsset,
    NoteBlock,
    NoteVersion,
    Source,
    SourcePage,
    Synthesis,
    SynthesisNode,
    SynthesisReferenceSource,
    SynthesisVersion,
    VisualAsset,
    utcnow,
)
from .blueprints import MACHINE_LEARNING, SynthesisBlueprint, get_blueprint
from .domain import (
    ConceptContentBlock,
    ConceptModuleJudgeResult,
    ConceptModulePayload,
    SemanticConceptDraft,
    SemanticConceptModuleDraft,
    SemanticSynthesisReport,
)
from .machine_learning_blueprint import ConceptSpec

SCHEMA_VERSION = "concept-v3"
PROMPT_VERSION = "semantic-concept-writer-v5"
JUDGE_PROMPT_VERSION = "semantic-concept-module-judge-v2"
REPAIR_PROMPT_VERSION = "semantic-concept-module-repair-v1"


class ModuleReviewRequired(ValueError):
    """A source-backed candidate failed the semantic publication gate."""



class ConceptUpdateError(ValueError):
    """A persisted Concept update failed; callers can still inspect its Job."""

    def __init__(self, message: str, *, job_id: str, error_code: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.error_code = error_code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    aliases = {
        "introduction": "overview",
        "discussion": "context-and-boundaries",
        "examples": "examples",
        "applications": "applications",
    }
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return aliases.get(normalized, normalized[:120] or "overview")


def _prefix_matches(number: str, prefix: str) -> bool:
    return number == prefix or number.startswith(prefix + ".")


def _prefix_depth(prefix: str) -> tuple[int, int]:
    return (prefix.count(".") + 1, len(prefix))


def _semantic_role(title: str, key: str) -> str:
    value = f"{title} {key}".casefold()
    if "example" in value or "application" in value:
        return "EXAMPLE" if "example" in value else "APPLICATION"
    if "limit" in value or "caveat" in value or "failure" in value:
        return "LIMITATION"
    if "compare" in value or " vs " in value or "versus" in value:
        return "COMPARISON"
    if key == "overview" or "introduction" in value:
        return "OVERVIEW"
    if "definition" in value or "what-is" in value:
        return "DEFINITION"
    return "MECHANISM"


def _collapsed_module(title: str) -> tuple[str, str, str]:
    """Collapse book-level micro-sections into reusable Concept module roles."""

    value = title.casefold()
    rules = [
        (("introduction", "definition", "basics"), "overview", "概述与定义", "OVERVIEW"),
        (("example", "worked"), "examples", "例子", "EXAMPLE"),
        (
            ("derivation", "algorithm", "fitting", "estimation", "learning", "training"),
            "mechanism-and-derivation",
            "机制与推导",
            "MECHANISM",
        ),
        (
            ("choosing", "tuning", "numerical", "computational", "speed", "scaling"),
            "implementation-and-selection",
            "实现与选择",
            "APPLICATION",
        ),
        (("comparison", "connection", " vs "), "comparisons", "比较与联系", "COMPARISON"),
        (("application",), "applications", "应用", "APPLICATION"),
        (
            ("limitation", "caution", "pros and cons", "curse", "failure", "unidentifiability"),
            "limitations",
            "边界与限制",
            "LIMITATION",
        ),
    ]
    for terms, key, label, role in rules:
        if any(term in value for term in terms):
            return key, label, role
    return "properties-and-variants", "关键性质与变体", "PROPERTIES"


MODULE_ROLE_ORDER = {
    "OVERVIEW": 0,
    "DEFINITION": 1,
    "INTUITION": 2,
    "MECHANISM": 3,
    "DERIVATION": 4,
    "PROPERTIES": 5,
    "COMPARISON": 6,
    "APPLICATION": 7,
    "EXAMPLE": 8,
    "LIMITATION": 9,
    "MISCONCEPTION": 10,
    "EXERCISE": 11,
}


def _module_title_hint(module_key: str) -> str:
    aliases = {
        "overview": "概述",
        "definition": "定义",
        "mechanism-and-derivation": "机制与推导",
        "properties-and-variants": "关键性质与变体",
        "implementation-and-selection": "实现与选择",
        "comparisons": "比较与联系",
        "applications": "应用",
        "examples": "例子",
        "limitations": "边界与限制",
        "exercises": "习题",
    }
    return aliases.get(module_key, module_key.replace("-", " "))


def _canonical_module_role(module_key: str) -> str:
    """Keep a shared module's role independent from textbook registration order."""

    roles = {
        "overview": "OVERVIEW",
        "definition": "DEFINITION",
        "mechanism-and-derivation": "MECHANISM",
        "properties-and-variants": "PROPERTIES",
        "implementation-and-selection": "APPLICATION",
        "comparisons": "COMPARISON",
        "applications": "APPLICATION",
        "examples": "EXAMPLE",
        "limitations": "LIMITATION",
        "exercises": "EXERCISE",
    }
    return roles.get(module_key, _semantic_role("", module_key))


class SemanticSynthesisBuilder:
    def __init__(
        self,
        database: Database,
        knowledge_agent: Any,
        blueprint: SynthesisBlueprint = MACHINE_LEARNING,
    ):
        self.db = database
        self.agent = knowledge_agent
        self.blueprint = blueprint
        self._concept_locks_guard = Lock()
        self._concept_locks: dict[str, RLock] = {}

    def _concept_lock(self, stable_key: str) -> RLock:
        with self._concept_locks_guard:
            return self._concept_locks.setdefault(stable_key, RLock())

    def _global_spec(self, spec: ConceptSpec) -> dict[str, Any]:
        """Remove source-specific wording from a reusable Concept build spec."""

        stable_key = self.blueprint.stable_concept_key(spec)
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
        blueprint = self.blueprint
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
        with self.db.session() as session:
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
                    f"{self.blueprint.source_note_prefix}:{section.stable_key}"
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
                spec["key"]: {"spec": spec, "modules": {}} for spec in self.blueprint.concepts
            }
            for section in sections:
                if section.node_kind != "CONCEPT" or section.id not in source_notes:
                    continue
                if section.title in self.blueprint.excluded_title_terms:
                    continue
                owner = self._owner(section.number)
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
                is_exercise = section.number in self.blueprint.exercise_owner_by_section
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
                        "source_ref": self._source_ref(section),
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
            return f"{self.blueprint.source_ref_prefix}:{section.number}:{source_path}"
        return (
            f"{self.blueprint.source_ref_prefix}:{section.number}:"
            f"p{section.start_page}-p{section.end_page}"
        )

    def _register_foundation_contributions(self, prepared: dict[str, dict[str, Any]]) -> None:
        """Register trusted backbone mappings before Concept generation."""

        with self.db.session() as session:
            for item in prepared.values():
                spec: ConceptSpec = item["spec"]
                concept = session.scalar(
                    select(Concept).where(
                        Concept.stable_key == self.blueprint.stable_concept_key(spec)
                    )
                )
                if concept is None:
                    raise ValueError(
                        f"Concept was not registered: {self.blueprint.stable_concept_key(spec)}"
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

        with self.db.session() as session:
            concept = session.scalar(
                select(Concept).where(Concept.stable_key == self.blueprint.stable_concept_key(spec))
            )
            if concept is None:
                raise ValueError(f"Unknown Concept: {self.blueprint.stable_concept_key(spec)}")
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
                    and (
                        contribution.decision_status == "ACCEPTED"
                        or contribution.id in included
                    )
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
                    if not note or not source_owner or note.visibility != "ACTIVE" or source_owner.visibility != "ACTIVE" or note.active_version_id != version.id or source_owner.current_version_id != version.source_version_id:
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
                            "selected_block_ids": _loads(selection.block_ids_json, []) if selection else [],
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

    @staticmethod
    def _fallback_module(seed: dict[str, Any]) -> SemanticConceptModuleDraft:
        source_chunks: list[str] = []
        for source in seed["sources"]:
            text = source["thesis"].strip() or source["title"]
            if text and text not in source_chunks:
                source_chunks.append(text)
            for block in source["blocks"][:2]:
                value = block["text"].strip()
                if value and value not in source_chunks:
                    source_chunks.append(value)
        markdown = "\n\n".join(source_chunks)[:10_000]
        if not markdown:
            markdown = f"{seed['title']}的教材来源已建立，等待进一步综合。"
        return SemanticConceptModuleDraft(
            block_key=seed["block_key"],
            title=seed["title"],
            render_type="PROSE",
            semantic_role=seed["semantic_role"],
            payload=ConceptModulePayload(
                blocks=[
                    {
                        "block_key": f"{seed['block_key']}-content-1",
                        "type": "PROSE",
                        "markdown": markdown,
                        "evidence_refs": sorted(
                            {
                                str(source.get("contribution_id") or "")
                                for source in seed["sources"]
                                if source.get("contribution_id")
                            }
                        ),
                    }
                ]
            ),
        )

    @staticmethod
    def _v3_module_candidate(raw: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
        """Normalize provider variants and bind every content block to valid evidence."""

        candidate = dict(raw)
        module_key = seed["block_key"]
        payload = dict(candidate.get("payload") or {})
        raw_blocks = payload.get("blocks") or candidate.pop("content_blocks", None)
        if not isinstance(raw_blocks, list) or not raw_blocks:
            legacy_type = str(candidate.get("render_type") or "PROSE").upper()
            raw_blocks = [
                {
                    "block_key": f"{module_key}-content-1",
                    "type": legacy_type,
                    **{
                        key: payload.get(key)
                        for key in (
                            "markdown",
                            "items",
                            "table_headers",
                            "table_rows",
                            "latex",
                            "symbols",
                            "steps",
                            "code",
                            "language",
                            "asset_ids",
                            "concept_key",
                        )
                        if payload.get(key)
                    },
                }
            ]
        allowed_evidence = {
            str(source.get("contribution_id") or "")
            for source in seed["sources"]
            if source.get("contribution_id")
        }
        aliases = {"PARAGRAPH": "PROSE", "FIGURE": "ASSET", "EXERCISE": "EXAMPLE"}
        blocks: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for index, value in enumerate(raw_blocks, 1):
            if not isinstance(value, dict):
                continue
            item = dict(value)
            raw_type = str(item.get("type") or item.pop("block_type", "PROSE")).upper()
            item["type"] = aliases.get(raw_type, raw_type)
            if item["type"] not in {
                "PROSE",
                "LIST",
                "TABLE",
                "FORMULA",
                "PROCEDURE",
                "EXAMPLE",
                "CODE",
                "ASSET",
                "CALLOUT",
                "CONCEPT_REF",
            }:
                item["type"] = "PROSE"
            key = str(item.get("block_key") or f"{module_key}-content-{index}")
            if key in seen_keys:
                key = f"{key}-{index}"
            seen_keys.add(key)
            item["block_key"] = key
            requested_evidence = item.get("evidence_refs")
            if isinstance(requested_evidence, list):
                evidence = [str(ref) for ref in requested_evidence if str(ref) in allowed_evidence]
            else:
                evidence = []
            item["evidence_refs"] = evidence
            for text_key in ("markdown", "latex", "code", "language", "concept_key"):
                if text_key in item and not isinstance(item[text_key], str):
                    item[text_key] = str(item[text_key] or "")
            for list_key in (
                "items",
                "steps",
                "asset_ids",
                "note_block_ids",
                "evidence_span_ids",
            ):
                value_list = item.get(list_key)
                if value_list is not None:
                    item[list_key] = (
                        [str(entry) for entry in value_list] if isinstance(value_list, list) else []
                    )
            headers = item.get("table_headers")
            if headers is not None:
                item["table_headers"] = (
                    [str(entry) for entry in headers] if isinstance(headers, list) else []
                )
            rows = item.get("table_rows")
            if rows is not None:
                item["table_rows"] = (
                    [[str(cell) for cell in row] for row in rows if isinstance(row, list)]
                    if isinstance(rows, list)
                    else []
                )
            normalized_symbols: list[dict[str, str]] = []
            raw_symbols = item.get("symbols")
            for symbol in raw_symbols if isinstance(raw_symbols, list) else []:
                if isinstance(symbol, dict):
                    name = str(symbol.get("symbol") or symbol.get("name") or "").strip()
                    meaning = str(symbol.get("meaning") or symbol.get("description") or "").strip()
                else:
                    raw_symbol = str(symbol).strip()
                    parts = re.split(r"\s*[:：]\s*", raw_symbol, maxsplit=1)
                    name = parts[0].strip()
                    meaning = parts[1].strip() if len(parts) > 1 else ""
                if name:
                    normalized_symbols.append({"symbol": name, "meaning": meaning})
            item["symbols"] = normalized_symbols
            if item["type"] == "ASSET" and not item.get("asset_ids"):
                if str(item.get("markdown") or "").strip():
                    item["type"] = "PROSE"
                else:
                    continue
            conflict = item.get("conflict")
            if isinstance(conflict, dict):
                conflict["status"] = str(conflict.get("status") or "UNRESOLVED").upper()
                if conflict["status"] not in {"UNRESOLVED", "RESOLVED"}:
                    conflict["status"] = "UNRESOLVED"
                positions = [
                    position
                    for position in conflict.get("positions", [])
                    if isinstance(position, dict)
                    and str(position.get("evidence_ref") or "") in allowed_evidence
                    and str(position.get("claim") or "").strip()
                ]
                if len(positions) < 2:
                    item.pop("conflict", None)
                else:
                    conflict["positions"] = positions
            try:
                blocks.append(ConceptContentBlock.model_validate(item).model_dump())
            except ValidationError:
                markdown = str(item.get("markdown") or "").strip()
                if markdown:
                    blocks.append(
                        ConceptContentBlock(
                            block_key=key,
                            type="PROSE",
                            markdown=markdown,
                            evidence_refs=item["evidence_refs"],
                        ).model_dump()
                    )
        payload["blocks"] = blocks
        candidate["payload"] = payload
        candidate["render_type"] = "PROSE"
        return candidate

    def _normalize_draft(
        self, payload: dict[str, Any], spec: ConceptSpec, modules: list[dict[str, Any]]
    ) -> tuple[SemanticConceptDraft, bool, str]:
        parsed: dict[str, SemanticConceptModuleDraft] = {}
        errors: list[str] = []
        expected_by_key = {item["block_key"]: item for item in modules}
        for raw in payload.get("modules", []) if isinstance(payload, dict) else []:
            if not isinstance(raw, dict):
                continue
            block_key = str(raw.get("block_key") or "")
            # Providers sometimes helpfully split a requested module into
            # smaller book-like subsections. Concept V3 intentionally owns the
            # module boundary, so extra keys are ignored instead of making the
            # otherwise valid response fall back to source excerpts.
            if block_key not in expected_by_key:
                continue
            candidate = self._v3_module_candidate(raw, expected_by_key[block_key])
            if candidate.get("semantic_role") not in {
                "OVERVIEW",
                "DEFINITION",
                "INTUITION",
                "MECHANISM",
                "PROPERTIES",
                "DERIVATION",
                "COMPARISON",
                "APPLICATION",
                "EXAMPLE",
                "LIMITATION",
                "MISCONCEPTION",
                "EXERCISE",
            }:
                candidate["semantic_role"] = expected_by_key[block_key]["semantic_role"]
            try:
                item = SemanticConceptModuleDraft.model_validate(candidate)
            except ValidationError as exc:
                errors.append(str(exc))
                continue
            parsed[item.block_key] = item
        expected = set(expected_by_key)
        normalized = [
            parsed.get(item["block_key"]) or self._fallback_module(item) for item in modules
        ]
        used_fallback = set(parsed) != expected or bool(errors)
        title = str(payload.get("title_zh") or spec["title"]).strip()
        summary = str(payload.get("summary") or spec["summary_hint"]).strip()
        raw_scope = payload.get("scope") if isinstance(payload, dict) else None
        raw_scope = raw_scope if isinstance(raw_scope, dict) else {}
        scope = {
            key: [str(item) for item in value] if isinstance(value, list) else []
            for key, value in {
                "includes": raw_scope.get("includes", [spec["summary_hint"]]),
                "excludes": raw_scope.get("excludes", ["特定来源的目录、前言、索引与重复说明"]),
                "prerequisites": raw_scope.get("prerequisites", []),
                "aliases": raw_scope.get("aliases", []),
            }.items()
        }
        draft = SemanticConceptDraft(
            title_zh=title,
            summary=summary,
            scope=scope,
            modules=normalized,
        )
        detail = "; ".join(errors[:2])
        if set(parsed) != expected:
            detail = (
                f"module keys expected={sorted(expected)}, actual={sorted(parsed)}; {detail}"
            ).strip("; ")
        return draft, used_fallback, detail

    def _write_draft(
        self,
        spec: ConceptSpec,
        modules: list[dict[str, Any]],
        update_context: dict[str, Any] | None = None,
    ) -> tuple[SemanticConceptDraft, bool, str]:
        prompt_modules = []
        per_module_budget = max(3_000, 32_000 // max(1, len(modules)))
        for module in modules:
            sources = []
            source_budget = max(
                500,
                per_module_budget // max(1, len(module["sources"])),
            )
            for source in module["sources"]:
                text = self._source_text(source, max_chars=min(4_000, source_budget))
                if not text:
                    continue
                sources.append(
                    {
                        "contribution_id": source.get("contribution_id", ""),
                        "note_version_id": source["note_version_id"],
                        "section": source.get("source_ref")
                        or f"{source.get('section_number', '')} {source.get('section_title', '')}",
                        "pages": source.get("pages", []),
                        "text": text,
                        "blocks": source.get("blocks", []),
                    }
                )
            prompt_modules.append(
                {
                    "block_key": module["block_key"],
                    "title": module["title"],
                    "semantic_role": module["semantic_role"],
                    "sources": sources,
                }
            )
        payload = self.agent.structured_json(
            system=(
                "你是长期知识库的知识编辑。请把多个来源笔记综合为一个跨教材、跨 Synthesis 复用的 Concept。"
                "Concept 不是原书章节复述；删除前言、目录口吻、学习目标和重复内容。"
                "输入仅包含当前有效来源。根据 update_context 的目标与保留要求更新；旧正文只用于对照，不得复用失效来源独有事实。"
                "必须进行去重、互补与冲突保留；重要旧知识在仍有有效支持时保留。数学公式保留条件，不能从任意有界函数推导无条件逼近。"
                "习题是 Concept 的有效 Block：保留题意、条件、问题与必要配图，不得省略；"
                "除非来源本身含解答，否则不要擅自补写答案。"
                "必须保留输入给出的全部 block_key，每个 key 恰好一次，不得新增或改名。"
                "同一 Block 内合并重复事实，保留必要定义、机制、公式、例子和适用边界；不得补写来源不支持的事实。"
                "输出严格 JSON：{title_zh,summary,scope:{includes:[],excludes:[],prerequisites:[],aliases:[]},"
                "modules:[{block_key,title,semantic_role,payload:{blocks:[contentBlock]}}]}。"
                "contentBlock={block_key,type,markdown,items,table_headers,table_rows,latex,symbols,steps,code,language,"
                "asset_ids,concept_key,evidence_refs,note_block_ids,evidence_span_ids,conflict}；type 只能是 PROSE/LIST/TABLE/FORMULA/PROCEDURE/"
                "EXAMPLE/CODE/CALLOUT/ASSET/CONCEPT_REF。每个 contentBlock 必须给出唯一 block_key，并在 evidence_refs"
                "中只使用输入提供的 contribution_id；note_block_ids 和 evidence_span_ids 必须精确引用输入 blocks 中"
                "真正支持本 Block 的 ID，不得笼统引用整篇笔记。asset_ids 只选择对理解本 Block 必要且由所选 NoteBlock"
                "提供的图片，装饰图不得选择。PROSE/CALLOUT/EXAMPLE 用 markdown；LIST 用 items；TABLE 用"
                "表头和行；FORMULA 的 latex 不含 $$ 定界符，并用 markdown 解释公式、symbols 解释主要符号；"
                "PROCEDURE 用 steps。一个 Module 可以有多个不同类型 contentBlock，使正文、公式、表格和例子自然穿插。"
                "每个 Module 最多输出 6 个 contentBlock；合并相邻内容，不重复结论。每块只选择最必要的支持 ID（通常各不超过8个），不要复制所有来源ID。"
                "若权威来源存在不能可靠消解的矛盾，在对应 Block 写 conflict={status:'UNRESOLVED',positions:["
                "{evidence_ref,claim},...],resolution:''}，不得静默选边。"
                "写作上使用简洁中文，专业名词首次出现保留英文括注；不用学习目标、本节目标和机械小结；"
                "不强求所有 Concept 具有同一栏目，也不生成空内容。Markdown 标题从三级以下开始，列表和表格之间留空行。"
            ),
            user={
                "concept": {
                    "canonical_key": self.blueprint.stable_concept_key(spec),
                    "title": spec["title"],
                    "kind": spec["kind"],
                    "summary_hint": spec["summary_hint"],
                },
                "required_modules": prompt_modules,
                "update_context": update_context or {},
            },
            operation="semantic_synthesis.write_concept_v3",
            prompt_version=PROMPT_VERSION,
            use_pro_model=spec["kind"] == "COMPOSITE",
        )
        return self._normalize_draft(payload, spec, modules)

    def _ensure_concepts(self) -> None:
        with self.db.session() as session:
            for spec in self.blueprint.concepts:
                stable_key = self.blueprint.stable_concept_key(spec)
                concept = session.scalar(select(Concept).where(Concept.stable_key == stable_key))
                if concept is None:
                    session.add(
                        Concept(
                            stable_key=stable_key,
                            domain_key=self.blueprint.domain_key,
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

        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None:
                raise ValueError(f"Unknown Concept: {concept_id}")
            stable_key = concept.stable_key
        with self._concept_lock(stable_key):
            return self._accept_atomic_note_locked(
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

        with self.db.session() as session:
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
                and self._release_contains_contribution(
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
        return self._run_concept_update_job_locked(job_id)

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
        with self.db.session() as session:
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
                remaining = session.scalar(
                    select(func.count(ConceptContribution.id)).where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.id != contribution.id,
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                ) or 0
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
                supersedes_contribution_id=(
                    contribution.id if operation == "REPLACE" else None
                ),
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
        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            if job.status != "FAILED":
                raise ValueError("Only FAILED Concept update jobs can be retried")
            contribution = session.get(ConceptContribution, job.contribution_id)
            if contribution is None or contribution.decision_status not in {
                "APPROVED", "RETRACT_PENDING"
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
        return self.get_concept_update_job(job_id)

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        """Execute or recover one durable Concept update claimed by the Worker."""

        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            concept = session.get(Concept, job.concept_id) if job else None
            if job is None or concept is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            stable_key = concept.stable_key
        with self._concept_lock(stable_key):
            return self._run_concept_update_job_locked(job_id)

    def _run_concept_update_job_locked(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
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
                prepared = self._prepared_from_contributions(
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
                result = self._compile_module_update_candidate(
                    prepared,
                    concept_id=concept_id,
                    base_version_id=expected_concept_version_id,
                    job_id=job_id,
                )
                candidate_id = result["concept_version_id"]
                with self.db.session() as session:
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
                    expected_status = (
                        "RETRACT_PENDING" if operation == "REMOVE" else "APPROVED"
                    )
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

            refreshed = self._refresh_synthesis_releases(
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
            with self.db.session() as session:
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
                with self.db.session() as session:
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
            with self.db.session() as session:
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

    @staticmethod
    def _release_contains_contribution(
        session,
        concept_version_id: str | None,
        module_id: str,
        contribution_id: str,
    ) -> bool:
        if not concept_version_id:
            return False
        placement = session.scalar(
            select(ConceptReleaseModule).where(
                ConceptReleaseModule.concept_version_id == concept_version_id,
                ConceptReleaseModule.module_id == module_id,
            )
        )
        if placement is None:
            return False
        module_version = session.get(ConceptModuleVersion, placement.module_version_id)
        if module_version is None:
            return False
        payload = _loads(module_version.payload_json, {})
        return any(
            isinstance(item, dict) and item.get("contribution_id") == contribution_id
            for item in payload.get("_contributions", [])
        )

    @staticmethod
    def _validate_update_draft(
        draft: SemanticConceptDraft,
        module_seed: dict[str, Any],
    ) -> None:
        if len(draft.modules) != 1 or draft.modules[0].block_key != module_seed["block_key"]:
            raise ValueError("Concept candidate must contain exactly the requested module")
        allowed = {
            str(source.get("contribution_id") or "")
            for source in module_seed["sources"]
            if source.get("contribution_id")
        }
        allowed_note_blocks: set[str] = set()
        allowed_spans: dict[str, str] = {}
        allowed_assets: dict[str, str] = {}
        for source in module_seed["sources"]:
            for source_block in source.get("blocks", []):
                note_block_id = str(source_block.get("note_block_id") or "")
                if not note_block_id:
                    continue
                allowed_note_blocks.add(note_block_id)
                for evidence in source_block.get("evidence", []):
                    span_id = str(evidence.get("evidence_span_id") or "")
                    if span_id:
                        allowed_spans[span_id] = note_block_id
                for asset_id in source_block.get("asset_ids", []):
                    allowed_assets[str(asset_id)] = note_block_id
        blocks = draft.modules[0].payload.blocks
        if not blocks:
            raise ValueError("Concept candidate module has no content blocks")
        for block in blocks:
            evidence = set(block.evidence_refs)
            if not evidence:
                raise ValueError(f"Concept candidate block has no evidence_refs: {block.block_key}")
            if not evidence <= allowed:
                raise ValueError(
                    f"Concept candidate block has unknown evidence_refs: {block.block_key}"
                )
            note_block_ids = set(block.note_block_ids)
            span_ids = set(block.evidence_span_ids)
            if not note_block_ids or not note_block_ids <= allowed_note_blocks:
                raise ValueError(
                    f"Concept candidate block has invalid note_block_ids: {block.block_key}"
                )
            if not span_ids or not span_ids <= set(allowed_spans):
                raise ValueError(
                    f"Concept candidate block has invalid evidence_span_ids: {block.block_key}"
                )
            if any(allowed_spans[span_id] not in note_block_ids for span_id in span_ids):
                raise ValueError(
                    f"Concept candidate evidence does not belong to selected NoteBlocks: {block.block_key}"
                )
            if not set(block.asset_ids) <= set(allowed_assets):
                raise ValueError(f"Concept candidate block has unknown asset_ids: {block.block_key}")
            if any(
                allowed_assets[asset_id] not in note_block_ids for asset_id in block.asset_ids
            ):
                raise ValueError(
                    f"Concept candidate asset does not belong to selected NoteBlocks: {block.block_key}"
                )

    def _update_context(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            pin = session.scalar(select(ConceptReleaseModule).where(
                ConceptReleaseModule.concept_version_id == job.expected_concept_version_id,
                ConceptReleaseModule.module_id == job.module_id))
            mv = session.get(ConceptModuleVersion, pin.module_version_id) if pin else None
            return {"request": job.rationale, "previous_module": mv.plain_text[:12000] if mv else "",
                    "rule": "保留仍由有效来源支持的重要知识；移除失效来源独有内容；检查适用条件与冲突。"}

    def _judge_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        job_id: str,
        attempt_no: int,
    ) -> ConceptModuleJudgeResult:
        """Review only the changed module against the evidence visible to its writer."""

        allowed_evidence = {
            str(source.get("contribution_id") or "")
            for source in module_seed["sources"]
            if source.get("contribution_id")
        }
        block_keys = {item.block_key for item in module_draft.payload.blocks}
        span_owners = {e["evidence_span_id"]: source["contribution_id"]
                       for source in module_seed["sources"] for b in source.get("blocks", []) for e in b.get("evidence", [])}
        judge_input = {
            "concept": {
                "canonical_key": self.blueprint.stable_concept_key(spec),
                "title": spec["title"],
            },
            "module": module_draft.model_dump(),
            "allowed_block_keys": sorted(block_keys),
            "allowed_contribution_ids": sorted(allowed_evidence),
            "allowed_evidence_span_ids": sorted(span_owners),
            "update_context": self._update_context(job_id),
            "evidence": [
                {
                    "contribution_id": source.get("contribution_id", ""),
                    "pages": source.get("pages", []),
                    "text": self._source_text(source, max_chars=3_000),
                    "blocks": source.get("blocks", []),
                }
                for source in module_seed["sources"]
            ],
        }
        input_hash = _sha256_text(_json(judge_input))
        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "JUDGING" if attempt_no == 1 else "REJUDGING"

        raw = self.agent.structured_json(
            system=(
                "你是独立的 Concept Module 质量审校员，不参与正文撰写。只根据给出的 evidence 审核候选 Module。"
                "重点检查：每个事实是否被引用证据支持；术语是否准确；公式和符号是否正确；是否把平移等变性误写为"
                "平移不变性；不同来源冲突是否显式保留；是否重复、冗长或难读。不得补充外部知识来替候选辩护。"
                "输出严格 JSON：{decision,summary,issues,coverage}。decision 只能为 PASS、REPAIR、REJECT。"
                "issues 每项为 {issue_id,severity,category,block_key,claim,message,evidence_refs,suggested_action}；"
                "severity 只能为 BLOCKER、MAJOR、MINOR；category 只能为 EVIDENCE_GAP、FACTUAL_ERROR、"
                "TERMINOLOGY、FORMULA、SOURCE_CONFLICT、READABILITY、DUPLICATION。"
                "BLOCKER 为严重事实错配、不可支持或隐去冲突；MAJOR 为错误公式、错误术语、实质遗漏或无效引用；MINOR 仅为措辞、轻微重复和排版建议。"
                "只有 MINOR 或无问题时用 PASS 并保留建议；有 MAJOR 用 REPAIR；有不可修复 BLOCKER 用 REJECT。"
                "审查 note_block_ids 应对照 evidence.blocks.note_block_id，不应对照 contribution IDs。"
                "引用粒度为内容 Block：evidence_refs 是本块来源的并集，每项事实至少有一条所选来源支持，每条引用至少支持本块一项事实；不要求每条来源支持本块每一句话。"
                "来源明确支持的等价公式与计数重表达不算事实错误；存在单位歧义时要求说明口径。"
                "已有模块在当前有效来源支持下保留事实是必要行为，不将与 previous_module 重复本身判为错误；不能引入外部教材知识。"
            ),
            user=judge_input,
            operation="semantic_synthesis.judge_concept_module_v1",
            prompt_version=JUDGE_PROMPT_VERSION,
            use_pro_model=False,
        )
        def validate_review(value):
            if isinstance(value, dict):
                for issue in value.get("issues", []):
                    refs = issue.get("evidence_refs", [])
                    spans = list(issue.get("evidence_span_ids", []))
                    spans.extend(ref for ref in refs if ref in span_owners)
                    issue["evidence_span_ids"] = list(dict.fromkeys(spans))
                    issue["evidence_refs"] = list(dict.fromkeys(span_owners.get(ref, ref) for ref in refs))
            checked = ConceptModuleJudgeResult.model_validate(value)
            for issue in checked.issues:
                if issue.block_key and issue.block_key not in block_keys:
                    raise ValueError(f"Judge referenced unknown block_key: {issue.block_key}")
                if not set(issue.evidence_refs) <= allowed_evidence or not set(issue.evidence_span_ids) <= set(span_owners):
                    raise ValueError(f"Judge referenced unknown evidence: {issue.issue_id}")
            return checked

        try:
            try:
                result = validate_review(raw)
            except (ValidationError, ValueError) as first_error:
                raw = self.agent.structured_json(
                    system="修正审校 JSON 的引用或枚举结构，不改变质量标准。block_key 仅使用 allowed_block_keys，evidence_refs 用 contribution IDs，evidence_span_ids 用 span IDs。decision/严重级别必须与问题一致。输出合法审校 JSON。",
                    user={**judge_input, "previous_review": raw, "schema_error": str(first_error)},
                    operation="semantic_synthesis.judge_concept_module_schema_repair", prompt_version=JUDGE_PROMPT_VERSION,
                    use_pro_model=False)
                result = validate_review(raw)
        except (ValidationError, ValueError) as exc:
            result = ConceptModuleJudgeResult(
                decision="REJECT",
                summary="Judge 输出无法通过结构化校验，按 fail-closed 策略拒绝发布。",
                issues=[
                    {
                        "issue_id": "judge-output-invalid",
                        "severity": "BLOCKER",
                        "category": "FACTUAL_ERROR",
                        "message": str(exc)[:2_000],
                        "suggested_action": "修复 Judge 输出或重试。",
                    }
                ],
                coverage={"validated": False},
            )
        output_json = _json(result.model_dump())
        with self.db.session() as session:
            session.add(
                ConceptModuleJudgeRecord(
                    concept_update_job_id=job_id,
                    attempt_no=attempt_no,
                    judge_model=self.agent.name,
                    prompt_version=JUDGE_PROMPT_VERSION,
                    input_sha256=input_hash,
                    output_sha256=_sha256_text(output_json),
                    decision=result.decision,
                    summary=result.summary,
                    issues_json=_json([item.model_dump() for item in result.issues]),
                )
            )
        return result

    def _repair_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        judge: ConceptModuleJudgeResult,
        job_id: str,
    ) -> SemanticConceptModuleDraft:
        """Repair one changed module once; never regenerate the complete Concept."""

        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "REPAIRING"
        raw = self.agent.structured_json(
            system=(
                "你是 Concept Module 局部修复器。只修改 Judge 指出的 Block，保持 module block_key 不变，"
                "不得新增输入证据之外的事实。输出严格 JSON：{module:{block_key,title,render_type,semantic_role,"
                "payload:{blocks:[...]}}}。每个内容 Block 必须保留合法 evidence_refs。"
            ),
            user={
                "concept": {
                    "canonical_key": self.blueprint.stable_concept_key(spec),
                    "title": spec["title"],
                },
                "module": module_draft.model_dump(),
                "issues": [item.model_dump() for item in judge.issues],
                "evidence": [
                    {
                        "contribution_id": source.get("contribution_id", ""),
                        "pages": source.get("pages", []),
                        "text": self._source_text(source, max_chars=3_000),
                        "blocks": source.get("blocks", []),
                    }
                    for source in module_seed["sources"]
                ],
            },
            operation="semantic_synthesis.repair_concept_module_v1",
            prompt_version=REPAIR_PROMPT_VERSION,
            use_pro_model=True,
        )
        candidate = raw.get("module", raw) if isinstance(raw, dict) else raw
        try:
            repaired = SemanticConceptModuleDraft.model_validate(candidate)
        except ValidationError as exc:
            candidate = self._repair_module_schema(candidate, module_seed, str(exc))
            repaired = SemanticConceptModuleDraft.model_validate(candidate)
        repaired.block_key = module_draft.block_key
        repaired.semantic_role = module_draft.semantic_role
        return repaired

    def _repair_module_schema(self, candidate, module_seed, detail):
        raw = self.agent.structured_json(
            system="仅纠正候选模块 JSON 的结构与引用字段，不增补事实，不改变模块 key。evidence_refs 只能使用 contribution_id；note_block_ids 只能使用对应 note_block_id；evidence_span_ids 必须属于所选 note_block；asset_ids 必须属于所选块，ASSET 必须包含合法 asset_ids。删除没有真实支持的内容，不能编造 ID。输出 {module:完整模块}。",
            user={"module": candidate, "validation_error": detail, "sources": module_seed["sources"]},
            operation="semantic_synthesis.module_schema_repair", prompt_version="module-schema-repair-v1", use_pro_model=False)
        return raw.get("module", raw) if isinstance(raw, dict) else raw

    def _align_declared_provenance(self, draft, module_seed):
        # Follow declared, real span/asset IDs to their authoritative parents.
        # Unknown IDs remain untouched and fail strict validation.
        spans, assets, blocks = {}, {}, {}
        for source in module_seed["sources"]:
            for block in source.get("blocks", []):
                bid = block.get("note_block_id")
                if not bid:
                    continue
                blocks[bid] = source.get("contribution_id")
                for evidence in block.get("evidence", []):
                    spans[evidence.get("evidence_span_id")] = bid
                for asset in block.get("asset_ids", []):
                    assets[asset] = bid
        for block in draft.modules[0].payload.blocks:
            if (block.evidence_span_ids and set(block.evidence_span_ids) <= set(spans)
                and set(block.asset_ids) <= set(assets) and set(block.note_block_ids) <= set(blocks)
                and set(block.evidence_refs) <= set(blocks.values())):
                parents = {spans[sid] for sid in block.evidence_span_ids} | {assets[aid] for aid in block.asset_ids}
                block.note_block_ids = sorted(parents)
                block.evidence_refs = sorted({blocks[bid] for bid in parents})

    def _validate_or_repair_update_draft(self, draft, module_seed):
        self._align_declared_provenance(draft, module_seed)
        try:
            self._validate_update_draft(draft, module_seed)
        except ValueError as exc:
            value = self._repair_module_schema(draft.modules[0].model_dump(), module_seed, str(exc))
            draft.modules[0] = SemanticConceptModuleDraft.model_validate(value)
            self._align_declared_provenance(draft, module_seed)
            self._validate_update_draft(draft, module_seed)

    def _compile_module_update_candidate(
        self,
        prepared: dict[str, Any],
        *,
        concept_id: str,
        base_version_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        """Generate and persist one READY module-level candidate without moving heads."""

        spec: ConceptSpec = prepared["spec"]
        modules: list[dict[str, Any]] = prepared["modules"]
        if len(modules) != 1:
            raise ValueError("Reliable Concept updates must target exactly one module")
        module_seed = modules[0]
        input_hash = _sha256_text(
            _json(
                {
                    "base_version_id": base_version_id,
                    "concept_id": concept_id,
                    "module_key": module_seed["block_key"],
                    "sources": sorted(
                        (
                            source["note_version_id"],
                            source["content_sha256"],
                            source.get("contribution_role", "FOUNDATION"),
                            _json(source.get("selected_block_ids", [])),
                        )
                        for source in module_seed["sources"]
                    ),
                }
            )
        )
        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "GENERATING"
            job.input_sha256 = input_hash
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept_id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                    ConceptBuildRecord.result_status == "JUDGED",
                    ConceptBuildRecord.fallback_used.is_(False),
                )
            )
            if record is not None:
                candidate = session.get(ConceptVersion, record.concept_version_id)
                if candidate is not None and candidate.status in {"READY", "PUBLISHED"}:
                    job.status = "READY"
                    job.candidate_concept_version_id = candidate.id
                    return {
                        "concept_id": concept_id,
                        "concept_version_id": candidate.id,
                        "reused": True,
                        "fallback": False,
                    }

        draft, fallback, fallback_detail = self._write_draft(spec, modules, self._update_context(job_id))
        if fallback:
            with self.db.session() as session:
                job = session.get(ConceptUpdateJob, job_id)
                job.fallback_used = True
                job.fallback_detail = fallback_detail
            raise ValueError(
                f"Concept candidate rejected because generation used fallback: {fallback_detail}"
            )
        self._validate_or_repair_update_draft(draft, module_seed)
        judge = self._judge_module_candidate(
            spec=spec,
            module_seed=module_seed,
            module_draft=draft.modules[0],
            job_id=job_id,
            attempt_no=1,
        )
        if judge.decision == "REPAIR":
            draft.modules[0] = self._repair_module_candidate(
                spec=spec,
                module_seed=module_seed,
                module_draft=draft.modules[0],
                judge=judge,
                job_id=job_id,
            )
            self._validate_or_repair_update_draft(draft, module_seed)
            judge = self._judge_module_candidate(
                spec=spec,
                module_seed=module_seed,
                module_draft=draft.modules[0],
                job_id=job_id,
                attempt_no=2,
            )
        if judge.decision != "PASS":
            if any(issue.issue_id == "judge-output-invalid" for issue in judge.issues):
                raise ValueError("Judge output schema invalid; retry model response")
            raise ModuleReviewRequired(
                f"Concept candidate rejected by independent Module Judge: {judge.summary}"
            )

        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            base = session.get(ConceptVersion, base_version_id)
            job = session.get(ConceptUpdateJob, job_id)
            if (
                concept is None
                or base is None
                or base.concept_id != concept.id
                or concept.active_version_id != base.id
            ):
                raise ValueError(
                    "Concept version conflict: candidate was not persisted; reload and retry"
                )
            target_module = session.scalar(
                select(ConceptModule).where(
                    ConceptModule.concept_id == concept.id,
                    ConceptModule.module_key == module_seed["block_key"],
                    ConceptModule.visibility == "ACTIVE",
                )
            )
            if target_module is None:
                raise ValueError("Target Concept module disappeared during generation")
            module_draft = draft.modules[0]
            note_version_ids = [item["note_version_id"] for item in module_seed["sources"]]
            contribution_snapshot = [
                {
                    "contribution_id": item.get("contribution_id", ""),
                    "note_version_id": item["note_version_id"],
                    "source_ref": item.get("source_ref", ""),
                    "role": item.get("contribution_role", "FOUNDATION"),
                    "selected_block_ids": item.get("selected_block_ids", []),
                }
                for item in module_seed["sources"]
            ]
            evidence_lookup: dict[str, tuple[str, str]] = {}
            selected_asset_blocks: dict[str, str] = {}
            for source in module_seed["sources"]:
                contribution_id = str(source.get("contribution_id") or "")
                for source_block in source.get("blocks", []):
                    note_block_id = str(source_block.get("note_block_id") or "")
                    for evidence in source_block.get("evidence", []):
                        span_id = str(evidence.get("evidence_span_id") or "")
                        if span_id:
                            evidence_lookup[span_id] = (contribution_id, note_block_id)
            for content_block in module_draft.payload.blocks:
                for asset_id in content_block.asset_ids:
                    selected_asset_blocks[asset_id] = content_block.block_key
            selected_asset_ids = set(selected_asset_blocks)
            assets = (
                session.execute(
                    select(NoteAsset, VisualAsset, BlobObject)
                    .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                    .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
                    .where(
                        NoteAsset.note_version_id.in_(note_version_ids),
                        NoteAsset.visual_asset_id.in_(selected_asset_ids),
                    )
                    .order_by(NoteAsset.order_index)
                ).all()
                if note_version_ids
                else []
            )
            unique_assets: list[tuple[NoteAsset, VisualAsset, BlobObject]] = []
            seen_assets: set[str] = set()
            for row in assets:
                if row[1].id not in seen_assets:
                    seen_assets.add(row[1].id)
                    unique_assets.append(row)
            stored_payload = module_draft.payload.model_dump()
            stored_payload["_contributions"] = contribution_snapshot
            module_hash = _sha256_text(
                _json(
                    {
                        "draft": module_draft.model_dump(),
                        "contributions": contribution_snapshot,
                        "asset_ids": [row[1].id for row in unique_assets],
                    }
                )
            )
            module_version = session.scalar(
                select(ConceptModuleVersion).where(
                    ConceptModuleVersion.module_id == target_module.id,
                    ConceptModuleVersion.content_sha256 == module_hash,
                )
            )
            if module_version is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptModuleVersion.version_no)).where(
                            ConceptModuleVersion.module_id == target_module.id
                        )
                    )
                    or 0
                ) + 1
                module_version = ConceptModuleVersion(
                    module_id=target_module.id,
                    version_no=version_no,
                    render_type=module_draft.render_type,
                    title=module_draft.title,
                    payload_json=_json(stored_payload),
                    plain_text=module_draft.plain_text(),
                    origin="ATOMIC_SYNTHESIZED",
                    lock_state="OPEN",
                    content_sha256=module_hash,
                    created_by=self.agent.name,
                )
                session.add(module_version)
                session.flush()
            for content_block in module_draft.payload.blocks:
                for span_id in content_block.evidence_span_ids:
                    contribution_id, note_block_id = evidence_lookup[span_id]
                    existing_evidence = session.scalar(
                        select(ConceptContentEvidence).where(
                            ConceptContentEvidence.module_version_id == module_version.id,
                            ConceptContentEvidence.content_block_key == content_block.block_key,
                            ConceptContentEvidence.evidence_span_id == span_id,
                        )
                    )
                    if existing_evidence is None:
                        session.add(
                            ConceptContentEvidence(
                                module_version_id=module_version.id,
                                content_block_key=content_block.block_key,
                                contribution_id=contribution_id,
                                note_block_id=note_block_id,
                                evidence_span_id=span_id,
                            )
                        )
            for order, (note_asset, visual, _blob) in enumerate(unique_assets):
                placement = session.scalar(
                    select(ConceptModuleAsset).where(
                        ConceptModuleAsset.module_version_id == module_version.id,
                        ConceptModuleAsset.visual_asset_id == visual.id,
                    )
                )
                if placement is None:
                    session.add(
                        ConceptModuleAsset(
                            module_version_id=module_version.id,
                            visual_asset_id=visual.id,
                            content_block_key=selected_asset_blocks[visual.id],
                            order_index=order,
                            placement=note_asset.placement,
                            caption=visual.caption,
                            alt_text=note_asset.alt_text,
                        )
                    )
                else:
                    placement.content_block_key = selected_asset_blocks[visual.id]

            old_placements = session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == base.id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all()
            release_modules = [
                (
                    item.module_id,
                    module_version.id
                    if item.module_id == target_module.id
                    else item.module_version_id,
                    item.sort_order,
                )
                for item in old_placements
            ]
            if not any(mid == target_module.id for mid, _vid, _order in release_modules):
                if job.operation != "RECONCILE":
                    raise ValueError("Target module is not pinned by the expected Concept version")
                release_modules.append((target_module.id, module_version.id, max([r[2] for r in release_modules], default=-1) + 1))
            release_hash = _sha256_text(
                _json(
                    {
                        "title": base.title,
                        "summary": base.summary,
                        "scope": base.scope,
                        "modules": release_modules,
                    }
                )
            )
            candidate = session.scalar(
                select(ConceptVersion).where(
                    ConceptVersion.concept_id == concept.id,
                    ConceptVersion.content_sha256 == release_hash,
                    ConceptVersion.schema_version == SCHEMA_VERSION,
                )
            )
            if candidate is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptVersion.version_no)).where(
                            ConceptVersion.concept_id == concept.id
                        )
                    )
                    or 0
                ) + 1
                candidate = ConceptVersion(
                    concept_id=concept.id,
                    version_no=version_no,
                    title=base.title,
                    original_title=base.original_title,
                    scope=base.scope,
                    summary=base.summary,
                    content_sha256=release_hash,
                    schema_version=SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    status="READY",
                )
                session.add(candidate)
                session.flush()
                for module_id, module_version_id, sort_order in release_modules:
                    session.add(
                        ConceptReleaseModule(
                            concept_version_id=candidate.id,
                            module_id=module_id,
                            module_version_id=module_version_id,
                            sort_order=sort_order,
                        )
                    )
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept.id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                )
            )
            if record is None:
                record = ConceptBuildRecord(
                    concept_id=concept.id,
                    concept_version_id=candidate.id,
                    input_sha256=input_hash,
                    prompt_version=PROMPT_VERSION,
                    model_name=self.agent.name,
                )
                session.add(record)
            record.concept_version_id = candidate.id
            record.result_status = "JUDGED"
            record.fallback_used = False
            record.fallback_detail = ""
            record.output_sha256 = candidate.content_sha256
            job.status = "READY"
            job.input_sha256 = input_hash
            job.candidate_concept_version_id = candidate.id
            for judge_record in session.scalars(
                select(ConceptModuleJudgeRecord).where(
                    ConceptModuleJudgeRecord.concept_update_job_id == job.id
                )
            ):
                judge_record.candidate_concept_version_id = candidate.id
                judge_record.module_version_id = module_version.id
            return {
                "concept_id": concept.id,
                "concept_version_id": candidate.id,
                "reused": False,
                "fallback": False,
            }

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            module = session.get(ConceptModule, job.module_id)
            judges = session.scalars(
                select(ConceptModuleJudgeRecord)
                .where(ConceptModuleJudgeRecord.concept_update_job_id == job.id)
                .order_by(ConceptModuleJudgeRecord.attempt_no)
            ).all()
            return {
                "job_id": job.id,
                "status": job.status,
                "concept_id": job.concept_id,
                "module_key": module.module_key if module else "",
                "note_version_id": job.atomic_note_version_id,
                "contribution_id": job.contribution_id,
                "operation": job.operation,
                "supersedes_contribution_id": job.supersedes_contribution_id,
                "expected_concept_version_id": job.expected_concept_version_id,
                "candidate_concept_version_id": job.candidate_concept_version_id,
                "input_sha256": job.input_sha256,
                "fallback_used": job.fallback_used,
                "fallback_detail": job.fallback_detail,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
                "attempt_count": job.attempt_count,
                "lease_owner": job.lease_owner,
                "lease_expires_at": (
                    job.lease_expires_at.isoformat() if job.lease_expires_at else None
                ),
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
                "completed_at": (job.completed_at.isoformat() if job.completed_at else None),
                "judges": [
                    {
                        "judge_id": item.id,
                        "attempt_no": item.attempt_no,
                        "decision": item.decision,
                        "summary": item.summary,
                        "issues": _loads(item.issues_json, []),
                        "input_sha256": item.input_sha256,
                        "output_sha256": item.output_sha256,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in judges
                ],
            }

    def list_concept_update_jobs(
        self, concept_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown Concept: {concept_id}")
            ids = session.scalars(
                select(ConceptUpdateJob.id)
                .where(ConceptUpdateJob.concept_id == concept_id)
                .order_by(ConceptUpdateJob.created_at.desc())
                .limit(max(1, min(limit, 50)))
            ).all()
        return [self.get_concept_update_job(job_id) for job_id in ids]

    def list_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown Concept: {concept_id}")
            statement = (
                select(ConceptContribution, ConceptModule, NoteVersion)
                .join(ConceptModule, ConceptModule.id == ConceptContribution.module_id)
                .outerjoin(
                    NoteVersion,
                    NoteVersion.id == ConceptContribution.atomic_note_version_id,
                )
                .where(ConceptModule.concept_id == concept_id)
                .order_by(ConceptModule.module_key, ConceptContribution.created_at)
            )
            if module_key:
                statement = statement.where(ConceptModule.module_key == module_key)
            rows = session.execute(statement).all()
            return [
                {
                    "contribution_id": contribution.id,
                    "module_key": module.module_key,
                    "note_version_id": contribution.atomic_note_version_id,
                    "note_title": version.title if version else "",
                    "contribution_role": contribution.contribution_role,
                    "decision_status": contribution.decision_status,
                    "rationale": contribution.rationale,
                    "assigned_by": contribution.assigned_by,
                    "superseded_by_id": contribution.superseded_by_id,
                    "created_at": contribution.created_at.isoformat(),
                    "updated_at": (
                        contribution.updated_at.isoformat()
                        if contribution.updated_at
                        else contribution.created_at.isoformat()
                    ),
                }
                for contribution, module, version in rows
            ]

    def _compile_one(self, prepared: dict[str, Any]) -> dict[str, Any]:
        stable_key = self.blueprint.stable_concept_key(prepared["spec"])
        with self._concept_lock(stable_key):
            return self._compile_one_locked(prepared)

    def _compile_one_locked(self, prepared: dict[str, Any]) -> dict[str, Any]:
        spec: ConceptSpec = prepared["spec"]
        modules: list[dict[str, Any]] = prepared["modules"]
        if not modules:
            raise ValueError(
                f"No source modules mapped to {self.blueprint.stable_concept_key(spec)}"
            )
        input_payload = {
            "spec": spec,
            "modules": [
                {
                    "block_key": item["block_key"],
                    "semantic_role": item["semantic_role"],
                    "sources": sorted(
                        (
                            source["note_version_id"],
                            source["content_sha256"],
                            source.get("contribution_role", "FOUNDATION"),
                        )
                        for source in item["sources"]
                    ),
                }
                for item in modules
            ],
        }
        input_hash = _sha256_text(_json(input_payload))
        with self.db.session() as session:
            concept = session.scalar(
                select(Concept).where(Concept.stable_key == self.blueprint.stable_concept_key(spec))
            )
            record = session.scalar(
                select(ConceptBuildRecord).where(
                    ConceptBuildRecord.concept_id == concept.id,
                    ConceptBuildRecord.input_sha256 == input_hash,
                    ConceptBuildRecord.prompt_version == PROMPT_VERSION,
                )
            )
            if record is not None:
                concept.active_version_id = record.concept_version_id
                concept.status = "PUBLISHED"
                return {
                    "concept_id": concept.id,
                    "concept_version_id": record.concept_version_id,
                    "reused": True,
                    "fallback": False,
                    "fallback_detail": "",
                }

        draft, fallback, fallback_detail = self._write_draft(spec, modules)
        with self.db.session() as session:
            concept = session.scalar(
                select(Concept).where(Concept.stable_key == self.blueprint.stable_concept_key(spec))
            )
            module_versions: list[tuple[ConceptModule, ConceptModuleVersion]] = []
            contribution_count = 0
            for module_draft, seed in zip(draft.modules, modules, strict=True):
                module = session.scalar(
                    select(ConceptModule).where(
                        ConceptModule.concept_id == concept.id,
                        ConceptModule.module_key == module_draft.block_key,
                    )
                )
                if module is None:
                    module = ConceptModule(
                        concept_id=concept.id,
                        module_key=module_draft.block_key,
                        semantic_role=module_draft.semantic_role,
                    )
                    session.add(module)
                    session.flush()
                else:
                    module.semantic_role = module_draft.semantic_role
                    module.visibility = "ACTIVE"
                note_version_ids = [item["note_version_id"] for item in seed["sources"]]
                contribution_snapshot = [
                    {
                        "contribution_id": item.get("contribution_id", ""),
                        "note_version_id": item["note_version_id"],
                        "source_ref": item.get("source_ref", ""),
                        "role": item.get("contribution_role", "FOUNDATION"),
                    }
                    for item in seed["sources"]
                ]
                contribution_count += len(contribution_snapshot)
                assets = (
                    session.execute(
                        select(NoteAsset, VisualAsset, BlobObject)
                        .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                        .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
                        .where(NoteAsset.note_version_id.in_(note_version_ids))
                        .order_by(NoteAsset.order_index)
                    ).all()
                    if note_version_ids
                    else []
                )
                unique_assets: list[tuple[NoteAsset, VisualAsset, BlobObject]] = []
                seen_assets: set[str] = set()
                for row in assets:
                    if row[1].id in seen_assets:
                        continue
                    seen_assets.add(row[1].id)
                    unique_assets.append(row)
                stored_payload = module_draft.payload.model_dump()
                stored_payload["_contributions"] = contribution_snapshot
                module_hash = _sha256_text(
                    _json(
                        {
                            "draft": module_draft.model_dump(),
                            "contributions": contribution_snapshot,
                            "asset_ids": [row[1].id for row in unique_assets],
                        }
                    )
                )
                module_version = session.scalar(
                    select(ConceptModuleVersion).where(
                        ConceptModuleVersion.module_id == module.id,
                        ConceptModuleVersion.content_sha256 == module_hash,
                    )
                )
                if module_version is None:
                    version_no = (
                        session.scalar(
                            select(func.max(ConceptModuleVersion.version_no)).where(
                                ConceptModuleVersion.module_id == module.id
                            )
                        )
                        or 0
                    ) + 1
                    module_version = ConceptModuleVersion(
                        module_id=module.id,
                        version_no=version_no,
                        render_type=module_draft.render_type,
                        title=module_draft.title,
                        payload_json=_json(stored_payload),
                        plain_text=module_draft.plain_text(),
                        origin="ATOMIC_SYNTHESIZED",
                        lock_state="OPEN",
                        content_sha256=module_hash,
                        created_by=self.agent.name,
                    )
                    session.add(module_version)
                    session.flush()
                module_versions.append((module, module_version))
                for order, (note_asset, visual, _blob) in enumerate(unique_assets):
                    placement = session.scalar(
                        select(ConceptModuleAsset).where(
                            ConceptModuleAsset.module_version_id == module_version.id,
                            ConceptModuleAsset.visual_asset_id == visual.id,
                        )
                    )
                    if placement is None:
                        session.add(
                            ConceptModuleAsset(
                                module_version_id=module_version.id,
                                visual_asset_id=visual.id,
                                order_index=order,
                                placement=note_asset.placement,
                                caption=visual.caption,
                                alt_text=note_asset.alt_text,
                            )
                        )

            release_hash = _sha256_text(
                _json(
                    {
                        "title": draft.title_zh,
                        "summary": draft.summary,
                        "scope": draft.scope.model_dump(),
                        "modules": [item.id for _module, item in module_versions],
                    }
                )
            )
            release = session.scalar(
                select(ConceptVersion).where(
                    ConceptVersion.concept_id == concept.id,
                    ConceptVersion.content_sha256 == release_hash,
                    ConceptVersion.schema_version == SCHEMA_VERSION,
                )
            )
            if release is None:
                version_no = (
                    session.scalar(
                        select(func.max(ConceptVersion.version_no)).where(
                            ConceptVersion.concept_id == concept.id
                        )
                    )
                    or 0
                ) + 1
                release = ConceptVersion(
                    concept_id=concept.id,
                    version_no=version_no,
                    title=draft.title_zh,
                    original_title=spec["title"],
                    scope=_json(draft.scope.model_dump()),
                    summary=draft.summary,
                    content_sha256=release_hash,
                    schema_version=SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    status="PUBLISHED",
                )
                session.add(release)
                session.flush()
                for order, (module, module_version) in enumerate(module_versions):
                    session.add(
                        ConceptReleaseModule(
                            concept_version_id=release.id,
                            module_id=module.id,
                            module_version_id=module_version.id,
                            sort_order=order,
                        )
                    )
            concept.canonical_name = draft.title_zh
            concept.original_name = spec["title"]
            concept.active_version_id = release.id
            concept.status = "PUBLISHED"
            concept.visibility = "ACTIVE"
            session.add(
                ConceptBuildRecord(
                    concept_id=concept.id,
                    concept_version_id=release.id,
                    input_sha256=input_hash,
                    prompt_version=PROMPT_VERSION,
                    model_name=self.agent.name,
                )
            )
            return {
                "concept_id": concept.id,
                "concept_version_id": release.id,
                "reused": False,
                "fallback": fallback,
                "fallback_detail": fallback_detail,
                "contributions": contribution_count,
            }

    def _refresh_synthesis_releases(
        self,
        concept_versions: dict[str, str],
        *,
        source_version_id: str | None = None,
        source_role: str = "SUPPORT",
        exclude_synthesis_id: str | None = None,
    ) -> list[str]:
        """Clone active releases that reference an updated global Concept."""

        refreshed: list[str] = []
        if not concept_versions:
            return refreshed
        with self.db.session() as session:
            syntheses = session.scalars(
                select(Synthesis).where(
                    Synthesis.visibility == "ACTIVE",
                    Synthesis.active_version_id.is_not(None),
                )
            ).all()
            for synthesis in syntheses:
                if synthesis.id == exclude_synthesis_id:
                    continue
                old_version = session.get(SynthesisVersion, synthesis.active_version_id)
                if old_version is None:
                    continue
                old_nodes = session.scalars(
                    select(SynthesisNode)
                    .where(SynthesisNode.synthesis_version_id == old_version.id)
                    .order_by(SynthesisNode.sort_order)
                ).all()
                if not any(node.concept_id in concept_versions for node in old_nodes):
                    continue
                target_versions = {
                    node.id: concept_versions.get(node.concept_id, node.concept_version_id)
                    for node in old_nodes
                }
                if all(target_versions[node.id] == node.concept_version_id for node in old_nodes):
                    continue
                release_hash = _sha256_text(
                    _json(
                        [
                            (
                                node.stable_key,
                                node.parent_id,
                                target_versions[node.id],
                                node.sort_order,
                            )
                            for node in old_nodes
                        ]
                    )
                )
                existing = session.scalar(
                    select(SynthesisVersion).where(
                        SynthesisVersion.synthesis_id == synthesis.id,
                        SynthesisVersion.content_sha256 == release_hash,
                    )
                )
                if existing is not None:
                    synthesis.active_version_id = existing.id
                    refreshed.append(existing.id)
                    continue
                version_no = (
                    session.scalar(
                        select(func.max(SynthesisVersion.version_no)).where(
                            SynthesisVersion.synthesis_id == synthesis.id
                        )
                    )
                    or 0
                ) + 1
                new_version = SynthesisVersion(
                    synthesis_id=synthesis.id,
                    backbone_corpus_id=old_version.backbone_corpus_id,
                    version_no=version_no,
                    title=old_version.title,
                    summary=old_version.summary,
                    status="PUBLISHED",
                    coverage_json=old_version.coverage_json,
                    content_sha256=release_hash,
                )
                session.add(new_version)
                session.flush()
                node_ids: dict[str, str] = {}
                for old_node in old_nodes:
                    new_node = SynthesisNode(
                        synthesis_version_id=new_version.id,
                        stable_key=old_node.stable_key,
                        parent_id=node_ids.get(old_node.parent_id or ""),
                        title=old_node.title,
                        original_title=old_node.original_title,
                        level=old_node.level,
                        sort_order=old_node.sort_order,
                        node_kind=old_node.node_kind,
                        concept_id=old_node.concept_id,
                        concept_version_id=target_versions[old_node.id],
                        source_locator_json=old_node.source_locator_json,
                    )
                    session.add(new_node)
                    session.flush()
                    node_ids[old_node.id] = new_node.id
                references = session.scalars(
                    select(SynthesisReferenceSource).where(
                        SynthesisReferenceSource.synthesis_version_id == old_version.id
                    )
                ).all()
                referenced_ids = {item.source_version_id for item in references}
                for reference in references:
                    session.add(
                        SynthesisReferenceSource(
                            synthesis_version_id=new_version.id,
                            source_version_id=reference.source_version_id,
                            source_role=reference.source_role,
                        )
                    )
                if source_version_id and source_version_id not in referenced_ids:
                    session.add(
                        SynthesisReferenceSource(
                            synthesis_version_id=new_version.id,
                            source_version_id=source_version_id,
                            source_role=source_role,
                        )
                    )
                synthesis.active_version_id = new_version.id
                refreshed.append(new_version.id)
        return refreshed

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        """Repin active Syntheses to each shared Concept's active version.

        The operation is deterministic and does not call an LLM. It is useful
        after importing historical data, repairing a partial build, or changing
        a Concept's active version outside the normal contribution path.
        """

        with self.db.session() as session:
            concepts = session.scalars(
                select(Concept).where(
                    Concept.visibility == "ACTIVE",
                    Concept.active_version_id.is_not(None),
                )
            ).all()
            active_versions: dict[str, str] = {}
            skipped: list[dict[str, str]] = []
            for concept in concepts:
                version = session.get(ConceptVersion, concept.active_version_id)
                if (
                    version is None
                    or version.concept_id != concept.id
                    or version.status != "PUBLISHED"
                ):
                    skipped.append(
                        {
                            "concept_id": concept.id,
                            "stable_key": concept.stable_key,
                            "active_version_id": concept.active_version_id or "",
                        }
                    )
                    continue
                active_versions[concept.id] = version.id
        refreshed = self._refresh_synthesis_releases(active_versions)
        return {
            "status": "REFRESHED",
            "active_concepts": len(active_versions),
            "refreshed_synthesis_versions": refreshed,
            "skipped_concepts": skipped,
        }

    def _publish_synthesis(
        self,
        corpus_id: str,
        source_version_id: str,
        *,
        allow_replace_legacy: bool,
        compiled_versions: dict[str, str],
        failures: list[dict[str, str]],
    ) -> tuple[str, str, int]:
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            synthesis = session.scalar(
                select(Synthesis).where(Synthesis.stable_key == self.blueprint.synthesis_key)
            )
            if synthesis is None:
                synthesis = Synthesis(
                    stable_key=self.blueprint.synthesis_key,
                    domain_key=self.blueprint.domain_key,
                    title=self.blueprint.title,
                    description=(
                        "按稳定知识主题组织、跨教材复用，并由来源 AtomicNote 持续补充的语义 Synthesis。"
                    ),
                )
                session.add(synthesis)
                session.flush()
            synthesis.visibility = "ACTIVE"
            previous_active_version_id = synthesis.active_version_id
            concepts = {
                item.stable_key: item
                for item in session.scalars(
                    select(Concept).where(
                        Concept.stable_key.in_(
                            [
                                self.blueprint.stable_concept_key(item)
                                for item in self.blueprint.concepts
                            ]
                        )
                    )
                ).all()
            }
            selected_versions = {
                item.stable_key: compiled_versions.get(item.id, item.active_version_id or "")
                for item in concepts.values()
            }
            release_hash = _sha256_text(
                _json(
                    {
                        "groups": self.blueprint.groups,
                        "concepts": [
                            (
                                item["key"],
                                selected_versions[self.blueprint.stable_concept_key(item)],
                                item["parent_key"],
                            )
                            for item in self.blueprint.concepts
                        ],
                        "failed": sorted(item["concept_key"] for item in failures),
                    }
                )
            )
            version = session.scalar(
                select(SynthesisVersion).where(
                    SynthesisVersion.synthesis_id == synthesis.id,
                    SynthesisVersion.content_sha256 == release_hash,
                )
            )
            if version is None:
                version_no = (
                    session.scalar(
                        select(func.max(SynthesisVersion.version_no)).where(
                            SynthesisVersion.synthesis_id == synthesis.id
                        )
                    )
                    or 0
                ) + 1
                coverage = {
                    "concept_nodes": len(self.blueprint.concepts),
                    "published_concepts": len(compiled_versions),
                    "fresh_concepts": len(compiled_versions),
                    "failed_concepts": len(failures),
                    "groups": len(self.blueprint.groups),
                    "coverage": round(
                        len(compiled_versions) / len(self.blueprint.concepts),
                        4,
                    ),
                    "semantic_backbone": True,
                }
                release_status = (
                    "PUBLISHED"
                    if not failures and coverage["fresh_concepts"] == len(self.blueprint.concepts)
                    else "PARTIAL"
                )
                version = SynthesisVersion(
                    synthesis_id=synthesis.id,
                    backbone_corpus_id=corpus.id,
                    version_no=version_no,
                    title=self.blueprint.release_title,
                    summary=self.blueprint.summary,
                    status=release_status,
                    coverage_json=_json(coverage),
                    content_sha256=release_hash,
                )
                session.add(version)
                session.flush()
                node_ids: dict[str, str] = {}
                sort_order = 0
                for group in self.blueprint.groups:
                    parent_id = node_ids.get(group["parent_key"] or "")
                    node = SynthesisNode(
                        synthesis_version_id=version.id,
                        stable_key=f"{self.blueprint.concept_namespace}-group:{group['key']}",
                        parent_id=parent_id,
                        title=group["title"],
                        original_title=group["title"],
                        level=1,
                        sort_order=sort_order,
                        node_kind="GROUP",
                        source_locator_json=_json({"semantic": True}),
                    )
                    session.add(node)
                    session.flush()
                    node_ids[group["key"]] = node.id
                    sort_order += 1
                    for spec in [
                        item
                        for item in self.blueprint.concepts
                        if item["parent_key"] == group["key"]
                    ]:
                        sort_order = self._append_concept_node(
                            session,
                            version.id,
                            spec,
                            concepts,
                            selected_versions,
                            node_ids,
                            sort_order,
                            level=2,
                        )
                session.add(
                    SynthesisReferenceSource(
                        synthesis_version_id=version.id,
                        source_version_id=source_version_id,
                        source_role="FOUNDATION",
                    )
                )
            if version.status == "PUBLISHED" or previous_active_version_id is None:
                synthesis.active_version_id = version.id

            for spec in self.blueprint.concepts:
                if spec["parent_key"] in {item["key"] for item in self.blueprint.concepts}:
                    parent_spec = next(
                        item
                        for item in self.blueprint.concepts
                        if item["key"] == spec["parent_key"]
                    )
                    source = concepts[self.blueprint.stable_concept_key(parent_spec)]
                    target = concepts[self.blueprint.stable_concept_key(spec)]
                    relation = session.scalar(
                        select(ConceptRelation).where(
                            ConceptRelation.source_concept_id == target.id,
                            ConceptRelation.target_concept_id == source.id,
                            ConceptRelation.relation_type == "PART_OF",
                        )
                    )
                    if relation is None:
                        session.add(
                            ConceptRelation(
                                source_concept_id=target.id,
                                target_concept_id=source.id,
                                relation_type="PART_OF",
                                source_ref=self.blueprint.synthesis_key,
                            )
                        )
            if allow_replace_legacy and self.blueprint.legacy_synthesis_key:
                legacy = session.scalar(
                    select(Synthesis).where(
                        Synthesis.stable_key == self.blueprint.legacy_synthesis_key
                    )
                )
                if legacy is not None:
                    legacy.visibility = "HIDDEN"
            return synthesis.id, version.id, len(self.blueprint.concepts)

    def _append_concept_node(
        self,
        session,
        synthesis_version_id: str,
        spec: ConceptSpec,
        concepts: dict[str, Concept],
        selected_versions: dict[str, str],
        node_ids: dict[str, str],
        sort_order: int,
        *,
        level: int,
    ) -> int:
        concept = concepts[self.blueprint.stable_concept_key(spec)]
        node = SynthesisNode(
            synthesis_version_id=synthesis_version_id,
            stable_key=concept.stable_key,
            parent_id=node_ids.get(spec["parent_key"]),
            title=concept.canonical_name,
            original_title=spec["title"],
            level=level,
            sort_order=sort_order,
            node_kind="CONCEPT",
            concept_id=concept.id,
            concept_version_id=selected_versions[concept.stable_key],
            source_locator_json=_json(
                {"semantic": True, "source_prefixes": spec["source_prefixes"]}
            ),
        )
        session.add(node)
        session.flush()
        node_ids[spec["key"]] = node.id
        sort_order += 1
        for child in [
            item for item in self.blueprint.concepts if item["parent_key"] == spec["key"]
        ]:
            sort_order = self._append_concept_node(
                session,
                synthesis_version_id,
                child,
                concepts,
                selected_versions,
                node_ids,
                sort_order,
                level=level + 1,
            )
        return sort_order

    def build(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> SemanticSynthesisReport:
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            if corpus is None:
                raise ValueError(f"Unknown corpus_id: {corpus_id}")
            self.blueprint = get_blueprint(corpus.domain_key)
        self._ensure_concepts()
        prepared, _section_keys = self._prepare_inputs(corpus_id, source_version_id)
        self._register_foundation_contributions(prepared)
        prepared = {
            spec["key"]: self._prepared_from_contributions(self._global_spec(spec))
            for spec in self.blueprint.concepts
        }
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            futures = {
                executor.submit(self._compile_one, prepared[spec["key"]]): spec
                for spec in self.blueprint.concepts
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    failures.append({"concept_key": spec["key"], "error": str(exc)})
        compiled_versions = {item["concept_id"]: item["concept_version_id"] for item in results}
        synthesis_id, synthesis_version_id, concept_count = self._publish_synthesis(
            corpus_id,
            source_version_id,
            allow_replace_legacy=not failures,
            compiled_versions=compiled_versions,
            failures=failures,
        )
        refreshed_synthesis_versions = (
            self._refresh_synthesis_releases(
                compiled_versions,
                source_version_id=source_version_id,
                source_role="FOUNDATION",
                exclude_synthesis_id=synthesis_id,
            )
            if not failures
            else []
        )
        concept_ids = [item["concept_id"] for item in results]
        with self.db.session() as session:
            active_version_ids = [
                item.active_version_id
                for item in session.scalars(select(Concept).where(Concept.id.in_(concept_ids)))
                if item.active_version_id
            ]
            active_module_ids = select(ConceptReleaseModule.module_id).where(
                ConceptReleaseModule.concept_version_id.in_(active_version_ids)
            )
            module_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ConceptReleaseModule)
                    .where(ConceptReleaseModule.concept_version_id.in_(active_version_ids))
                )
                if active_version_ids
                else 0
            ) or 0
            contribution_count = (
                session.scalar(
                    select(func.count(ConceptContribution.id)).where(
                        ConceptContribution.module_id.in_(active_module_ids),
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                )
                if active_version_ids
                else 0
            ) or 0
        fallbacks = [
            {
                "concept_id": item["concept_id"],
                "detail": item.get("fallback_detail", ""),
            }
            for item in results
            if item.get("fallback")
        ]
        return SemanticSynthesisReport(
            synthesis_id=synthesis_id,
            synthesis_version_id=synthesis_version_id,
            status="PUBLISHED" if not failures else "PARTIAL",
            concepts=concept_count,
            groups=len(self.blueprint.groups),
            modules=module_count,
            contributions=contribution_count,
            reused_builds=sum(bool(item.get("reused")) for item in results),
            refreshed_synthesis_versions=refreshed_synthesis_versions,
            fallbacks=fallbacks,
            failed=failures,
        )
