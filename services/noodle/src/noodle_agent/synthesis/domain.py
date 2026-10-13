from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ConceptBlockType = Literal[
    "MARKDOWN",
    "FORMULA",
    "ALGORITHM",
    "EXAMPLE",
    "TABLE",
    "FIGURE",
    "LIMITATION",
]


class ConceptBlockDraft(BaseModel):
    block_key: str = Field(min_length=1, max_length=160)
    block_type: ConceptBlockType = "MARKDOWN"
    content: str = Field(min_length=1, max_length=12_000)


class ConceptDraft(BaseModel):
    """Uniform prose contract; optional ideas are blocks, not empty columns."""

    title_zh: str = Field(min_length=1, max_length=240)
    scope: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(min_length=1, max_length=2_000)
    blocks: list[ConceptBlockDraft] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_block_keys(self) -> ConceptDraft:
        keys = [item.block_key for item in self.blocks]
        if len(keys) != len(set(keys)):
            raise ValueError("Concept block_key values must be unique")
        return self


class BackboneManifest(BaseModel):
    stable_key: str
    domain_key: str
    title: str
    authors: list[str]
    edition_label: str
    language: str = "en"
    source_url: str
    license_note: str


class BuildReport(BaseModel):
    corpus_id: str
    synthesis_id: str
    synthesis_version_id: str
    status: str
    toc_nodes: int
    concept_nodes: int
    seeded_concepts: int
    published_concepts: int
    coverage: float


class FigureExtractionReport(BaseModel):
    corpus_id: str
    discovered: int
    extracted: int
    skipped_unmapped: int
    concepts_with_figures: int
    pages_with_figures: int
    bytes_written: int


class FigureCaptionTranslationReport(BaseModel):
    corpus_id: str
    requested: int
    translated: int
    skipped_existing: int
    batches: int
    failed: list[dict[str, str]] = Field(default_factory=list)


ModuleRenderType = Literal[
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
]

ModuleSemanticRole = Literal[
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
]


class ConceptScope(BaseModel):
    """Stable semantic boundary; Synthesis placement is intentionally excluded."""

    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class ConceptConflictPosition(BaseModel):
    evidence_ref: str = Field(min_length=1)
    claim: str = Field(min_length=1, max_length=2_000)


class ConceptConflict(BaseModel):
    status: Literal["UNRESOLVED", "RESOLVED"] = "UNRESOLVED"
    positions: list[ConceptConflictPosition] = Field(min_length=2)
    resolution: str = Field(default="", max_length=2_000)


class ConceptContentBlock(BaseModel):
    """One independently renderable, evidence-bound unit inside a semantic module."""

    block_key: str = Field(min_length=1, max_length=220)
    type: ModuleRenderType = "PROSE"
    markdown: str = ""
    items: list[str] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    latex: str = ""
    symbols: list[dict[str, str]] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    code: str = ""
    language: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    concept_key: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    note_block_ids: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)
    conflict: ConceptConflict | None = None

    @model_validator(mode="after")
    def validate_content(self) -> ConceptContentBlock:
        valid = {
            "PROSE": bool(self.markdown.strip()),
            "CALLOUT": bool(self.markdown.strip()),
            "EXAMPLE": bool(self.markdown.strip()),
            "LIST": bool(self.items),
            "TABLE": bool(self.table_headers and self.table_rows),
            "FORMULA": bool(self.latex.strip()),
            "PROCEDURE": bool(self.steps),
            "CODE": bool(self.code.strip()),
            "ASSET": bool(self.asset_ids),
            "CONCEPT_REF": bool(self.concept_key.strip()),
        }[self.type]
        if not valid:
            raise ValueError(f"{self.type} content block is empty")
        if self.conflict and self.conflict.status == "RESOLVED" and not self.conflict.resolution.strip():
            raise ValueError("A resolved conflict requires a resolution")
        return self

    def plain_text(self) -> str:
        if self.type == "LIST":
            return "\n".join(self.items)
        if self.type == "TABLE":
            return "\n".join(
                " | ".join(str(cell) for cell in row)
                for row in [self.table_headers, *self.table_rows]
                if row
            )
        if self.type == "FORMULA":
            formula = f"$$\n{self.latex.strip()}\n$$" if self.latex.strip() else ""
            return "\n\n".join(item for item in [formula, self.markdown.strip()] if item)
        if self.type == "PROCEDURE":
            return "\n".join(f"{index}. {item}" for index, item in enumerate(self.steps, 1))
        if self.type == "CODE":
            return self.code
        return self.markdown


class ConceptModulePayload(BaseModel):
    """Typed union-like payload without forcing irrelevant empty modules."""

    markdown: str = ""
    items: list[str] = Field(default_factory=list)
    table_headers: list[str] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    latex: str = ""
    symbols: list[dict[str, str]] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    code: str = ""
    language: str = ""
    asset_ids: list[str] = Field(default_factory=list)
    concept_key: str = ""
    blocks: list[ConceptContentBlock] = Field(default_factory=list, max_length=40)


class SemanticConceptModuleDraft(BaseModel):
    block_key: str = Field(min_length=1, max_length=180)
    title: str = Field(default="", max_length=240)
    render_type: ModuleRenderType = "PROSE"
    semantic_role: ModuleSemanticRole = "OVERVIEW"
    payload: ConceptModulePayload

    def plain_text(self) -> str:
        payload = self.payload
        if payload.blocks:
            return "\n\n".join(block.plain_text() for block in payload.blocks)
        if self.render_type == "LIST":
            return "\n".join(payload.items)
        if self.render_type == "TABLE":
            return "\n".join(
                " | ".join(str(cell) for cell in row)
                for row in [payload.table_headers, *payload.table_rows]
                if row
            )
        if self.render_type == "FORMULA":
            explanation = payload.markdown.strip()
            formula = f"$$\n{payload.latex.strip()}\n$$" if payload.latex.strip() else ""
            return "\n\n".join(item for item in [formula, explanation] if item)
        if self.render_type == "PROCEDURE":
            return "\n".join(
                f"{index}. {item}" for index, item in enumerate(payload.steps, 1)
            )
        if self.render_type == "CODE":
            return payload.code
        return payload.markdown

    @model_validator(mode="after")
    def validate_payload_shape(self) -> SemanticConceptModuleDraft:
        payload = self.payload
        if payload.blocks:
            keys = [item.block_key for item in payload.blocks]
            if len(keys) != len(set(keys)):
                raise ValueError("Concept content block_key values must be unique per module")
            return self
        valid = {
            "PROSE": bool(payload.markdown.strip()),
            "CALLOUT": bool(payload.markdown.strip()),
            "EXAMPLE": bool(payload.markdown.strip()),
            "LIST": bool(payload.items),
            "TABLE": bool(payload.table_headers and payload.table_rows),
            "FORMULA": bool(payload.latex.strip()),
            "PROCEDURE": bool(payload.steps),
            "CODE": bool(payload.code.strip()),
            "ASSET": bool(payload.asset_ids),
            "CONCEPT_REF": bool(payload.concept_key.strip()),
        }[self.render_type]
        if not valid:
            raise ValueError(f"{self.render_type} module payload is empty")
        return self


class SemanticConceptDraft(BaseModel):
    title_zh: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=2_000)
    scope: ConceptScope = Field(default_factory=ConceptScope)
    modules: list[SemanticConceptModuleDraft] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_module_keys(self) -> SemanticConceptDraft:
        keys = [item.block_key for item in self.modules]
        if len(keys) != len(set(keys)):
            raise ValueError("Semantic Concept block_key values must be unique")
        return self


class ConceptModuleJudgeIssue(BaseModel):
    issue_id: str = Field(min_length=1, max_length=120)
    severity: Literal["BLOCKER", "MAJOR", "MINOR"] = "MAJOR"
    category: Literal[
        "EVIDENCE_GAP",
        "FACTUAL_ERROR",
        "TERMINOLOGY",
        "FORMULA",
        "SOURCE_CONFLICT",
        "READABILITY",
        "DUPLICATION",
    ]
    block_key: str = Field(default="", max_length=220)
    claim: str = Field(default="", max_length=2_000)
    message: str = Field(min_length=1, max_length=4_000)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_span_ids: list[str] = Field(default_factory=list)
    suggested_action: str = Field(default="", max_length=4_000)


class ConceptModuleJudgeResult(BaseModel):
    """Independent semantic gate for one changed Concept module."""

    decision: Literal["PASS", "REPAIR", "REJECT"]
    summary: str = Field(min_length=1, max_length=2_000)
    issues: list[ConceptModuleJudgeIssue] = Field(default_factory=list, max_length=30)
    coverage: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def decision_matches_issues(self) -> ConceptModuleJudgeResult:
        if self.decision == "PASS" and any(
            issue.severity in {"BLOCKER", "MAJOR"} for issue in self.issues
        ):
            raise ValueError("PASS cannot contain BLOCKER or MAJOR issues")
        if self.decision in {"REPAIR", "REJECT"} and not self.issues:
            raise ValueError(f"{self.decision} requires at least one issue")
        return self


class BackboneMigrationReport(BaseModel):
    source_id: str
    source_version_id: str
    parse_run_id: str
    source_note_id: str
    source_note_version_id: str
    outline_nodes: int
    atomic_notes: int
    note_blocks: int
    figures: int


class SemanticSynthesisReport(BaseModel):
    synthesis_id: str
    synthesis_version_id: str
    status: str
    concepts: int
    groups: int
    modules: int
    contributions: int
    reused_builds: int
    refreshed_synthesis_versions: list[str] = Field(default_factory=list)
    fallbacks: list[dict[str, str]] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)
