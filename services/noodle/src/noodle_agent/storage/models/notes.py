from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id, utcnow


class KnowledgeMapRecord(Base):
    __tablename__ = "knowledge_maps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="VALIDATED")
    summary: Mapped[str] = mapped_column(Text)
    map_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class NoteCandidate(Base):
    __tablename__ = "note_candidates"
    __table_args__ = (UniqueConstraint("knowledge_map_id", "stable_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_map_id: Mapped[str] = mapped_column(ForeignKey("knowledge_maps.id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    failure_json: Mapped[str] = mapped_column(Text, default="[]")
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0)


class QualityEvaluationRecord(Base):
    """Immutable judge verdict bound to the exact candidate content."""

    __tablename__ = "quality_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_map_id",
            "stable_key",
            "content_sha256",
            "evaluator_model",
            "prompt_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_map_id: Mapped[str] = mapped_column(ForeignKey("knowledge_maps.id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(200), index=True)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    evaluator_model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    issues_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AtomicNote(Base):
    __tablename__ = "atomic_notes"
    __table_args__ = (UniqueConstraint("source_version_id", "stable_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(200))
    active_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class NoteVersion(Base):
    __tablename__ = "note_versions"
    __table_args__ = (UniqueConstraint("note_id", "version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_id: Mapped[str] = mapped_column(ForeignKey("atomic_notes.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("note_versions.id"))
    title: Mapped[str] = mapped_column(Text)
    thesis: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    quality_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    review_status: Mapped[str] = mapped_column(String(30), default="UNREVIEWED")
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100), default="agent")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class NoteBlock(Base):
    __tablename__ = "note_blocks"
    __table_args__ = (UniqueConstraint("note_version_id", "block_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    block_key: Mapped[str] = mapped_column(String(200))
    previous_block_id: Mapped[str | None] = mapped_column(ForeignKey("note_blocks.id"))
    parent_block_id: Mapped[str | None] = mapped_column(ForeignKey("note_blocks.id"))
    block_type: Mapped[str] = mapped_column(String(40), default="PARAGRAPH")
    order_index: Mapped[int] = mapped_column(Integer)
    content_text: Mapped[str] = mapped_column(Text)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(30), default="AI_GENERATED")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    page_id: Mapped[str] = mapped_column(ForeignKey("source_pages.id"), index=True)
    document_block_id: Mapped[str | None] = mapped_column(ForeignKey("document_blocks.id"))
    quoted_text: Mapped[str] = mapped_column(Text)
    normalized_text_hash: Mapped[str] = mapped_column(String(64))
    evidence_level: Mapped[str] = mapped_column(String(40), default="MINERU_TEXT_MATCHED")
    confidence: Mapped[float | None] = mapped_column(Float)


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    note_block_id: Mapped[str] = mapped_column(ForeignKey("note_blocks.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    claim_text: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(30), default="OTHER")
    support_status: Mapped[str] = mapped_column(String(30), default="SUPPORTED")


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (UniqueConstraint("claim_id", "span_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id"), index=True)
    span_id: Mapped[str] = mapped_column(ForeignKey("evidence_spans.id"), index=True)
    role: Mapped[str] = mapped_column(String(30), default="PRIMARY")


class NoteAsset(Base):
    __tablename__ = "note_assets"
    __table_args__ = (UniqueConstraint("note_version_id", "visual_asset_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    visual_asset_id: Mapped[str] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    note_block_id: Mapped[str | None] = mapped_column(ForeignKey("note_blocks.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    placement: Mapped[str] = mapped_column(String(20), default="AFTER")
    purpose: Mapped[str] = mapped_column(Text)
    alt_text: Mapped[str] = mapped_column(Text)


class VisualUnderstandingRecord(Base):
    __tablename__ = "visual_understandings"
    __table_args__ = (UniqueConstraint("visual_asset_id", "model", "prompt_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    visual_asset_id: Mapped[str] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)
    prompt_version: Mapped[str] = mapped_column(String(100))
    result_json: Mapped[str] = mapped_column(Text)
    result_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="COMPLETED", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class NoteEmbedding(Base):
    __tablename__ = "note_embeddings"
    __table_args__ = (UniqueConstraint("note_version_id", "model_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(200))
    dimension: Mapped[int] = mapped_column(Integer)
    vector_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    ready: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SourceNote(Base):
    __tablename__ = "source_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), unique=True, index=True)
    active_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SourceNoteVersion(Base):
    __tablename__ = "source_note_versions"
    __table_args__ = (UniqueConstraint("source_note_id", "version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_note_id: Mapped[str] = mapped_column(ForeignKey("source_notes.id"), index=True)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(ForeignKey("source_note_versions.id"))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    learning_goal: Mapped[str] = mapped_column(Text, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="agent")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SourceNoteOutlineNode(Base):
    __tablename__ = "source_note_outline_nodes"
    __table_args__ = (UniqueConstraint("source_note_version_id", "stable_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_note_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_note_versions.id"), index=True
    )
    stable_key: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("source_note_outline_nodes.id"))
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    order_index: Mapped[int] = mapped_column(Integer)
    learning_goal: Mapped[str] = mapped_column(Text, default="")
    source_sections_json: Mapped[str] = mapped_column(Text, default="[]")


class SourceNoteUnitPlacement(Base):
    __tablename__ = "source_note_unit_placements"
    __table_args__ = (
        UniqueConstraint("source_note_version_id", "note_version_id"),
        UniqueConstraint("outline_node_id", "order_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_note_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_note_versions.id"), index=True
    )
    outline_node_id: Mapped[str] = mapped_column(
        ForeignKey("source_note_outline_nodes.id"), index=True
    )
    note_id: Mapped[str] = mapped_column(ForeignKey("atomic_notes.id"), index=True)
    note_version_id: Mapped[str] = mapped_column(ForeignKey("note_versions.id"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
