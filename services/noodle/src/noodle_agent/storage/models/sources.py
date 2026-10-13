from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id, utcnow


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
