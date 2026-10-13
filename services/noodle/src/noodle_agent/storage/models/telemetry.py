from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_id, utcnow


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


class Attachment(Base):
    __tablename__ = "agent_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    message_id: Mapped[str | None] = mapped_column(String(36), index=True)
    blob_id: Mapped[str] = mapped_column(ForeignKey("blob_objects.id"), index=True)
    original_name: Mapped[str] = mapped_column(Text)
    attachment_type: Mapped[str] = mapped_column(String(30), default="IMAGE")
    status: Mapped[str] = mapped_column(String(30), default="READY", index=True)
    vision_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column()
