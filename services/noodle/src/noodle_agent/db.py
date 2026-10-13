from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from sqlite3 import OperationalError as SqliteOperationalError

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class BlobObject(Base):
    __tablename__ = "blob_objects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_backend: Mapped[str] = mapped_column(String(20), default="LOCAL")
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer)
    derived_from_blob_id: Mapped[str | None] = mapped_column(ForeignKey("blob_objects.id"))
    lifecycle_state: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_type: Mapped[str] = mapped_column(String(20), default="PDF")
    title: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    current_version_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class LibraryFolder(Base):
    """User-owned logical folder; independent from the Harness filesystem workspace."""

    __tablename__ = "library_folders"
    __table_args__ = (UniqueConstraint("parent_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("library_folders.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class LibraryEntry(Base):
    """Places one Source in at most one logical folder with stable manual ordering."""

    __tablename__ = "library_entries"
    __table_args__ = (UniqueConstraint("source_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    folder_id: Mapped[str] = mapped_column(ForeignKey("library_folders.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (UniqueConstraint("source_id", "version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    original_blob_id: Mapped[str] = mapped_column(ForeignKey("blob_objects.id"))
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(Text)
    canonical_parse_run_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ParseRun(Base):
    __tablename__ = "parse_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    provider_task_id: Mapped[str | None] = mapped_column(String(200), index=True)
    parser_version: Mapped[str | None] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="SUBMITTED")
    raw_artifact_blob_id: Mapped[str | None] = mapped_column(ForeignKey("blob_objects.id"))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_version_id: Mapped[str] = mapped_column(ForeignKey("source_versions.id"), index=True)
    parse_run_id: Mapped[str | None] = mapped_column(ForeignKey("parse_runs.id"))
    status: Mapped[str] = mapped_column(String(40), default="CREATED", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="CREATED")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    warning_json: Mapped[str] = mapped_column(Text, default="[]")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column()
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SourcePage(Base):
    __tablename__ = "source_pages"
    __table_args__ = (UniqueConstraint("parse_run_id", "page_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    page_index: Mapped[int] = mapped_column(Integer)
    page_label: Mapped[str] = mapped_column(String(50))
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    mapping_status: Mapped[str] = mapped_column(String(30), default="UNVERIFIED")


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (UniqueConstraint("parse_run_id", "external_block_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    page_id: Mapped[str] = mapped_column(ForeignKey("source_pages.id"), index=True)
    external_block_id: Mapped[str] = mapped_column(String(200))
    block_type: Mapped[str] = mapped_column(String(40))
    reading_order: Mapped[int] = mapped_column(Integer)
    text_content: Mapped[str] = mapped_column(Text, default="")
    bbox_json: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class OutlineNode(Base):
    __tablename__ = "outline_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("outline_nodes.id"))
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    start_page_index: Mapped[int] = mapped_column(Integer)
    end_page_index: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(30), default="HEADING")
    confidence: Mapped[float | None] = mapped_column(Float)
    order_index: Mapped[int] = mapped_column(Integer)


class VisualAsset(Base):
    __tablename__ = "visual_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    page_id: Mapped[str] = mapped_column(ForeignKey("source_pages.id"), index=True)
    document_block_id: Mapped[str] = mapped_column(ForeignKey("document_blocks.id"), unique=True)
    blob_id: Mapped[str] = mapped_column(ForeignKey("blob_objects.id"))
    asset_type: Mapped[str] = mapped_column(String(40), default="FIGURE")
    caption: Mapped[str] = mapped_column(Text, default="")
    bbox_json: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(String(30), default="MINERU")
    quality_status: Mapped[str] = mapped_column(String(30), default="USABLE")


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


class NotionPageMap(Base):
    __tablename__ = "notion_page_maps"

    note_id: Mapped[str] = mapped_column(ForeignKey("atomic_notes.id"), primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(100), unique=True)
    database_id: Mapped[str] = mapped_column(String(100))
    last_pushed_version_id: Mapped[str | None] = mapped_column(String(36))
    last_remote_hash: Mapped[str | None] = mapped_column(String(64))
    projected_origin: Mapped[str | None] = mapped_column(String(20))
    projected_evidence: Mapped[str | None] = mapped_column(String(20))
    projected_review: Mapped[str | None] = mapped_column(String(20))
    remote_status: Mapped[str] = mapped_column(String(30), default="UNPUBLISHED")
    last_seen_at: Mapped[datetime | None] = mapped_column()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    risk_level: Mapped[str] = mapped_column(String(10))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[str] = mapped_column(String(100))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class StageEvent(Base):
    """Append-only execution timeline for a persisted document job."""

    __tablename__ = "stage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    progress: Mapped[float | None] = mapped_column(Float)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class LlmCallEvent(Base):
    """Sanitized model-call telemetry; prompt and document bodies are not stored."""

    __tablename__ = "llm_call_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    stage: Mapped[str | None] = mapped_column(String(60), index=True)
    note_key: Mapped[str | None] = mapped_column(String(200), index=True)
    operation: Mapped[str] = mapped_column(String(100), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200), index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), index=True)
    input_sha256: Mapped[str] = mapped_column(String(64))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    input_chars: Mapped[int] = mapped_column(Integer)
    output_chars: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    response_id: Mapped[str | None] = mapped_column(String(200))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class VisionCallEvent(Base):
    """Sanitized vision-call telemetry keyed by immutable image content."""

    __tablename__ = "vision_call_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    stage: Mapped[str | None] = mapped_column(String(60), index=True)
    note_key: Mapped[str | None] = mapped_column(String(200), index=True)
    visual_asset_id: Mapped[str | None] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    operation: Mapped[str] = mapped_column(String(100), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200), index=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), index=True)
    image_sha256: Mapped[str] = mapped_column(String(64), index=True)
    input_sha256: Mapped[str] = mapped_column(String(64))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    response_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    channel: Mapped[str] = mapped_column(String(30), default="WEB")
    title: Mapped[str] = mapped_column(Text, default="新对话")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class AgentAttachment(Base):
    __tablename__ = "agent_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.id"), index=True
    )
    blob_id: Mapped[str] = mapped_column(ForeignKey("blob_objects.id"), index=True)
    original_name: Mapped[str] = mapped_column(Text)
    attachment_type: Mapped[str] = mapped_column(String(30), default="IMAGE")
    status: Mapped[str] = mapped_column(String(30), default="READY", index=True)
    vision_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column()


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="RUNNING", index=True)
    input_text: Mapped[str] = mapped_column(Text)
    final_answer: Mapped[str | None] = mapped_column(Text)
    selected_skill: Mapped[str | None] = mapped_column(String(100))
    context_json: Mapped[str] = mapped_column(Text, default="{}")
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (UniqueConstraint("agent_run_id", "call_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    call_index: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    risk_level: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), index=True)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tool_calls.id"), index=True)
    risk_level: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    decision_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column()


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
    concept_version_id: Mapped[str] = mapped_column(
        ForeignKey("concept_versions.id"), index=True
    )
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
    contribution_id: Mapped[str] = mapped_column(
        ForeignKey("concept_contributions.id"), index=True
    )
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
    contribution_id: Mapped[str] = mapped_column(ForeignKey("concept_contributions.id"), primary_key=True)
    block_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    event_id: Mapped[str] = mapped_column(String(36))


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30.0}
            if url.startswith("sqlite")
            else {},
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        # Configure lock waiting before WAL negotiation. On a cold concurrent
        # start, switching the brand-new database into WAL itself takes a write
        # lock and must wait for the sibling process instead of failing fast.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except SqliteOperationalError as exc:
            # WAL mode is persistent for the database file. During the first
            # simultaneous connections one sibling may already be switching
            # it; that operation reports SQLITE_BUSY without honoring the busy
            # handler. The schema-level BEGIN IMMEDIATE below remains the real
            # serialization barrier, so only this exact transient is safe to
            # defer to the winning connection.
            if "locked" not in str(exc).casefold():
                raise
        cursor.close()

    def init(self) -> None:
        # API and Worker intentionally share one SQLite file and commonly start
        # at the same time. SQLAlchemy's per-table ``checkfirst`` probes are
        # not atomic across processes: two cold starters can both observe a
        # missing table and one then fails with "table ... already exists".
        # Acquire SQLite's write lock before any schema probe/DDL so the second
        # process waits, then observes the schema committed by the first one.
        with self.engine.connect() as connection:
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            Base.metadata.create_all(connection)
            columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(atomic_notes)").fetchall()
            }
            if columns and "source_version_id" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE atomic_notes ADD COLUMN source_version_id VARCHAR(36)"
                )
                connection.exec_driver_sql(
                    """
                    UPDATE atomic_notes
                    SET source_version_id = (
                        SELECT note_versions.source_version_id
                        FROM note_versions
                        WHERE note_versions.note_id = atomic_notes.id
                        ORDER BY note_versions.version_no
                        LIMIT 1
                    )
                    WHERE source_version_id IS NULL
                    """
                )
                connection.exec_driver_sql(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_atomic_note_source_version_stable
                    ON atomic_notes(source_version_id, stable_key)
                    """
                )
            note_block_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(note_blocks)").fetchall()
            }
            if note_block_columns and "content_json" not in note_block_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE note_blocks ADD COLUMN content_json TEXT DEFAULT '{}'"
                )
            note_asset_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(note_assets)").fetchall()
            }
            if note_asset_columns and "note_block_id" not in note_asset_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE note_assets ADD COLUMN note_block_id VARCHAR(36)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_note_assets_note_block_id "
                    "ON note_assets(note_block_id)"
                )
            if note_asset_columns and "placement" not in note_asset_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE note_assets ADD COLUMN placement VARCHAR(20) DEFAULT 'AFTER'"
                )
            concept_asset_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(concept_module_assets)"
                ).fetchall()
            }
            if concept_asset_columns and "content_block_key" not in concept_asset_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE concept_module_assets ADD COLUMN "
                    "content_block_key VARCHAR(220) DEFAULT ''"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_concept_module_assets_content_block_key "
                    "ON concept_module_assets(content_block_key)"
                )
            job_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()
            }
            if job_columns and "lease_owner" not in job_columns:
                connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN lease_owner VARCHAR(120)")
            if job_columns and "lease_expires_at" not in job_columns:
                connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN lease_expires_at DATETIME")
            if job_columns and "heartbeat_at" not in job_columns:
                connection.exec_driver_sql("ALTER TABLE jobs ADD COLUMN heartbeat_at DATETIME")
            backbone_figure_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(backbone_figures)"
                ).fetchall()
            }
            if backbone_figure_columns and "caption_zh" not in backbone_figure_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE backbone_figures ADD COLUMN caption_zh TEXT DEFAULT ''"
                )
            concept_build_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(concept_build_records)"
                ).fetchall()
            }
            if concept_build_columns and "result_status" not in concept_build_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE concept_build_records ADD COLUMN "
                    "result_status VARCHAR(30) DEFAULT 'LEGACY_UNVERIFIED'"
                )
            if concept_build_columns and "fallback_used" not in concept_build_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE concept_build_records ADD COLUMN fallback_used BOOLEAN DEFAULT 0"
                )
            if concept_build_columns and "fallback_detail" not in concept_build_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE concept_build_records ADD COLUMN fallback_detail TEXT DEFAULT ''"
                )
            if concept_build_columns and "output_sha256" not in concept_build_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE concept_build_records ADD COLUMN "
                    "output_sha256 VARCHAR(64) DEFAULT ''"
                )
            concept_job_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(concept_update_jobs)"
                ).fetchall()
            }
            for column, ddl in {
                "result_json": "TEXT DEFAULT '{}'",
                "attempt_count": "INTEGER DEFAULT 0",
                "next_attempt_at": "DATETIME",
                "lease_owner": "VARCHAR(120)",
                "lease_expires_at": "DATETIME",
                "heartbeat_at": "DATETIME",
                "operation": "VARCHAR(30) DEFAULT 'ADD'",
                "supersedes_contribution_id": "VARCHAR(36)",
            }.items():
                if concept_job_columns and column not in concept_job_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE concept_update_jobs ADD COLUMN {column} {ddl}"
                    )
            contribution_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(concept_contributions)"
                ).fetchall()
            }
            for column, ddl in {
                "superseded_by_id": "VARCHAR(36)",
                "updated_at": "DATETIME",
                "decided_at": "DATETIME",
            }.items():
                if contribution_columns and column not in contribution_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE concept_contributions ADD COLUMN {column} {ddl}"
                    )
            if contribution_columns and "updated_at" not in contribution_columns:
                connection.exec_driver_sql(
                    "UPDATE concept_contributions SET updated_at = created_at "
                    "WHERE updated_at IS NULL"
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_jobs_lease_owner ON jobs(lease_owner)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_jobs_lease_expires_at ON jobs(lease_expires_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_build_records_result_status "
                "ON concept_build_records(result_status)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_build_records_output_sha256 "
                "ON concept_build_records(output_sha256)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_update_jobs_lease_owner "
                "ON concept_update_jobs(lease_owner)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_update_jobs_lease_expires_at "
                "ON concept_update_jobs(lease_expires_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_update_jobs_next_attempt_at "
                "ON concept_update_jobs(next_attempt_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_update_jobs_operation "
                "ON concept_update_jobs(operation)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_concept_contributions_superseded_by_id "
                "ON concept_contributions(superseded_by_id)"
            )
            connection.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
                        note_version_id UNINDEXED,
                        note_id UNINDEXED,
                        title,
                        thesis,
                        body,
                        tags,
                        tokenize='unicode61'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(
                        concept_version_id UNINDEXED,
                        concept_id UNINDEXED,
                        title,
                        summary,
                        body,
                        aliases,
                        tokenize='unicode61'
                    )
                    """
                )
            )
            connection.commit()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def healthcheck(self) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1

    def close(self) -> None:
        self.engine.dispose()


def delete_fts_for_note(connection: Session, note_id: str) -> None:
    connection.execute(text("DELETE FROM note_fts WHERE note_id = :note_id"), {"note_id": note_id})


def insert_fts(
    connection: Session,
    *,
    note_version_id: str,
    note_id: str,
    title: str,
    thesis: str,
    body: str,
    tags: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO note_fts(note_version_id, note_id, title, thesis, body, tags)
            VALUES (:version_id, :note_id, :title, :thesis, :body, :tags)
            """
        ),
        {
            "version_id": note_version_id,
            "note_id": note_id,
            "title": title,
            "thesis": thesis,
            "body": body,
            "tags": tags,
        },
    )
