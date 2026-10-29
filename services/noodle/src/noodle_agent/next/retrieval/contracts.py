from typing import Literal

from pydantic import Field, model_validator

from ..contracts import Contract, Id, Text


class Scope(Contract):
    mode: Literal["GLOBAL", "SOURCE", "NOTES"] = "GLOBAL"
    source_ids: list[Id] = Field(default_factory=list)
    note_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.mode == "GLOBAL" and (self.source_ids or self.note_ids):
            raise ValueError("GLOBAL cannot carry hidden filters")
        if self.mode == "SOURCE" and (len(self.source_ids) != 1 or self.note_ids):
            raise ValueError("SOURCE requires exactly one source_id")
        if self.mode == "NOTES" and (not self.note_ids or self.source_ids):
            raise ValueError("NOTES requires note_ids only")
        return self


class Query(Contract):
    query: str = Field(min_length=1, max_length=4000)
    scope: Scope = Field(default_factory=Scope)
    allowed_kinds: list[Literal["ATOMIC", "CONCEPT"]] = Field(
        default_factory=lambda: ["ATOMIC", "CONCEPT"], min_length=1
    )
    retrieval_mode: Literal["HYBRID", "KEYWORD_ONLY"] = "HYBRID"
    rerank: Literal["AUTO", "OFF", "REQUIRED"] = "AUTO"
    top_k: int = Field(default=6, ge=1, le=20)
    entity_note_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def restrict(self):
        if len(set(self.allowed_kinds)) != len(self.allowed_kinds):
            raise ValueError("Duplicate allowed kind")
        if self.scope.mode == "SOURCE" and self.allowed_kinds != ["ATOMIC"]:
            raise ValueError("Source scope only admits ATOMIC notes")
        return self


class Claim(Contract):
    text: Text
    citation_ids: list[Id] = Field(min_length=1)


class AnswerDraft(Contract):
    claims: list[Claim] = Field(default_factory=list)
    insufficient: bool = False

    @model_validator(mode="after")
    def coherent(self):
        if self.insufficient == bool(self.claims):
            raise ValueError("Return claims or insufficient, exclusively")
        return self


class AnswerCheck(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    supported_claim_indexes: list[int]
    severity: Literal["REPAIRABLE", "SEVERE"] = "REPAIRABLE"


class Ranking(Contract):
    ordered_chunk_ids: list[Id]


class Provenance(Contract):
    source_revision_id: Id
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page: int | None = Field(default=None, ge=1)
    source_families: list[str] = Field(default_factory=list)


class MatchedRange(Contract):
    chunk_id: Id
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Citation(Contract):
    citation_id: Id
    chunk_id: Id
    note_id: Id
    revision_id: Id
    kind: Literal["ATOMIC", "CONCEPT"]
    title: Text
    section_id: Id
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ids: list[Id]
    provenance: list[Provenance]
    matched_ranges: list[MatchedRange] = Field(default_factory=list)
    expansion_reason: str = "MATCH_ONLY"


class Hit(Citation):
    section_title: Text
    body: Text
    score: float
    ranks: dict[str, int]
    rerank_score: float | None = None


class Counts(Contract):
    atomic_notes: int = Field(ge=0)
    concept_notes: int = Field(ge=0)
    chunks: int = Field(ge=0)
    independent_source_families: int = Field(ge=0)


class Snapshot(Contract):
    scope: Scope
    profile_id: Id | None
    index_generation: Id | None
    validity_epoch: int | None
    context_sha256: str | None


class Diagnostics(Contract):
    lanes: dict[str, int]
    eligible_chunks: int
    context_tokens: int
    coverage_pending_revisions: list[Id]
    candidate_count: int = 0
    rerank: dict = Field(default_factory=dict)
    context_artifact_tokens: int = 0
    answer_input_tokens_estimate: int = 0
    review_input_tokens_estimate: int = 0
    answer_revisions: int = Field(default=0, ge=0, le=2)
    knowledge_repair_requested: bool = False
    post_repair_semantic_review: bool | None = None


class SearchResponse(Contract):
    request_id: Id
    status: Literal[
        "READY",
        "PROFILE_MISMATCH",
        "INDEX_PENDING",
        "NO_EVIDENCE",
        "SCOPE_RESTRICTED",
        "CLARIFICATION_REQUIRED",
        "PROVIDER_ERROR",
        "CONTENT_CHANGED",
        "COVERAGE_GAP",
    ]
    message: str
    hits: list[Hit]
    counts: Counts
    snapshot: Snapshot
    timings_ms: dict[str, float]
    degraded_reason: str | None
    diagnostics: Diagnostics | None = None


class AnswerResponse(SearchResponse):
    answer_markdown: str
    citations: list[Citation]
    source_titles: dict[str, str] = Field(default_factory=dict)
    usage_summary: dict = Field(default_factory=dict)
