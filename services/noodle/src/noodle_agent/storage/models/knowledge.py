from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id, utcnow


class BackboneCorpus(Base):
    """One authoritative source used to seed a reusable knowledge backbone."""

    __tablename__ = "backbone_corpora"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stable_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    domain_key: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text)
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    edition_label: Mapped[str] = mapped_column(String(120), default="")
    language: Mapped[str] = mapped_column(String(20), default="en")
    source_url: Mapped[str] = mapped_column(Text, default="")
    license_note: Mapped[str] = mapped_column(Text, default="")
    local_path: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default="REGISTERED", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class BackboneSection(Base):
    """A deterministic textbook TOC node; knowledge-bearing nodes become Concepts."""

    __tablename__ = "backbone_sections"
    __table_args__ = (UniqueConstraint("corpus_id", "stable_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    corpus_id: Mapped[str] = mapped_column(ForeignKey("backbone_corpora.id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(220), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("backbone_sections.id"), index=True)
    number: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer)
    node_kind: Mapped[str] = mapped_column(String(30), default="CONCEPT", index=True)
    start_page: Mapped[int] = mapped_column(Integer)
    end_page: Mapped[int] = mapped_column(Integer)
    concept_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(30), default="PLANNED", index=True)
    source_locator_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class BackboneFigure(Base):
    """A source-faithful textbook figure mapped to the deepest owning Concept."""

    __tablename__ = "backbone_figures"
    __table_args__ = (UniqueConstraint("corpus_id", "figure_label"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    corpus_id: Mapped[str] = mapped_column(ForeignKey("backbone_corpora.id"), index=True)
    section_id: Mapped[str] = mapped_column(ForeignKey("backbone_sections.id"), index=True)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer, index=True)
    figure_label: Mapped[str] = mapped_column(String(80), index=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    caption_zh: Mapped[str] = mapped_column(Text, default="")
    storage_key: Mapped[str] = mapped_column(Text)
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    bbox_json: Mapped[str] = mapped_column(Text, default="[]")
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    extraction_method: Mapped[str] = mapped_column(String(60), default="PDF_CAPTION_RENDER")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Concept(Base):
    """Stable cross-source identity; mutable prose lives in ConceptVersion."""

    __tablename__ = "concepts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stable_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    domain_key: Mapped[str] = mapped_column(String(120), index=True)
    canonical_name: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text, default="")
    active_version_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ConceptAlias(Base):
    """Normalized name that resolves to one global Concept identity."""

    __tablename__ = "concept_aliases"
    __table_args__ = (UniqueConstraint("normalized_alias"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    alias_type: Mapped[str] = mapped_column(String(30), default="ALIAS", index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptVersion(Base):
    __tablename__ = "concept_versions"
    __table_args__ = (UniqueConstraint("concept_id", "version_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(60), default="concept-v1")
    prompt_version: Mapped[str] = mapped_column(String(60), default="backbone-writer-v1")
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptEmbedding(Base):
    """Derived vector for an immutable ConceptVersion used by unified RAG."""

    __tablename__ = "concept_embeddings"
    __table_args__ = (UniqueConstraint("concept_version_id", "model_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(200), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptMergeRecord(Base):
    """Audited redirect after two Concept identities are merged."""

    __tablename__ = "concept_merge_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    target_concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"))
    target_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"))
    merged_target_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"))
    actor: Mapped[str] = mapped_column(String(100), default="agent")
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="MERGED", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptBlock(Base):
    """Notion-shaped content block; empty optional sections are never persisted."""

    __tablename__ = "concept_blocks"
    __table_args__ = (UniqueConstraint("concept_version_id", "block_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), index=True)
    block_key: Mapped[str] = mapped_column(String(160))
    block_type: Mapped[str] = mapped_column(String(40))
    sort_order: Mapped[int] = mapped_column(Integer)
    content_text: Mapped[str] = mapped_column(Text, default="")
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    source_role: Mapped[str] = mapped_column(String(30), default="BACKBONE")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptEvidenceLink(Base):
    __tablename__ = "concept_evidence_links"
    __table_args__ = (UniqueConstraint("concept_block_id", "source_ref", "source_role"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_block_id: Mapped[str] = mapped_column(ForeignKey("concept_blocks.id"), index=True)
    source_role: Mapped[str] = mapped_column(String(30), default="BACKBONE")
    source_ref: Mapped[str] = mapped_column(Text)
    source_locator_json: Mapped[str] = mapped_column(Text, default="{}")
    document_block_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_blocks.id"), index=True
    )
    note_block_id: Mapped[str | None] = mapped_column(ForeignKey("note_blocks.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptModule(Base):
    """Stable editable module identity owned by one reusable Concept."""

    __tablename__ = "concept_modules"
    __table_args__ = (UniqueConstraint("concept_id", "module_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    module_key: Mapped[str] = mapped_column(String(180))
    semantic_role: Mapped[str] = mapped_column(String(40), default="OVERVIEW", index=True)
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ConceptModuleVersion(Base):
    """Immutable content version for one Concept module."""

    __tablename__ = "concept_module_versions"
    __table_args__ = (UniqueConstraint("module_id", "version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    module_id: Mapped[str] = mapped_column(ForeignKey("concept_modules.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    render_type: Mapped[str] = mapped_column(String(40), default="PROSE")
    title: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    plain_text: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(40), default="ATOMIC_SYNTHESIZED")
    lock_state: Mapped[str] = mapped_column(String(30), default="OPEN")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="agent")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptReleaseModule(Base):
    """Pins a module version into one reproducible ConceptVersion release."""

    __tablename__ = "concept_release_modules"
    __table_args__ = (
        UniqueConstraint("concept_version_id", "module_id"),
        UniqueConstraint("concept_version_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), index=True)
    module_id: Mapped[str] = mapped_column(ForeignKey("concept_modules.id"), index=True)
    module_version_id: Mapped[str] = mapped_column(
        ForeignKey("concept_module_versions.id"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer)


class ConceptContribution(Base):
    """One accepted or proposed source contribution to a stable Concept module."""

    __tablename__ = "concept_contributions"
    __table_args__ = (UniqueConstraint("module_id", "atomic_note_version_id", "contribution_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    module_id: Mapped[str] = mapped_column(ForeignKey("concept_modules.id"), index=True)
    atomic_note_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("note_versions.id"), index=True
    )
    source_role: Mapped[str] = mapped_column(String(40), default="ATOMIC_NOTE")
    source_ref: Mapped[str] = mapped_column(Text, default="")
    contribution_role: Mapped[str] = mapped_column(String(40), default="SUPPORT")
    decision_status: Mapped[str] = mapped_column(String(30), default="ACCEPTED", index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    assigned_by: Mapped[str] = mapped_column(String(100), default="migration")
    superseded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_contributions.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column()


class ConceptModuleAsset(Base):
    """Version-specific asset placement inside one Concept module."""

    __tablename__ = "concept_module_assets"
    __table_args__ = (UniqueConstraint("module_version_id", "visual_asset_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    module_version_id: Mapped[str] = mapped_column(
        ForeignKey("concept_module_versions.id"), index=True
    )
    visual_asset_id: Mapped[str] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    content_block_key: Mapped[str] = mapped_column(String(220), default="", index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    placement: Mapped[str] = mapped_column(String(20), default="AFTER")
    caption: Mapped[str] = mapped_column(Text, default="")
    alt_text: Mapped[str] = mapped_column(Text, default="")


class ConceptContentEvidence(Base):
    """Claim-level provenance from one generated content block to source evidence."""

    __tablename__ = "concept_content_evidence"
    __table_args__ = (
        UniqueConstraint("module_version_id", "content_block_key", "evidence_span_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    module_version_id: Mapped[str] = mapped_column(
        ForeignKey("concept_module_versions.id"), index=True
    )
    content_block_key: Mapped[str] = mapped_column(String(220), index=True)
    contribution_id: Mapped[str] = mapped_column(ForeignKey("concept_contributions.id"), index=True)
    note_block_id: Mapped[str] = mapped_column(ForeignKey("note_blocks.id"), index=True)
    evidence_span_id: Mapped[str] = mapped_column(ForeignKey("evidence_spans.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptBuildRecord(Base):
    """Makes semantic Concept compilation idempotent for the same source set."""

    __tablename__ = "concept_build_records"
    __table_args__ = (UniqueConstraint("concept_id", "input_sha256", "prompt_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    concept_version_id: Mapped[str] = mapped_column(ForeignKey("concept_versions.id"), index=True)
    input_sha256: Mapped[str] = mapped_column(String(64), index=True)
    prompt_version: Mapped[str] = mapped_column(String(100))
    model_name: Mapped[str] = mapped_column(String(200), default="")
    result_status: Mapped[str] = mapped_column(String(30), default="VALIDATED", index=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_detail: Mapped[str] = mapped_column(Text, default="")
    output_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptUpdateJob(Base):
    """Durable state machine for one explicitly approved Concept update."""

    __tablename__ = "concept_update_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    module_id: Mapped[str] = mapped_column(ForeignKey("concept_modules.id"), index=True)
    atomic_note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    contribution_id: Mapped[str] = mapped_column(ForeignKey("concept_contributions.id"), index=True)
    operation: Mapped[str] = mapped_column(String(30), default="ADD", index=True)
    supersedes_contribution_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_contributions.id"), index=True
    )
    expected_concept_version_id: Mapped[str] = mapped_column(
        ForeignKey("concept_versions.id"), index=True
    )
    candidate_concept_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_versions.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    actor: Mapped[str] = mapped_column(String(100), default="agent")
    rationale: Mapped[str] = mapped_column(Text, default="")
    input_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_detail: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class ConceptModuleJudgeRecord(Base):
    """Immutable audit record for one independent module quality review."""

    __tablename__ = "concept_module_judge_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    concept_update_job_id: Mapped[str] = mapped_column(
        ForeignKey("concept_update_jobs.id"), index=True
    )
    candidate_concept_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_versions.id"), index=True
    )
    module_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_module_versions.id"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    judge_model: Mapped[str] = mapped_column(String(200), default="")
    prompt_version: Mapped[str] = mapped_column(String(100))
    input_sha256: Mapped[str] = mapped_column(String(64), index=True)
    output_sha256: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(30), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConceptRelation(Base):
    """Reusable semantic relation independent from any Synthesis placement."""

    __tablename__ = "concept_relations"
    __table_args__ = (UniqueConstraint("source_concept_id", "target_concept_id", "relation_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    target_concept_id: Mapped[str] = mapped_column(ForeignKey("concepts.id"), index=True)
    relation_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    source_ref: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Synthesis(Base):
    __tablename__ = "syntheses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stable_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    domain_key: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    active_version_id: Mapped[str | None] = mapped_column(String(36))
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SynthesisVersion(Base):
    __tablename__ = "synthesis_versions"
    __table_args__ = (UniqueConstraint("synthesis_id", "version_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    synthesis_id: Mapped[str] = mapped_column(ForeignKey("syntheses.id"), index=True)
    backbone_corpus_id: Mapped[str] = mapped_column(ForeignKey("backbone_corpora.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    coverage_json: Mapped[str] = mapped_column(Text, default="{}")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SynthesisNode(Base):
    __tablename__ = "synthesis_nodes"
    __table_args__ = (UniqueConstraint("synthesis_version_id", "stable_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    synthesis_version_id: Mapped[str] = mapped_column(
        ForeignKey("synthesis_versions.id"), index=True
    )
    stable_key: Mapped[str] = mapped_column(String(220), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("synthesis_nodes.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[int] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer)
    node_kind: Mapped[str] = mapped_column(String(30), default="CONCEPT")
    concept_id: Mapped[str | None] = mapped_column(ForeignKey("concepts.id"), index=True)
    concept_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("concept_versions.id"), index=True
    )
    source_locator_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SynthesisReferenceSource(Base):
    """Many-to-many provenance: a semantic Synthesis can use many source versions."""

    __tablename__ = "synthesis_reference_sources"
    __table_args__ = (UniqueConstraint("synthesis_version_id", "source_version_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    synthesis_version_id: Mapped[str] = mapped_column(
        ForeignKey("synthesis_versions.id"), index=True
    )
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    source_role: Mapped[str] = mapped_column(String(40), default="REFERENCE")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeSyncEvent(Base):
    __tablename__ = "knowledge_sync_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class KnowledgeSyncProposal(Base):
    __tablename__ = "knowledge_sync_proposals"
    __table_args__ = (UniqueConstraint("event_id", "note_version_id", "target_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sync_events.id"), index=True)
    note_version_id: Mapped[str] = mapped_column(String(36), index=True)
    target_key: Mapped[str] = mapped_column(String(240))
    decision: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    error_detail: Mapped[str] = mapped_column(Text, default="")


class KnowledgeModuleState(Base):
    __tablename__ = "knowledge_module_states"
    module_id: Mapped[str] = mapped_column(ForeignKey("concept_modules.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64), default="")
    event_id: Mapped[str] = mapped_column(String(36), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")


class KnowledgeContributionSelection(Base):
    __tablename__ = "knowledge_contribution_selections"
    contribution_id: Mapped[str] = mapped_column(
        ForeignKey("concept_contributions.id"), primary_key=True
    )
    block_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    event_id: Mapped[str] = mapped_column(String(36))
