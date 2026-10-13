from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobStage(StrEnum):
    CREATED = "CREATED"
    SUBMITTING_PARSE = "SUBMITTING_PARSE"
    WAITING_PARSE = "WAITING_PARSE"
    NORMALIZING = "NORMALIZING"
    MAPPING = "MAPPING"
    GENERATING = "GENERATING"
    PROFILING_DOCUMENT = "PROFILING_DOCUMENT"
    BUILDING_KNOWLEDGE_MAP = "BUILDING_KNOWLEDGE_MAP"
    PLANNING_SOURCE_NOTE_OUTLINE = "PLANNING_SOURCE_NOTE_OUTLINE"
    PLANNING_NOTE_SET = "PLANNING_NOTE_SET"
    REVIEWING_NOTE_SET = "REVIEWING_NOTE_SET"
    REPAIRING_NOTE_SET = "REPAIRING_NOTE_SET"
    ANALYZING_VISUALS = "ANALYZING_VISUALS"
    DRAFTING_NOTES = "DRAFTING_NOTES"
    EVALUATING_NOTES = "EVALUATING_NOTES"
    VALIDATING_NOTES = "VALIDATING_NOTES"
    INDEXING = "INDEXING"
    COMPLETED = "COMPLETED"


class BlockOrigin(StrEnum):
    AI_GENERATED = "AI_GENERATED"
    AI_REVISED = "AI_REVISED"
    HUMAN_REVISED = "HUMAN_REVISED"


class QualityStatus(StrEnum):
    PENDING = "PENDING"
    AUTO_VALIDATED = "AUTO_VALIDATED"
    NEEDS_REPAIR = "NEEDS_REPAIR"


class ReviewStatus(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    HUMAN_APPROVED = "HUMAN_APPROVED"


class SupportStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    HUMAN_ADDED = "HUMAN_ADDED"
    UNSUPPORTED = "UNSUPPORTED"


class ParsedBlock(BaseModel):
    block_id: str
    block_type: str = "TEXT"
    text: str = ""
    page_index: int
    page_label: str
    level: int | None = None
    bbox: list[float] | None = None
    asset_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    title: str
    page_count: int
    markdown: str
    blocks: list[ParsedBlock]
    raw_artifact_path: str | None = None
    parser_name: str
    parser_version: str | None = None


class TopicSegment(BaseModel):
    stable_key: str
    title: str
    start_page_index: int
    end_page_index: int
    rationale: str = ""


class DocumentProfile(BaseModel):
    document_type: str = "COURSE_MATERIAL"
    document_summary: str
    learning_goal: str = ""
    segments: list[TopicSegment]


class KnowledgeWindow(BaseModel):
    stable_key: str
    segment_key: str
    segment_title: str
    window_index: int
    window_count: int
    block_ids: list[str]


class KnowledgeConceptCandidate(BaseModel):
    """Compact window-local concept used before the global map reducer."""

    stable_key: str
    title: str
    question_answered: str
    core_claim: str
    concept_type: str = "CONCEPT"
    importance: str = "SUPPORTING"
    evidence_block_ids: list[str] = Field(default_factory=list)
    asset_block_ids: list[str] = Field(default_factory=list)
    source_sections: list[str] = Field(default_factory=list)


class KnowledgeConcept(BaseModel):
    stable_key: str
    title: str
    question_answered: str
    core_claim: str
    concept_type: str = "CONCEPT"
    importance: str = "SUPPORTING"
    importance_rationale: str = ""
    evidence_block_ids: list[str] = Field(default_factory=list)
    asset_block_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_sections: list[str] = Field(default_factory=list)
    prerequisite_keys: list[str] = Field(default_factory=list)
    related_keys: list[str] = Field(default_factory=list)


class KnowledgeMap(BaseModel):
    document_summary: str
    concepts: list[KnowledgeConcept]
    skipped_sections: list[dict[str, str]] = Field(default_factory=list)


class SourceNoteOutlineNodeSpec(BaseModel):
    stable_key: str
    title: str
    level: int = Field(ge=1, le=3)
    parent_key: str | None = None
    learning_goal: str = ""
    source_sections: list[str] = Field(default_factory=list)


class SourceNoteOutlinePlan(BaseModel):
    title: str
    summary: str
    learning_goal: str = ""
    nodes: list[SourceNoteOutlineNodeSpec]


class NoteSpec(BaseModel):
    stable_key: str
    title: str
    question_answered: str
    target_thesis: str
    outline_node_key: str = ""
    concept_keys: list[str]
    evidence_block_ids: list[str] = Field(default_factory=list)
    asset_block_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_sections: list[str] = Field(default_factory=list)
    rationale: str = ""
    note_kind: str = "CONCEPT"
    editorial_intent: str = ""
    preferred_block_types: list[str] = Field(default_factory=list)
    visual_requirements: list[str] = Field(default_factory=list)


class NoteSetPlan(BaseModel):
    document_summary: str
    notes: list[NoteSpec]
    skipped_concepts: list[dict[str, str]] = Field(default_factory=list)


class SourceNotePlanningBundle(BaseModel):
    """One structured planning result containing both hierarchy and note units."""

    schema_version: str = "source-plan-v2"
    source_note_outline: SourceNoteOutlinePlan
    note_set_plan: NoteSetPlan


class PlanIssue(BaseModel):
    code: str
    detail: str
    note_keys: list[str] = Field(default_factory=list)
    concept_keys: list[str] = Field(default_factory=list)


class ConceptCoverage(BaseModel):
    concept_key: str
    status: str
    note_keys: list[str] = Field(default_factory=list)
    reason: str = ""


class NoteSetReview(BaseModel):
    passed: bool
    issues: list[PlanIssue] = Field(default_factory=list)
    coverage: list[ConceptCoverage] = Field(default_factory=list)


class NoteBlockDraft(BaseModel):
    block_type: str = "PARAGRAPH"
    text: str = ""
    items: list[str] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    caption: str = ""
    purpose: str = ""
    language: str = ""
    evidence_block_ids: list[str] = Field(default_factory=list)
    asset_block_ids: list[str] = Field(default_factory=list)

    def plain_text(self) -> str:
        kind = self.block_type.upper()
        if kind in {"BULLETED_LIST", "NUMBERED_LIST"}:
            return "\n".join(item.strip() for item in self.items if item.strip())
        if kind == "TABLE":
            rows = [self.table_headers, *self.table_rows]
            return "\n".join(" | ".join(str(cell).strip() for cell in row) for row in rows if row)
        if kind == "IMAGE":
            return self.caption.strip() or self.purpose.strip()
        return self.text.strip()

    def content_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "items": self.items,
            "table_headers": self.table_headers,
            "table_rows": self.table_rows,
            "caption": self.caption,
            "purpose": self.purpose,
            "language": self.language,
        }


class NoteDraft(BaseModel):
    stable_key: str
    title: str
    thesis: str
    blocks: list[NoteBlockDraft]
    tags: list[str] = Field(default_factory=list)
    source_sections: list[str] = Field(default_factory=list)


class VisualUnderstanding(BaseModel):
    visual_asset_id: str = ""
    external_block_id: str
    image_sha256: str
    model: str
    prompt_version: str = "visual-understanding-v2"
    asset_type: str = "OTHER"
    is_decorative: bool = False
    essential: bool = False
    caption: str = ""
    teaching_role: str = ""
    ocr_text: list[str] = Field(default_factory=list)
    factual_observations: list[str] = Field(default_factory=list)
    spatial_relations: list[str] = Field(default_factory=list)
    suggested_placement: str = ""
    confidence: float = 0.0
    analysis_tier: str = "API_FAST"
    route_reason: str = ""
    is_fragment: bool = False
    fragment_group: str = ""
    group_id: str = ""
    group_type: str = "SINGLE"
    related_asset_ids: list[str] = Field(default_factory=list)


class KnowledgePlan(BaseModel):
    document_summary: str
    notes: list[NoteDraft]
    skipped_sections: list[dict[str, str]] = Field(default_factory=list)


class NoteEvaluation(BaseModel):
    stable_key: str
    passed: bool
    issues: list[str] = Field(default_factory=list)
    content_sha256: str = ""
    evaluator_model: str = ""
    prompt_version: str = ""


class ReleaseNoteIssue(BaseModel):
    stable_key: str
    issues: list[str] = Field(default_factory=list)


class OutlineTitlePatch(BaseModel):
    stable_key: str
    title: str
    reason: str = ""


class SourceReleaseReview(BaseModel):
    passed: bool
    note_issues: list[ReleaseNoteIssue] = Field(default_factory=list)
    outline_title_patches: list[OutlineTitlePatch] = Field(default_factory=list)
    summary_patch: str = ""
    learning_goal_patch: str = ""
    global_issues: list[str] = Field(default_factory=list)


class GenerationBundle(BaseModel):
    schema_version: str = "source-note-generation-v7-occam"
    prompt_version: str = "pdf-source-note-v5"
    profile: DocumentProfile
    knowledge_map: KnowledgeMap
    source_note_outline: SourceNoteOutlinePlan
    note_set_plan: NoteSetPlan
    note_set_review: NoteSetReview | None = None
    final_plan: KnowledgePlan
    evaluations: list[NoteEvaluation] = Field(default_factory=list)


class SearchHit(BaseModel):
    note_id: str
    note_version_id: str
    title: str
    thesis: str
    body: str
    score: float
    entity_type: Literal["ATOMIC_NOTE", "CONCEPT"] = "ATOMIC_NOTE"
    concept_id: str = ""
    synthesis_paths: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)


class Answer(BaseModel):
    answer: str
    hits: list[SearchHit]
    coverage_warning: str | None = None
