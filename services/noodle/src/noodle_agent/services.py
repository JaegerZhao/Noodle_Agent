from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from .adapters.blob import LocalBlobStore
from .adapters.llm import build_embedding, build_knowledge_agent
from .adapters.mineru import build_parser, validate_pdf
from .adapters.vision import build_vision_provider, resilient_vision_batch
from .config import Settings
from .db import (
    AgentAttachment,
    AtomicNote,
    AuditEvent,
    BlobObject,
    Claim,
    ClaimEvidence,
    Concept,
    ConceptContribution,
    ConceptModule,
    Database,
    DocumentBlock,
    EvidenceSpan,
    Job,
    KnowledgeMapRecord,
    KnowledgeModuleState,
    KnowledgeSyncEvent,
    LibraryEntry,
    LibraryFolder,
    LlmCallEvent,
    NoteAsset,
    NoteBlock,
    NoteCandidate,
    NoteEmbedding,
    NoteVersion,
    NotionPageMap,
    OutlineNode,
    ParseRun,
    QualityEvaluationRecord,
    Source,
    SourceNote,
    SourceNoteOutlineNode,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    SourcePage,
    SourceVersion,
    StageEvent,
    VisionCallEvent,
    VisualAsset,
    VisualUnderstandingRecord,
    delete_fts_for_note,
    insert_fts,
    utcnow,
)
from .domain import (
    Answer,
    BlockOrigin,
    ConceptCoverage,
    DocumentProfile,
    GenerationBundle,
    JobStage,
    JobStatus,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteBlockDraft,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedDocument,
    PlanIssue,
    QualityStatus,
    ReviewStatus,
    SearchHit,
    SourceNoteOutlineNodeSpec,
    SourceNoteOutlinePlan,
    SourceNotePlanningBundle,
    VisualUnderstanding,
)
from .parse_cache import parser_contract, reuse_parse
from .planning import normalize_note_set_assets
from .quality import (
    deduplicate_asset_placements,
    note_content_sha256,
    required_asset_ids,
    used_asset_ids,
)
from .release import apply_outline_title_patches, build_release_manifest
from .synthesis import (
    BackboneSourceMigrator,
    D2LSourceImporter,
    SemanticSynthesisBuilder,
    SynthesisService,
)
from .synthesis.curation import KnowledgeCurationService
from .synthesis.knowledge_sync import KnowledgeSyncService, blocked_concepts, eligible_note
from .telemetry import TelemetryRecorder
from .visual_routing import (
    batch_by_page_affinity,
    enrich_planned_visual_sequences,
    route_planned_visuals,
)

TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.COMPLETED_WITH_WARNINGS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}

GENERATION_SCHEMA_VERSION = "source-note-generation-v7-occam"
KNOWLEDGE_MAP_CONTRACT_VERSION = "source-candidates-v2"
PLANNING_CONTRACT_VERSION = "source-plan-v2"
WRITING_CONTRACT_VERSION = "note-writer-v11"
JUDGE_CONTRACT_VERSION = "note-judge-v13"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _utc_iso(value: datetime | None) -> str | None:
    """Serialize the database's naive-UTC timestamps without timezone ambiguity."""
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _merged_event_duration_ms(events: list[Any]) -> int:
    """Return wall-clock model time, counting overlapping calls only once."""
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        duration_ms = max(0, int(event.duration_ms or 0))
        if not event.started_at or duration_ms <= 0:
            continue
        intervals.append((event.started_at, event.started_at + timedelta(milliseconds=duration_ms)))
    if not intervals:
        return 0
    intervals.sort(key=lambda item: item[0])
    total_ms = 0
    current_start, current_end = intervals[0]
    for started_at, completed_at in intervals[1:]:
        if started_at <= current_end:
            current_end = max(current_end, completed_at)
            continue
        total_ms += int((current_end - current_start).total_seconds() * 1000)
        current_start, current_end = started_at, completed_at
    return total_ms + int((current_end - current_start).total_seconds() * 1000)


_WARNING_SEVERITIES = {"INFO", "WARNING", "ERROR"}
_INFO_WARNING_CODES = {"SKIPPED_SECTION"}


def _warning_severity(item: dict[str, Any]) -> str:
    """Return the public severity while remaining compatible with old rows."""
    explicit = str(item.get("severity") or "").upper()
    if explicit in _WARNING_SEVERITIES:
        return explicit
    return "INFO" if item.get("code") in _INFO_WARNING_CODES else "WARNING"


def _normalize_warnings(items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        item = dict(raw) if isinstance(raw, dict) else {"detail": str(raw)}
        item["severity"] = _warning_severity(item)
        normalized.append(item)
    return normalized


def _has_material_warnings(items: list[dict[str, Any]]) -> bool:
    return any(_warning_severity(item) in {"WARNING", "ERROR"} for item in items)


def _public_job_status(status: str, warnings: list[dict[str, Any]]) -> str:
    """Downgrade historical info-only warning completions without mutating storage."""
    if (
        status == JobStatus.COMPLETED_WITH_WARNINGS.value
        and warnings
        and not _has_material_warnings(warnings)
    ):
        return JobStatus.COMPLETED.value
    return status


def _safe_filename(value: str, *, fallback: str = "note") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100].rstrip(" .-") or fallback


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


@lru_cache(maxsize=4096)
def _image_dimensions(path: str) -> dict[str, int]:
    import fitz

    try:
        image = fitz.Pixmap(path)
        return {"width": image.width, "height": image.height}
    except (OSError, RuntimeError, ValueError, fitz.mupdf.FzErrorBase):
        # Dimensions are optional layout metadata. A corrupt or unsupported
        # asset must not prevent reading an otherwise valid note.
        return {}


class NoodleService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_directories()
        self.db = Database(settings.database_url)
        self.telemetry = TelemetryRecorder(self.db, settings.data_dir)
        self.blobs = LocalBlobStore(settings.blob_dir)
        self.parser = build_parser(settings)
        self.agent = build_knowledge_agent(settings, self.telemetry)
        self.embedding = build_embedding(settings, self.telemetry)
        self.vision = build_vision_provider(settings, self.telemetry)
        self.synthesis = SynthesisService(self.db, self.agent)
        self.knowledge_curation = KnowledgeCurationService(self.db, self.embedding)
        self.backbone_migrator = BackboneSourceMigrator(self.db, self.blobs)
        self.d2l_importer = D2LSourceImporter(self.db, self.blobs)
        self.semantic_synthesis = SemanticSynthesisBuilder(self.db, self.agent)
        self.knowledge_sync = KnowledgeSyncService(self)

    def init(self) -> None:
        self.db.init()

    def close(self) -> None:
        self.db.close()

    def register_backbone(self, pdf_path: Path) -> dict[str, Any]:
        return self.synthesis.register_backbone(pdf_path).model_dump()

    def extract_backbone_figures(self, corpus_id: str) -> dict[str, Any]:
        return self.synthesis.extract_figures(corpus_id).model_dump()

    def translate_backbone_figure_captions(
        self,
        corpus_id: str,
        *,
        batch_size: int = 16,
        concurrency: int = 4,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.synthesis.translate_figure_captions(
            corpus_id,
            batch_size=batch_size,
            concurrency=concurrency,
            force=force,
        ).model_dump()

    def import_d2l_source(self, repository: Path) -> dict[str, Any]:
        return self.d2l_importer.import_repository(repository).model_dump()

    def migrate_backbone_source(self, corpus_id: str) -> dict[str, Any]:
        return self.backbone_migrator.migrate(corpus_id).model_dump()

    def build_semantic_synthesis(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> dict[str, Any]:
        return self.semantic_synthesis.build(
            corpus_id, source_version_id, concurrency=concurrency
        ).model_dump()

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        return self.semantic_synthesis.refresh_shared_concept_versions()

    def accept_atomic_note_contribution(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self.semantic_synthesis.accept_atomic_note(
            concept_id=concept_id,
            module_key=module_key,
            note_version_id=note_version_id,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
            run_inline=False,
        )

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self.semantic_synthesis.run_concept_update_job(job_id)

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self.semantic_synthesis.get_concept_update_job(job_id)

    def list_concept_update_jobs(self, concept_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self.semantic_synthesis.list_concept_update_jobs(concept_id, limit=limit)

    def list_concept_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        return self.semantic_synthesis.list_contributions(concept_id, module_key=module_key)

    def transition_concept_contribution(self, **kwargs: Any) -> dict[str, Any]:
        return self.semantic_synthesis.transition_contribution(**kwargs)

    def retry_concept_update_job(self, job_id: str, *, actor: str) -> dict[str, Any]:
        return self.semantic_synthesis.retry_concept_update_job(job_id, actor=actor)

    def suggest_concept_candidates(
        self, concept_id: str, module_key: str, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Retrieve proposals only; similarity never grants write authority."""

        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            module = session.scalar(
                select(ConceptModule).where(
                    ConceptModule.concept_id == concept_id,
                    ConceptModule.module_key == module_key,
                    ConceptModule.visibility == "ACTIVE",
                )
            )
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown concept_id: {concept_id}")
            if module is None:
                raise ValueError(f"Unknown active module_key: {module_key}")
            linked = set(
                session.scalars(
                    select(ConceptContribution.atomic_note_version_id).where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.atomic_note_version_id.is_not(None),
                        ConceptContribution.decision_status.notin_(["REJECTED", "RETRACTED"]),
                    )
                ).all()
            )
            title = concept.canonical_name
        query = " ".join(
            item for item in [title, module_key.replace("-", " ")] if item
        )
        hits = self.search(query, limit=max(limit * 3, 12))
        candidates = []
        for hit in hits:
            if hit.note_version_id in linked:
                continue
            pages = sorted(
                {
                    int(item["page_index"]) + 1
                    for item in hit.evidence
                    if item.get("page_index") is not None
                }
            )
            candidates.append(
                {
                    "concept_id": concept_id,
                    "module_key": module_key,
                    "note_id": hit.note_id,
                    "note_version_id": hit.note_version_id,
                    "title": hit.title,
                    "thesis": hit.thesis,
                    "score": round(hit.score, 6),
                    "pages": pages,
                    "evidence_count": len(hit.evidence),
                    "asset_count": len(hit.assets),
                    "proposal_only": True,
                }
            )
            if len(candidates) >= limit:
                break
        return candidates

    def get_concept_update_diff(self, job_id: str) -> dict[str, Any]:
        job = self.get_concept_update_job(job_id)
        candidate_id = job.get("candidate_concept_version_id")
        if not candidate_id:
            return {**job, "diff_available": False, "modules": []}
        before = self.get_concept(
            job["concept_id"], version_id=job["expected_concept_version_id"]
        )
        after = self.get_concept(job["concept_id"], version_id=candidate_id)
        before_by_key = {item["block_key"]: item for item in before["blocks"]}
        after_by_key = {item["block_key"]: item for item in after["blocks"]}
        modules = []
        for key in sorted(set(before_by_key) | set(after_by_key)):
            old = before_by_key.get(key)
            new = after_by_key.get(key)
            old_content = old.get("content", "") if old else ""
            new_content = new.get("content", "") if new else ""
            modules.append(
                {
                    "module_key": key,
                    "title": (new or old or {}).get("title", key),
                    "status": (
                        "ADDED" if old is None else "REMOVED" if new is None else
                        "CHANGED" if old_content != new_content else "UNCHANGED"
                    ),
                    "before": old_content,
                    "after": new_content,
                    "before_module_version_id": old.get("block_version_id") if old else None,
                    "after_module_version_id": new.get("block_version_id") if new else None,
                }
            )
        return {**job, "diff_available": True, "modules": modules}

    def get_backbone_figure_path(self, figure_id: str) -> Path:
        return self.synthesis.get_figure_path(figure_id)

    def enrich_backbone(
        self,
        corpus_id: str,
        *,
        limit: int | None = None,
        concurrency: int | None = None,
        include_published: bool = False,
        long_leaves_only: bool = False,
    ) -> dict[str, Any]:
        return self.synthesis.enrich_pending(
            corpus_id,
            limit=limit,
            concurrency=concurrency or self.settings.draft_concurrency,
            include_published=include_published,
            long_leaves_only=long_leaves_only,
        )

    def list_syntheses(self) -> list[dict[str, Any]]:
        return self.synthesis.list_syntheses()

    def get_synthesis(self, synthesis_id: str) -> dict[str, Any]:
        return self.synthesis.get_synthesis(synthesis_id)

    def get_concept(self, concept_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        resolved_id = concept_id if version_id else self.knowledge_curation.resolve_concept_id(concept_id)
        # A historical pinned version belongs to the pre-merge identity and
        # cannot be applied to its canonical redirect.
        resolved_version = version_id if resolved_id == concept_id else None
        result = self.synthesis.get_concept(resolved_id, version_id=resolved_version)
        with self.db.session() as session:
            identity = session.get(Concept, resolved_id)
            current_read = version_id is None or bool(identity and identity.active_version_id == resolved_version)
            blocked = current_read and resolved_id in blocked_concepts(session)
            sync_status = "READY" if current_read else "HISTORICAL"
            if blocked:
                states = session.scalars(select(KnowledgeModuleState).join(
                    ConceptModule, ConceptModule.id == KnowledgeModuleState.module_id).where(
                    ConceptModule.concept_id == resolved_id, KnowledgeModuleState.dirty.is_(True))).all()
                events = [session.get(KnowledgeSyncEvent, item.event_id) for item in states]
                busy = any(e and e.status in {"QUEUED", "RUNNING"} for e in events)
                sync_status = "REBUILDING" if busy else "NEEDS_REVIEW" if any(
                    e and e.status == "COMPLETED_WITH_REVIEW" for e in events) else "SYNC_FAILED"
        result["sync_status"] = sync_status
        if blocked:
            result["blocks"] = []
            result["figures"] = []
            result["summary"] = ("来源已变更，知识正在同步；暂不提供旧内容。" if sync_status == "REBUILDING" else
                "有效来源重建的候选尚未通过检查，当前知识暂停用于阅读与检索。请在工作台的知识同步中查看原因并重新审校。")
        return result

    def list_concepts(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        return self.knowledge_curation.list_concepts(query, limit=limit)

    def create_synthesis(
        self,
        *,
        stable_key: str,
        domain_key: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self.knowledge_curation.create_synthesis(
            stable_key=stable_key,
            domain_key=domain_key,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def publish_synthesis(
        self,
        synthesis_id: str,
        *,
        expected_version_id: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self.knowledge_curation.publish_synthesis(
            synthesis_id=synthesis_id,
            expected_version_id=expected_version_id,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def merge_concepts(
        self,
        *,
        source_concept_id: str,
        target_concept_id: str,
        expected_source_version_id: str,
        expected_target_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self.knowledge_curation.merge_concepts(
            source_concept_id=source_concept_id,
            target_concept_id=target_concept_id,
            expected_source_version_id=expected_source_version_id,
            expected_target_version_id=expected_target_version_id,
            rationale=rationale,
            actor=actor,
        )

    def __enter__(self) -> Self:
        self.init()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def ingest_pdf(
        self,
        pdf_path: Path,
        *,
        title: str | None = None,
        source_id: str | None = None,
        force_new_version: bool = False,
    ) -> dict[str, str]:
        pdf_path = pdf_path.resolve()
        if not pdf_path.exists():
            raise FileNotFoundError(pdf_path)
        validate_pdf(pdf_path)
        storage_key, sha256, byte_size = self.blobs.put(pdf_path, "application/pdf")
        if self.settings.parser == "fixture":
            sidecar = pdf_path.with_suffix(".mineru.json")
            if sidecar.exists():
                blob_pdf = self.blobs.path_for(storage_key)
                shutil.copyfile(
                    sidecar,
                    blob_pdf.with_suffix(".mineru.json"),
                )
                fixture = _parse_json(sidecar.read_text(encoding="utf-8"), {})
                for page in fixture.get("pages", []):
                    for block in page.get("blocks", []):
                        relative_asset = block.get("asset_path")
                        if not relative_asset:
                            continue
                        source_asset = (sidecar.parent / relative_asset).resolve()
                        if sidecar.parent.resolve() not in source_asset.parents:
                            raise ValueError("Fixture asset escapes the sidecar directory")
                        target_asset = (blob_pdf.parent / relative_asset).resolve()
                        if blob_pdf.parent.resolve() not in target_asset.parents:
                            raise ValueError("Fixture asset escapes the blob directory")
                        target_asset.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source_asset, target_asset)
        with self.db.session() as session:
            blob = session.scalar(
                select(BlobObject).where(
                    BlobObject.content_sha256 == sha256,
                    BlobObject.media_type == "application/pdf",
                )
            )
            if blob is None:
                blob = BlobObject(
                    storage_key=storage_key,
                    content_sha256=sha256,
                    media_type="application/pdf",
                    byte_size=byte_size,
                )
                session.add(blob)
                session.flush()

            if not source_id and not force_new_version:
                existing_version = session.scalar(
                    select(SourceVersion)
                    .join(Source, Source.id == SourceVersion.source_id)
                    .where(SourceVersion.content_sha256 == sha256, Source.visibility == "ACTIVE")
                    .order_by(SourceVersion.created_at.desc())
                )
                if existing_version is not None:
                    existing_job = session.scalar(
                        select(Job)
                        .where(Job.source_version_id == existing_version.id)
                        .order_by(Job.created_at.desc())
                    )
                    if existing_job is not None:
                        return {
                            "source_id": existing_version.source_id,
                            "source_version_id": existing_version.id,
                            "job_id": existing_job.id,
                            "deduplicated": "true",
                        }

            if source_id:
                source = session.get(Source, source_id)
                if source is None or source.visibility != "ACTIVE":
                    raise ValueError(f"Unknown source_id: {source_id}")
            else:
                source = Source(title=title or pdf_path.stem)
                session.add(source)
                session.flush()

            existing = session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.content_sha256 == sha256,
                )
            )
            if existing is not None and not force_new_version:
                existing_job = session.scalar(
                    select(Job)
                    .where(Job.source_version_id == existing.id)
                    .order_by(Job.created_at.desc())
                )
                if existing_job is not None:
                    return {
                        "source_id": source.id,
                        "source_version_id": existing.id,
                        "job_id": existing_job.id,
                        "deduplicated": "true",
                    }

            version_no = (
                session.scalar(
                    select(func.max(SourceVersion.version_no)).where(
                        SourceVersion.source_id == source.id
                    )
                )
                or 0
            ) + 1
            source_version = SourceVersion(
                source_id=source.id,
                version_no=version_no,
                original_blob_id=blob.id,
                content_sha256=sha256,
                byte_size=byte_size,
                original_name=pdf_path.name,
            )
            session.add(source_version)
            session.flush()
            job = Job(source_version_id=source_version.id)
            session.add(job)
            session.add(
                AuditEvent(
                    actor="local-user",
                    action="INGEST_PDF",
                    risk_level="L1",
                    target_type="SourceVersion",
                    target_id=source_version.id,
                    detail_json=_json({"sha256": sha256, "name": pdf_path.name}),
                )
            )
            session.flush()
            return {
                "source_id": source.id,
                "source_version_id": source_version.id,
                "job_id": job.id,
                "deduplicated": "false",
            }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            return self._job_dict(job)

    def get_job_result(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            warnings = _normalize_warnings(_parse_json(job.warning_json, []))
            public_status = _public_job_status(job.status, warnings)
            payload: dict[str, Any] = {
                "job_id": job.id,
                "status": public_status,
                "stage": job.stage,
                "ready": public_status
                in {
                    JobStatus.COMPLETED.value,
                    JobStatus.COMPLETED_WITH_WARNINGS.value,
                },
                "source_version_id": job.source_version_id,
                "warnings": warnings,
                "error_code": job.error_code,
                "error_detail": job.error_detail,
            }
            source_version = session.get(SourceVersion, job.source_version_id)
            if source_version is None:
                return payload
            payload["source_id"] = source_version.source_id
            row = session.execute(
                select(SourceNote, SourceNoteVersion)
                .join(SourceNoteVersion, SourceNoteVersion.source_note_id == SourceNote.id)
                .where(
                    SourceNote.source_id == source_version.source_id,
                    SourceNote.active_version_id == SourceNoteVersion.id,
                    SourceNoteVersion.source_version_id == source_version.id,
                )
            ).first()
            if row is None:
                return payload
            source_note, source_note_version = row
            payload.update(
                {
                    "source_note_id": source_note.id,
                    "source_note_version_id": source_note_version.id,
                    "title": source_note_version.title,
                    "note_count": session.scalar(
                        select(func.count(SourceNoteUnitPlacement.id)).where(
                            SourceNoteUnitPlacement.source_note_version_id == source_note_version.id
                        )
                    )
                    or 0,
                }
            )
            return payload

    def review_existing_release(self, job_id: str) -> dict[str, Any]:
        """Re-run only Source-level editorial review on the canonical release."""

        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
        release_path = normalized_path.parent / "release-v1.json"
        generation_path = normalized_path.parent / "generation-v4.json"
        if not normalized_path.is_file() or not release_path.is_file():
            raise ValueError("Canonical release is unavailable")
        manifest = self._read_generation_artifact(release_path)
        outline = SourceNoteOutlinePlan.model_validate(manifest["outline"])
        note_set = NoteSetPlan.model_validate(manifest["note_set_plan"])
        notes = [
            NoteDraft.model_validate(
                {key: value for key, value in raw.items() if key != "content_sha256"}
            )
            for raw in manifest.get("notes", [])
        ]
        evaluations = [
            NoteEvaluation.model_validate(raw) for raw in manifest.get("evaluations", [])
        ]
        review_release = getattr(self.agent, "review_source_release", None)
        if not callable(review_release):
            raise TypeError("The active knowledge agent cannot review Source releases")
        document = ParsedDocument.model_validate_json(normalized_path.read_text(encoding="utf-8"))
        review = self._invoke_agent(
            job_id,
            JobStage.VALIDATING_NOTES,
            review_release,
            document,
            outline,
            note_set,
            notes,
            note_key="source-release-review-only",
        )
        if review.note_issues or review.global_issues:
            return {
                "job_id": job_id,
                "source_note_id": None,
                "review": review.model_dump(),
                "exports": [],
                "requires_validation_restart": True,
            }
        outline = apply_outline_title_patches(outline, review)
        updated = build_release_manifest(
            outline=outline,
            note_set=note_set,
            notes=notes,
            evaluations=evaluations,
            failures=list(manifest.get("rejected") or []),
            review=review,
        )
        self._write_generation_artifact(release_path, updated)
        generation_payload = self._read_generation_artifact(generation_path)
        generation_payload["final_release"] = updated
        self._write_generation_artifact(generation_path, generation_payload)
        with self.db.session() as session:
            knowledge_map = session.scalar(
                select(KnowledgeMapRecord).where(
                    KnowledgeMapRecord.source_version_id == source_version.id,
                    KnowledgeMapRecord.parse_run_id == job.parse_run_id,
                )
            )
            if knowledge_map is not None:
                knowledge_map.map_json = _json(updated)
                knowledge_map.summary = outline.summary
        source_note_id = self._persist_source_note(
            job_id=job_id,
            source_id=source.id,
            source_version_id=source_version.id,
            outline=outline,
            note_set=note_set,
            passed_notes=notes,
        )
        exports = self.export_markdown(source_version.id)
        return {
            "job_id": job_id,
            "source_note_id": source_note_id,
            "review": review.model_dump(),
            "exports": exports,
        }

    def list_library_folders(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            folders = session.scalars(
                select(LibraryFolder).order_by(
                    LibraryFolder.parent_id,
                    LibraryFolder.sort_order,
                    LibraryFolder.created_at,
                )
            ).all()
            counts = dict(
                session.execute(
                    select(LibraryEntry.folder_id, func.count(LibraryEntry.id)).group_by(
                        LibraryEntry.folder_id
                    )
                ).all()
            )
            return [
                {
                    "id": folder.id,
                    "parent_id": folder.parent_id,
                    "name": folder.name,
                    "sort_order": folder.sort_order,
                    "source_count": int(counts.get(folder.id, 0)),
                    "created_at": _utc_iso(folder.created_at),
                    "updated_at": _utc_iso(folder.updated_at),
                }
                for folder in folders
            ]

    def create_library_folder(
        self,
        name: str,
        *,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Folder name must not be empty")
        with self.db.session() as session:
            if parent_id and session.get(LibraryFolder, parent_id) is None:
                raise ValueError(f"Unknown parent folder: {parent_id}")
            duplicate = session.scalar(
                select(LibraryFolder).where(
                    LibraryFolder.parent_id == parent_id,
                    LibraryFolder.name == clean_name,
                )
            )
            if duplicate is not None:
                raise ValueError("A folder with this name already exists here")
            next_order = session.scalar(
                select(func.max(LibraryFolder.sort_order)).where(
                    LibraryFolder.parent_id == parent_id
                )
            )
            folder = LibraryFolder(
                name=clean_name,
                parent_id=parent_id,
                sort_order=int(next_order or 0) + 1,
            )
            session.add(folder)
            session.flush()
            return {
                "id": folder.id,
                "parent_id": folder.parent_id,
                "name": folder.name,
                "sort_order": folder.sort_order,
                "source_count": 0,
                "created_at": _utc_iso(folder.created_at),
                "updated_at": _utc_iso(folder.updated_at),
            }

    def rename_library_folder(self, folder_id: str, name: str) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Folder name must not be empty")
        with self.db.session() as session:
            folder = session.get(LibraryFolder, folder_id)
            if folder is None:
                raise ValueError(f"Unknown folder: {folder_id}")
            duplicate = session.scalar(
                select(LibraryFolder).where(
                    LibraryFolder.parent_id == folder.parent_id,
                    LibraryFolder.name == clean_name,
                    LibraryFolder.id != folder.id,
                )
            )
            if duplicate is not None:
                raise ValueError("A folder with this name already exists here")
            folder.name = clean_name
            folder.updated_at = utcnow()
            session.flush()
            count = session.scalar(
                select(func.count(LibraryEntry.id)).where(LibraryEntry.folder_id == folder.id)
            )
            return {
                "id": folder.id,
                "parent_id": folder.parent_id,
                "name": folder.name,
                "sort_order": folder.sort_order,
                "source_count": int(count or 0),
                "created_at": _utc_iso(folder.created_at),
                "updated_at": _utc_iso(folder.updated_at),
            }

    def move_library_source(
        self,
        source_id: str,
        *,
        folder_id: str | None,
    ) -> dict[str, Any]:
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise ValueError(f"Unknown source: {source_id}")
            if folder_id and session.get(LibraryFolder, folder_id) is None:
                raise ValueError(f"Unknown folder: {folder_id}")
            entry = session.scalar(select(LibraryEntry).where(LibraryEntry.source_id == source_id))
            if folder_id is None:
                if entry is not None:
                    session.delete(entry)
                return {"source_id": source_id, "folder_id": None, "sort_order": 0}
            next_order = session.scalar(
                select(func.max(LibraryEntry.sort_order)).where(LibraryEntry.folder_id == folder_id)
            )
            if entry is None:
                entry = LibraryEntry(source_id=source_id, folder_id=folder_id)
                session.add(entry)
            entry.folder_id = folder_id
            entry.sort_order = int(next_order or 0) + 1
            entry.updated_at = utcnow()
            session.flush()
            return {
                "source_id": source_id,
                "folder_id": folder_id,
                "sort_order": entry.sort_order,
            }

    def reorder_library_sources(
        self,
        folder_id: str,
        source_ids: list[str],
    ) -> dict[str, Any]:
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_ids contains duplicates")
        with self.db.session() as session:
            if session.get(LibraryFolder, folder_id) is None:
                raise ValueError(f"Unknown folder: {folder_id}")
            entries = session.scalars(
                select(LibraryEntry).where(LibraryEntry.folder_id == folder_id)
            ).all()
            by_source = {entry.source_id: entry for entry in entries}
            if set(source_ids) != set(by_source):
                raise ValueError("source_ids must contain every source in the folder exactly once")
            for index, source_id in enumerate(source_ids, start=1):
                by_source[source_id].sort_order = index
                by_source[source_id].updated_at = utcnow()
            return {"folder_id": folder_id, "source_ids": source_ids}

    def get_library_overview(self) -> dict[str, Any]:
        """Return one compact projection for the workbench; domain tables stay authoritative."""
        with self.db.session() as session:
            sources = session.scalars(
                select(Source)
                .where(Source.visibility == "ACTIVE")
                .order_by(Source.updated_at.desc())
            ).all()
            entries = {item.source_id: item for item in session.scalars(select(LibraryEntry)).all()}
            source_notes = {
                item.source_id: item
                for item in session.scalars(
                    select(SourceNote).where(SourceNote.visibility == "ACTIVE")
                ).all()
            }
            all_jobs = session.scalars(select(Job).order_by(Job.created_at.desc())).all()
            latest_job_by_version: dict[str, Job] = {}
            for job in all_jobs:
                latest_job_by_version.setdefault(job.source_version_id, job)
            llm_tokens = dict(
                session.execute(
                    select(LlmCallEvent.job_id, func.sum(LlmCallEvent.total_tokens))
                    .where(LlmCallEvent.job_id.is_not(None))
                    .group_by(LlmCallEvent.job_id)
                ).all()
            )
            vision_tokens = dict(
                session.execute(
                    select(VisionCallEvent.job_id, func.sum(VisionCallEvent.total_tokens))
                    .where(VisionCallEvent.job_id.is_not(None))
                    .group_by(VisionCallEvent.job_id)
                ).all()
            )
            rows: list[dict[str, Any]] = []
            for source in sources:
                version = (
                    session.get(SourceVersion, source.current_version_id)
                    if source.current_version_id
                    else None
                )
                if version is None:
                    version = session.scalar(
                        select(SourceVersion)
                        .where(SourceVersion.source_id == source.id)
                        .order_by(SourceVersion.version_no.desc())
                        .limit(1)
                    )
                if version is None:
                    continue
                job = latest_job_by_version.get(version.id)
                source_note = source_notes.get(source.id)
                active_release = (
                    session.get(SourceNoteVersion, source_note.active_version_id)
                    if source_note and source_note.active_version_id
                    else None
                )
                entry = entries.get(source.id)
                job_payload = self._job_dict(job) if job else None
                if job_payload is not None:
                    job_payload["total_tokens"] = int(llm_tokens.get(job.id, 0) or 0) + int(
                        vision_tokens.get(job.id, 0) or 0
                    )
                    end = job.updated_at if job.status in TERMINAL_STATUSES else utcnow()
                    job_payload["duration_ms"] = max(
                        0, int((end - job.created_at).total_seconds() * 1000)
                    )
                rows.append(
                    {
                        "source_id": source.id,
                        "source_version_id": version.id,
                        "folder_id": entry.folder_id if entry else None,
                        "sort_order": entry.sort_order if entry else 0,
                        "title": source.title,
                        "source_file": version.original_name,
                        "source_media_type": version.media_type,
                        "source_kind": "PDF" if version.media_type == "application/pdf" else "TEXTBOOK",
                        "original_view_available": version.media_type == "application/pdf",
                        "byte_size": version.byte_size,
                        "created_at": _utc_iso(source.created_at),
                        "updated_at": _utc_iso(source.updated_at),
                        "source_note_id": source_note.id if source_note else None,
                        "source_note_version_id": (active_release.id if active_release else None),
                        "source_note_version_no": (
                            active_release.version_no if active_release else None
                        ),
                        "job": job_payload,
                    }
                )
            return {
                "folders": self.list_library_folders(),
                "sources": rows,
                "jobs": [self._job_dict(job) for job in all_jobs[:50]],
            }

    def get_source_pdf(self, source_id: str) -> dict[str, Any]:
        """Resolve the active source's original PDF without exposing blob storage keys."""
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if source is None or source.visibility != "ACTIVE":
                raise ValueError(f"Unknown source_id: {source_id}")
            version = (
                session.get(SourceVersion, source.current_version_id)
                if source.current_version_id
                else None
            )
            if version is None:
                version = session.scalar(
                    select(SourceVersion)
                    .where(SourceVersion.source_id == source.id)
                    .order_by(SourceVersion.version_no.desc())
                    .limit(1)
                )
            if version is None:
                raise ValueError("Source has no versions")
            if version.media_type != "application/pdf":
                raise ValueError("This source is a textbook chapter, not an original PDF")
            blob = session.get(BlobObject, version.original_blob_id)
            if blob is None:
                raise ValueError("Source PDF blob is missing")
            path = self.blobs.path_for(blob.storage_key)
            if not path.is_file():
                raise FileNotFoundError("Source PDF file is missing")
            return {
                "path": path,
                "filename": version.original_name,
                "media_type": version.media_type or "application/pdf",
                "source_version_id": version.id,
            }

    def list_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.db.session() as session:
            jobs = session.scalars(
                select(Job).order_by(Job.updated_at.desc()).limit(max(1, min(limit, 50)))
            ).all()
            return [self._job_dict(job) for job in jobs]

    def get_job_timeline(self, job_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            stages = session.scalars(
                select(StageEvent)
                .where(StageEvent.job_id == job_id)
                .order_by(StageEvent.started_at)
            ).all()
            llm_calls = session.scalars(
                select(LlmCallEvent)
                .where(LlmCallEvent.job_id == job_id)
                .order_by(LlmCallEvent.started_at)
            ).all()
            vision_calls = session.scalars(
                select(VisionCallEvent)
                .where(VisionCallEvent.job_id == job_id)
                .order_by(VisionCallEvent.started_at)
            ).all()

            def stamp(value):
                return _utc_iso(value)

            model_calls = [*llm_calls, *vision_calls]
            model_cumulative_duration_ms = sum(item.duration_ms or 0 for item in model_calls)

            return {
                "job": self._job_dict(job),
                "summary": {
                    "stage_count": len(stages),
                    "llm_call_count": len(llm_calls),
                    "vision_call_count": len(vision_calls),
                    "llm_duration_ms": sum(item.duration_ms or 0 for item in llm_calls),
                    "vision_duration_ms": sum(item.duration_ms or 0 for item in vision_calls),
                    "model_cumulative_duration_ms": model_cumulative_duration_ms,
                    "model_wall_duration_ms": _merged_event_duration_ms(model_calls),
                    "total_tokens": sum(item.total_tokens or 0 for item in llm_calls)
                    + sum(item.total_tokens or 0 for item in vision_calls),
                },
                "stages": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "status": item.status,
                        "progress": item.progress,
                        "detail": _parse_json(item.detail_json, {}),
                        "started_at": stamp(item.started_at),
                        "completed_at": stamp(item.completed_at),
                        "duration_ms": item.duration_ms,
                    }
                    for item in stages
                ],
                "llm_calls": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "note_key": item.note_key,
                        "operation": item.operation,
                        "model": item.model,
                        "prompt_version": item.prompt_version,
                        "status": item.status,
                        "duration_ms": item.duration_ms,
                        "input_sha256": item.input_sha256,
                        "output_sha256": item.output_sha256,
                        "input_chars": item.input_chars,
                        "output_chars": item.output_chars,
                        "prompt_tokens": item.prompt_tokens,
                        "completion_tokens": item.completion_tokens,
                        "total_tokens": item.total_tokens,
                        "response_id": item.response_id,
                        "error_code": item.error_code,
                        "started_at": stamp(item.started_at),
                    }
                    for item in llm_calls
                ],
                "vision_calls": [
                    {
                        "id": item.id,
                        "trace_id": item.trace_id,
                        "stage": item.stage,
                        "note_key": item.note_key,
                        "visual_asset_id": item.visual_asset_id,
                        "operation": item.operation,
                        "model": item.model,
                        "status": item.status,
                        "duration_ms": item.duration_ms,
                        "image_sha256": item.image_sha256,
                        "total_tokens": item.total_tokens,
                        "response_id": item.response_id,
                        "error_code": item.error_code,
                        "started_at": stamp(item.started_at),
                    }
                    for item in vision_calls
                ],
            }

    def resume_job(self, job_id: str) -> dict[str, Any]:
        resumable_stages = {
            JobStage.MAPPING.value,
            JobStage.GENERATING.value,
            JobStage.PROFILING_DOCUMENT.value,
            JobStage.BUILDING_KNOWLEDGE_MAP.value,
            JobStage.PLANNING_SOURCE_NOTE_OUTLINE.value,
            JobStage.PLANNING_NOTE_SET.value,
            JobStage.REVIEWING_NOTE_SET.value,
            JobStage.REPAIRING_NOTE_SET.value,
            JobStage.ANALYZING_VISUALS.value,
            JobStage.DRAFTING_NOTES.value,
            JobStage.EVALUATING_NOTES.value,
            JobStage.VALIDATING_NOTES.value,
        }
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            if job.status != JobStatus.FAILED.value:
                raise ValueError(f"Only FAILED jobs can be resumed; current status is {job.status}")
            parse_run = session.get(ParseRun, job.parse_run_id) if job.parse_run_id else None
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            can_resume_checkpoint = (
                job.stage not in resumable_stages
                or parse_run is None
                or parse_run.status != "SUCCEEDED"
                or not normalized_path.is_file()
            ) is False
            if not can_resume_checkpoint:
                replacement = Job(source_version_id=job.source_version_id)
                session.add(replacement)
                session.flush()
                session.add(
                    AuditEvent(
                        actor="local-user",
                        action="RETRY_JOB_FROM_ORIGINAL",
                        risk_level="L1",
                        target_type="Job",
                        target_id=replacement.id,
                        detail_json=_json(
                            {
                                "failed_job_id": job.id,
                                "reason": "no_safe_parse_checkpoint",
                            }
                        ),
                    )
                )
                session.flush()
                payload = self._job_dict(replacement)
                payload["retry_mode"] = "from_original"
                payload["replaces_job_id"] = job.id
                return payload
            job.status = JobStatus.RUNNING.value
            job.error_code = None
            job.error_detail = None
            job.next_attempt_at = utcnow()
            session.add(
                AuditEvent(
                    actor="local-user",
                    action="RESUME_JOB",
                    risk_level="L1",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json({"stage": job.stage}),
                )
            )
            session.flush()
            payload = self._job_dict(job)
            payload["retry_mode"] = "checkpoint"
            payload["resumed_from_stage"] = job.stage
            return payload

    def delete_source_local_data(self, source_id: str, *, actor: str = "local-user") -> dict[str, Any]:
        return self.knowledge_sync.delete_source(source_id, actor)

    def _purge_source_local_data(
        self,
        source_id: str,
        *,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        """Remove a source and all Noodle-owned copies without touching the upload origin."""

        del actor

        artifact_directories: list[Path] = []
        export_directories: list[Path] = []
        blob_paths: list[Path] = []
        deleted_counts: dict[str, int] = {}

        def ids(session: Session, statement) -> list[str]:
            return list(session.scalars(statement).all())

        def remove_rows(session: Session, model, condition, key: str) -> None:
            result = session.execute(
                delete(model).where(condition),
                execution_options={"synchronize_session": False},
            )
            deleted_counts[key] = int(result.rowcount or 0)

        with self.db.session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise ValueError(f"Unknown source_id: {source_id}")
            version_ids = ids(
                session,
                select(SourceVersion.id).where(SourceVersion.source_id == source.id),
            )
            if not version_ids:
                raise ValueError("Source has no versions")
            original_blob_ids = set(
                ids(
                    session,
                    select(SourceVersion.original_blob_id).where(SourceVersion.id.in_(version_ids)),
                )
            )
            running = session.scalar(
                select(Job.id).where(
                    Job.source_version_id.in_(version_ids),
                    Job.status.notin_(TERMINAL_STATUSES),
                )
            )
            if running is not None:
                raise ValueError(
                    "Generated data cannot be deleted while a job is still running; "
                    f"active job is {running}"
                )

            jobs = session.scalars(select(Job).where(Job.source_version_id.in_(version_ids))).all()
            job_ids = [item.id for item in jobs]
            parse_ids = ids(
                session,
                select(ParseRun.id).where(ParseRun.source_version_id.in_(version_ids)),
            )
            note_ids = ids(
                session,
                select(AtomicNote.id).where(AtomicNote.source_id == source.id),
            )
            note_version_ids = (
                ids(session, select(NoteVersion.id).where(NoteVersion.note_id.in_(note_ids)))
                if note_ids
                else []
            )
            source_note_ids = ids(
                session,
                select(SourceNote.id).where(SourceNote.source_id == source.id),
            )
            source_note_version_ids = (
                ids(
                    session,
                    select(SourceNoteVersion.id).where(
                        SourceNoteVersion.source_note_id.in_(source_note_ids)
                    ),
                )
                if source_note_ids
                else []
            )
            knowledge_map_ids = ids(
                session,
                select(KnowledgeMapRecord.id).where(
                    KnowledgeMapRecord.source_version_id.in_(version_ids)
                ),
            )
            visual_assets = (
                session.scalars(
                    select(VisualAsset).where(VisualAsset.parse_run_id.in_(parse_ids))
                ).all()
                if parse_ids
                else []
            )
            visual_asset_ids = [item.id for item in visual_assets]
            derived_blob_ids = {item.blob_id for item in visual_assets}
            raw_blob_ids = (
                ids(
                    session,
                    select(ParseRun.raw_artifact_blob_id).where(
                        ParseRun.id.in_(parse_ids),
                        ParseRun.raw_artifact_blob_id.is_not(None),
                    ),
                )
                if parse_ids
                else []
            )
            derived_blob_ids.update(raw_blob_ids)

            for parse_id in parse_ids:
                artifact_directories.append(self.settings.artifact_dir / parse_id)
            for version_id in version_ids:
                export_directories.extend(
                    path
                    for path in self.settings.export_dir.glob(f"*--{version_id[:8]}--*")
                    if path.is_dir()
                )

            for note_id in note_ids:
                delete_fts_for_note(session, note_id)
            if source_note_version_ids:
                remove_rows(
                    session,
                    SourceNoteUnitPlacement,
                    SourceNoteUnitPlacement.source_note_version_id.in_(source_note_version_ids),
                    "source_note_unit_placements",
                )
                remove_rows(
                    session,
                    SourceNoteOutlineNode,
                    SourceNoteOutlineNode.source_note_version_id.in_(source_note_version_ids),
                    "source_note_outline_nodes",
                )
                remove_rows(
                    session,
                    SourceNoteVersion,
                    SourceNoteVersion.id.in_(source_note_version_ids),
                    "source_note_versions",
                )
            if source_note_ids:
                remove_rows(
                    session,
                    SourceNote,
                    SourceNote.id.in_(source_note_ids),
                    "source_notes",
                )
            if note_ids:
                remove_rows(
                    session,
                    NotionPageMap,
                    NotionPageMap.note_id.in_(note_ids),
                    "notion_page_maps",
                )
            if note_version_ids:
                claim_ids = ids(
                    session,
                    select(Claim.id).where(Claim.note_version_id.in_(note_version_ids)),
                )
                if claim_ids:
                    remove_rows(
                        session,
                        ClaimEvidence,
                        ClaimEvidence.claim_id.in_(claim_ids),
                        "claim_evidence",
                    )
                remove_rows(
                    session,
                    NoteAsset,
                    NoteAsset.note_version_id.in_(note_version_ids),
                    "note_assets",
                )
                remove_rows(
                    session,
                    NoteEmbedding,
                    NoteEmbedding.note_version_id.in_(note_version_ids),
                    "note_embeddings",
                )
                remove_rows(
                    session,
                    Claim,
                    Claim.note_version_id.in_(note_version_ids),
                    "claims",
                )
                remove_rows(
                    session,
                    NoteBlock,
                    NoteBlock.note_version_id.in_(note_version_ids),
                    "note_blocks",
                )
                remove_rows(
                    session,
                    NoteVersion,
                    NoteVersion.id.in_(note_version_ids),
                    "note_versions",
                )
            if note_ids:
                remove_rows(
                    session,
                    AtomicNote,
                    AtomicNote.id.in_(note_ids),
                    "atomic_notes",
                )
            if knowledge_map_ids:
                remove_rows(
                    session,
                    QualityEvaluationRecord,
                    QualityEvaluationRecord.knowledge_map_id.in_(knowledge_map_ids),
                    "quality_evaluations",
                )
                remove_rows(
                    session,
                    NoteCandidate,
                    NoteCandidate.knowledge_map_id.in_(knowledge_map_ids),
                    "note_candidates",
                )
                remove_rows(
                    session,
                    KnowledgeMapRecord,
                    KnowledgeMapRecord.id.in_(knowledge_map_ids),
                    "knowledge_maps",
                )
            if visual_asset_ids:
                remove_rows(
                    session,
                    VisualUnderstandingRecord,
                    VisualUnderstandingRecord.visual_asset_id.in_(visual_asset_ids),
                    "visual_understandings",
                )
                remove_rows(
                    session,
                    VisionCallEvent,
                    VisionCallEvent.visual_asset_id.in_(visual_asset_ids),
                    "orphan_vision_calls",
                )
            if parse_ids:
                span_ids = ids(
                    session,
                    select(EvidenceSpan.id).where(EvidenceSpan.parse_run_id.in_(parse_ids)),
                )
                if span_ids:
                    remove_rows(
                        session,
                        ClaimEvidence,
                        ClaimEvidence.span_id.in_(span_ids),
                        "orphan_claim_evidence",
                    )
                remove_rows(
                    session,
                    EvidenceSpan,
                    EvidenceSpan.parse_run_id.in_(parse_ids),
                    "evidence_spans",
                )
                remove_rows(
                    session,
                    VisualAsset,
                    VisualAsset.parse_run_id.in_(parse_ids),
                    "visual_assets",
                )
                remove_rows(
                    session,
                    DocumentBlock,
                    DocumentBlock.parse_run_id.in_(parse_ids),
                    "document_blocks",
                )
                remove_rows(
                    session,
                    OutlineNode,
                    OutlineNode.parse_run_id.in_(parse_ids),
                    "outline_nodes",
                )
                remove_rows(
                    session,
                    SourcePage,
                    SourcePage.parse_run_id.in_(parse_ids),
                    "source_pages",
                )
            if job_ids:
                remove_rows(
                    session,
                    StageEvent,
                    StageEvent.job_id.in_(job_ids),
                    "stage_events",
                )
                remove_rows(
                    session,
                    LlmCallEvent,
                    LlmCallEvent.job_id.in_(job_ids),
                    "llm_call_events",
                )
                remove_rows(
                    session,
                    VisionCallEvent,
                    VisionCallEvent.job_id.in_(job_ids),
                    "vision_call_events",
                )
                remove_rows(session, Job, Job.id.in_(job_ids), "jobs")
            if parse_ids:
                remove_rows(
                    session,
                    ParseRun,
                    ParseRun.id.in_(parse_ids),
                    "parse_runs",
                )
            related_target_ids = {
                source.id,
                *version_ids,
                *job_ids,
                *parse_ids,
                *note_ids,
                *note_version_ids,
                *source_note_ids,
                *source_note_version_ids,
                *knowledge_map_ids,
                *visual_asset_ids,
            }
            if related_target_ids:
                remove_rows(
                    session,
                    AuditEvent,
                    AuditEvent.target_id.in_(related_target_ids),
                    "audit_events",
                )
            remove_rows(
                session,
                LibraryEntry,
                LibraryEntry.source_id == source.id,
                "library_entries",
            )
            remove_rows(
                session,
                SourceVersion,
                SourceVersion.id.in_(version_ids),
                "source_versions",
            )
            remove_rows(session, Source, Source.id == source.id, "sources")

            candidate_blob_ids = derived_blob_ids | original_blob_ids
            referenced_blob_ids = set(ids(session, select(SourceVersion.original_blob_id)))
            referenced_blob_ids.update(
                ids(
                    session,
                    select(ParseRun.raw_artifact_blob_id).where(
                        ParseRun.raw_artifact_blob_id.is_not(None)
                    ),
                )
            )
            referenced_blob_ids.update(ids(session, select(VisualAsset.blob_id)))
            referenced_blob_ids.update(ids(session, select(AgentAttachment.blob_id)))
            referenced_blob_ids.update(
                ids(
                    session,
                    select(BlobObject.derived_from_blob_id).where(
                        BlobObject.derived_from_blob_id.is_not(None)
                    ),
                )
            )
            removable_blob_ids = candidate_blob_ids - referenced_blob_ids
            if removable_blob_ids:
                removable_blobs = session.scalars(
                    select(BlobObject).where(BlobObject.id.in_(removable_blob_ids))
                ).all()
                for blob in removable_blobs:
                    blob_paths.append(self.blobs.path_for(blob.storage_key))
                remove_rows(
                    session,
                    BlobObject,
                    BlobObject.id.in_(removable_blob_ids),
                    "blob_objects",
                )

        filesystem_warnings: list[str] = []
        try:
            self.telemetry.purge_jobs(job_ids)
        except OSError as exc:
            filesystem_warnings.append(f"Telemetry cleanup: {exc}")
        for directory in [*artifact_directories, *export_directories]:
            try:
                if directory.is_dir():
                    shutil.rmtree(directory)
            except OSError as exc:
                filesystem_warnings.append(f"{directory}: {exc}")
        for path in blob_paths:
            try:
                path.unlink(missing_ok=True)
                path.with_suffix(".mineru.json").unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            except OSError as exc:
                filesystem_warnings.append(f"{path}: {exc}")
        return {
            "source_id": source_id,
            "source_deleted": True,
            "external_original_untouched": True,
            "deleted_counts": deleted_counts,
            "filesystem_warnings": filesystem_warnings,
        }

    def restart_generation(
        self,
        job_id: str,
        from_step: str,
        *,
        actor: str = "local-user",
    ) -> dict[str, Any]:
        keep_keys = {
            "profile": {"schema_version"},
            "knowledge-map": {"schema_version", "profile"},
            "outline": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
            },
            "note-set": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "source_note_outline",
            },
            "vision": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
            },
            "drafts": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
            },
            "evaluation": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
                "drafts",
            },
            "validation": {
                "schema_version",
                "profile",
                "knowledge_windows",
                "local_concepts",
                "knowledge_skipped",
                "knowledge_map",
                "planning_bundle",
                "source_note_outline",
                "initial_note_set_plan",
                "initial_note_set_review",
                "note_set_plan",
                "note_set_review",
                "visual_understandings",
                "visual_analysis_complete",
                "drafts",
                "evaluations",
                "note_boundary_changes",
                "note_asset_reclassifications",
                "visual_sequence_additions",
            },
        }
        if from_step not in keep_keys:
            raise ValueError(f"Unknown generation restart step: {from_step}")
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            restartable_statuses = {
                JobStatus.FAILED.value,
                JobStatus.COMPLETED.value,
                JobStatus.COMPLETED_WITH_WARNINGS.value,
            }
            if job.status not in restartable_statuses:
                raise ValueError(
                    "Generation can only be restarted from a terminal generated job; "
                    f"current status is {job.status}"
                )
            parse_run = session.get(ParseRun, job.parse_run_id) if job.parse_run_id else None
            state = _parse_json(job.state_json, {})
            normalized_path = Path(str(state.get("normalized_path") or ""))
            artifact_path = normalized_path.parent / "generation-v4.json"
            if (
                parse_run is None
                or parse_run.status != "SUCCEEDED"
                or not normalized_path.is_file()
                or not artifact_path.is_file()
            ):
                raise ValueError("Generation artifact is unavailable")
            payload = self._read_generation_artifact(artifact_path)
            retained = {key: value for key, value in payload.items() if key in keep_keys[from_step]}
            for contract_key in (
                "knowledge_map_contract_version",
                "planning_contract_version",
                "writing_contract_version",
                "judge_contract_version",
            ):
                if contract_key in payload:
                    retained[contract_key] = payload[contract_key]
            retained["schema_version"] = GENERATION_SCHEMA_VERSION
            retained.setdefault("drafts", {})
            self._write_generation_artifact(artifact_path, retained)
            stage_by_step = {
                "profile": JobStage.PROFILING_DOCUMENT,
                "knowledge-map": JobStage.BUILDING_KNOWLEDGE_MAP,
                "outline": JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                "note-set": JobStage.PLANNING_NOTE_SET,
                "vision": JobStage.ANALYZING_VISUALS,
                "drafts": JobStage.DRAFTING_NOTES,
                "evaluation": JobStage.EVALUATING_NOTES,
                "validation": JobStage.VALIDATING_NOTES,
            }
            job.stage = stage_by_step[from_step].value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.46
            job.error_code = None
            job.error_detail = None
            job.next_attempt_at = utcnow()
            for key in (
                "generation_step",
                "current_note_key",
                "current_window_key",
                "drafted_note_count",
                "completed_window_count",
            ):
                state.pop(key, None)
            state["generation_artifact"] = str(artifact_path)
            state["restart_from"] = from_step
            job.state_json = _json(state)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="RESTART_GENERATION",
                    risk_level="L1",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json({"from_step": from_step}),
                )
            )
            session.flush()
            return self._job_dict(job)

    @staticmethod
    def _job_dict(job: Job) -> dict[str, Any]:
        state = _parse_json(job.state_json, {})
        warnings = _normalize_warnings(_parse_json(job.warning_json, []))
        end = job.updated_at if job.status in TERMINAL_STATUSES else utcnow()
        return {
            "id": job.id,
            "source_version_id": job.source_version_id,
            "parse_run_id": job.parse_run_id,
            "status": _public_job_status(job.status, warnings),
            "stage": job.stage,
            "progress": job.progress,
            "warnings": warnings,
            "error_code": job.error_code,
            "error_detail": job.error_detail,
            "next_attempt_at": _utc_iso(job.next_attempt_at),
            "created_at": _utc_iso(job.created_at),
            "updated_at": _utc_iso(job.updated_at),
            "duration_ms": max(0, int((end - job.created_at).total_seconds() * 1000)),
            "harness": state.get("harness", {}),
        }

    def link_job_to_harness(
        self,
        job_id: str,
        *,
        session_id: str | None,
        tool_call_id: str | None,
    ) -> None:
        """Persist the originating Harness identities without coupling schemas."""
        if not session_id and not tool_call_id:
            return
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise ValueError(f"Unknown job_id: {job_id}")
            state = _parse_json(job.state_json, {})
            state["harness"] = {
                "session_id": session_id or "",
                "tool_call_id": tool_call_id or "",
                "linked_at": _utc_iso(utcnow()),
            }
            job.state_json = _json(state)
            session.add(
                AuditEvent(
                    actor=(f"harness-session:{session_id}" if session_id else "harness"),
                    action="LINK_HARNESS_JOB",
                    risk_level="L0",
                    target_type="Job",
                    target_id=job.id,
                    detail_json=_json(state["harness"]),
                )
            )

    def run_job_once(self, job_id: str, *, force_poll: bool = False) -> dict[str, Any]:
        try:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise ValueError(f"Unknown job_id: {job_id}")
                if job.status in TERMINAL_STATUSES:
                    return self._job_dict(job)
                if (
                    job.status == JobStatus.WAITING_EXTERNAL.value
                    and job.next_attempt_at
                    and job.next_attempt_at > utcnow()
                    and not force_poll
                ):
                    return self._job_dict(job)
                stage = job.stage

            if stage == JobStage.CREATED.value:
                self._submit_parse(job_id)
            self._poll_or_normalize(job_id)
            with self.db.session() as session:
                job = session.get(Job, job_id)
                generation_stages = {
                    JobStage.MAPPING.value,
                    JobStage.GENERATING.value,
                    JobStage.PROFILING_DOCUMENT.value,
                    JobStage.BUILDING_KNOWLEDGE_MAP.value,
                    JobStage.PLANNING_SOURCE_NOTE_OUTLINE.value,
                    JobStage.PLANNING_NOTE_SET.value,
                    JobStage.REVIEWING_NOTE_SET.value,
                    JobStage.REPAIRING_NOTE_SET.value,
                    JobStage.ANALYZING_VISUALS.value,
                    JobStage.DRAFTING_NOTES.value,
                    JobStage.EVALUATING_NOTES.value,
                    JobStage.VALIDATING_NOTES.value,
                }
                if job is not None and job.stage in generation_stages:
                    pass
                else:
                    return self._job_dict(job)
            self._generate_and_activate(job_id)
        # The persisted job boundary must convert every adapter/domain failure
        # into a diagnosable terminal state instead of losing the exception.
        except Exception as exc:  # noqa: BLE001
            self._fail_job(job_id, exc)
        return self.get_job(job_id)

    def run_job_until_terminal(
        self, job_id: str, *, timeout_seconds: float = 1800.0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self.run_job_once(job_id, force_poll=True)
            if result["status"] in TERMINAL_STATUSES:
                return result
            wait_for = max(0.2, self.settings.mineru_poll_seconds)
            time.sleep(wait_for)
        raise TimeoutError(f"Job {job_id} did not finish in {timeout_seconds} seconds")

    def _submit_parse(self, job_id: str) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            blob = session.get(BlobObject, source_version.original_blob_id)
            pdf_path = self.blobs.path_for(blob.storage_key)
            request_hash = _sha256_text(
                _json(
                    {
                        "source_version": source_version.id,
                        "provider": self.parser.name,
                        "content": source_version.content_sha256,
                    }
                )
            )
            parse_run = ParseRun(
                source_version_id=source_version.id,
                provider=self.parser.name,
                request_hash=request_hash,
                status="SUBMITTING",
            )
            session.add(parse_run)
            session.flush()
            job.parse_run_id = parse_run.id
            job.stage = JobStage.SUBMITTING_PARSE.value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.05

        self.telemetry.transition_stage(
            job_id,
            JobStage.SUBMITTING_PARSE.value,
            0.05,
            {"provider": self.parser.name},
        )
        if reuse_parse(self, job_id):
            self.telemetry.transition_stage(job_id, JobStage.MAPPING.value, 0.4, {"parse_cache_hit": True})
            return
        submission = self.parser.submit(pdf_path, source_version.id)
        with self.db.session() as session:
            job = session.get(Job, job_id)
            parse_run = session.get(ParseRun, job.parse_run_id)
            parse_run.provider_task_id = submission["external_id"]
            parse_run.status = "WAITING"
            job.stage = JobStage.WAITING_PARSE.value
            state = {"parser_result": submission.get("result") or {}}
            job.state_json = _json(state)
            if submission.get("state") == "done":
                job.status = JobStatus.RUNNING.value
                job.next_attempt_at = utcnow()
            else:
                job.status = JobStatus.WAITING_EXTERNAL.value
                job.next_attempt_at = utcnow() + timedelta(
                    seconds=self.settings.mineru_poll_seconds
                )
            job.progress = 0.12
        self.telemetry.transition_stage(
            job_id,
            JobStage.WAITING_PARSE.value,
            0.12,
            {
                "provider": self.parser.name,
                "external_state": str(submission.get("state") or "pending"),
            },
        )

    def _poll_or_normalize(self, job_id: str) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.stage != JobStage.WAITING_PARSE.value:
                return
            parse_run = session.get(ParseRun, job.parse_run_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
            state = _parse_json(job.state_json, {})
            external_id = parse_run.provider_task_id
            result = state.get("parser_result") or {}

        if reuse_parse(self, job_id):
            self.telemetry.transition_stage(job_id, JobStage.MAPPING.value, 0.4, {"parse_cache_hit": True})
            return
        if not result or result.get("state") not in {"done", "failed"}:
            result = self.parser.poll(external_id)
        external_state = str(result.get("state", "pending")).lower()
        if external_state in {"waiting-file", "uploading", "pending", "running", "converting"}:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                job.status = JobStatus.WAITING_EXTERNAL.value
                job.next_attempt_at = utcnow() + timedelta(
                    seconds=self.settings.mineru_poll_seconds
                )
                job.state_json = _json({"parser_result": result})
                job.progress = min(0.28, max(job.progress, 0.15))
            self.telemetry.transition_stage(
                job_id,
                JobStage.WAITING_PARSE.value,
                min(0.28, max(job.progress, 0.15)),
                {"provider": self.parser.name, "external_state": external_state},
            )
            return
        if external_state == "failed":
            raise RuntimeError(
                f"MinerU parse failed: {result.get('err_code')} {result.get('err_msg')}"
            )
        if external_state != "done":
            raise RuntimeError(f"Unknown parser state: {external_state}")

        self.telemetry.transition_stage(
            job_id,
            JobStage.NORMALIZING.value,
            0.3,
            {"provider": self.parser.name, "external_state": external_state},
        )
        artifact_dir = self.settings.artifact_dir / parse_run.id
        document = self.parser.download_and_normalize(
            external_id, result, artifact_dir, source.title
        )
        normalized_path = artifact_dir / "normalized.json"
        normalized_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        self._persist_parsed_document(job_id, document, normalized_path)

    def _persist_parsed_document(
        self, job_id: str, document: ParsedDocument, normalized_path: Path
    ) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            parse_run = session.get(ParseRun, job.parse_run_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            blob = session.get(BlobObject, source_version.original_blob_id)
            actual_pages = validate_pdf(self.blobs.path_for(blob.storage_key))
            warnings = _parse_json(job.warning_json, [])
            markdown_only = (document.parser_version or "").endswith("-markdown")
            if actual_pages and document.page_count != actual_pages:
                if markdown_only:
                    warnings.append(
                        {
                            "code": "PAGE_PRECISION_UNAVAILABLE",
                            "detail": (
                                f"Markdown-only parser result reports {document.page_count} page(s); "
                                f"PDF has {actual_pages}. Use MINERU_MODE=precision for page evidence."
                            ),
                        }
                    )
                    document.page_count = actual_pages
                else:
                    raise RuntimeError(
                        f"Global page mapping mismatch: PDF={actual_pages}, parser={document.page_count}"
                    )
            page_by_index: dict[int, SourcePage] = {}
            page_labels = {block.page_index: block.page_label for block in document.blocks}
            for page_index in range(document.page_count):
                page = SourcePage(
                    parse_run_id=parse_run.id,
                    page_index=page_index,
                    page_label=page_labels.get(page_index, str(page_index + 1)),
                    mapping_status="UNAVAILABLE" if markdown_only else "VERIFIED",
                )
                session.add(page)
                session.flush()
                page_by_index[page_index] = page
            heading_rows: list[tuple[DocumentBlock, int]] = []
            for order, block in enumerate(document.blocks):
                page_index = min(max(block.page_index, 0), document.page_count - 1)
                row = DocumentBlock(
                    parse_run_id=parse_run.id,
                    page_id=page_by_index[page_index].id,
                    external_block_id=block.block_id,
                    block_type=block.block_type,
                    reading_order=order,
                    text_content=block.text,
                    bbox_json=_json(block.bbox) if block.bbox else None,
                    metadata_json=_json(block.metadata),
                )
                session.add(row)
                session.flush()
                if block.asset_path:
                    asset_path = Path(block.asset_path)
                    if asset_path.exists() and asset_path.is_file():
                        media_type = (
                            mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
                        )
                        storage_key, sha256, byte_size = self.blobs.put(asset_path, media_type)
                        asset_blob = session.scalar(
                            select(BlobObject).where(
                                BlobObject.content_sha256 == sha256,
                                BlobObject.media_type == media_type,
                            )
                        )
                        if asset_blob is None:
                            asset_blob = BlobObject(
                                storage_key=storage_key,
                                content_sha256=sha256,
                                media_type=media_type,
                                byte_size=byte_size,
                            )
                            session.add(asset_blob)
                            session.flush()
                        session.add(
                            VisualAsset(
                                parse_run_id=parse_run.id,
                                page_id=page_by_index[page_index].id,
                                document_block_id=row.id,
                                blob_id=asset_blob.id,
                                asset_type=block.block_type,
                                caption=block.text,
                                bbox_json=_json(block.bbox) if block.bbox else None,
                            )
                        )
                if block.block_type in {"TITLE", "HEADING"}:
                    heading_rows.append((row, int(block.level or 1)))
            for order, (row, level) in enumerate(heading_rows):
                page = session.get(SourcePage, row.page_id)
                next_page = (
                    session.get(SourcePage, heading_rows[order + 1][0].page_id).page_index
                    if order + 1 < len(heading_rows)
                    else document.page_count - 1
                )
                session.add(
                    OutlineNode(
                        parse_run_id=parse_run.id,
                        title=row.text_content,
                        level=level,
                        start_page_index=page.page_index,
                        end_page_index=max(page.page_index, next_page),
                        order_index=order,
                    )
                )
            parse_run.status = "SUCCEEDED"
            parse_run.parser_version = document.parser_version
            parse_run.completed_at = utcnow()
            source_version.canonical_parse_run_id = parse_run.id
            job.stage = JobStage.MAPPING.value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.4
            warnings = _normalize_warnings(warnings)
            job.warning_json = _json(warnings)
            state = {"normalized_path": str(normalized_path)}
            if document.parser_name == "mineru":
                state["parse_cache_contract"] = parser_contract(self)
            job.state_json = _json(state)
        self.telemetry.transition_stage(
            job_id,
            JobStage.MAPPING.value,
            0.4,
            {
                "page_count": document.page_count,
                "block_count": len(document.blocks),
                "outline_node_count": len(heading_rows),
            },
        )

    def _update_generation_stage(
        self,
        job_id: str,
        stage: JobStage,
        progress: float,
        **state_updates: Any,
    ) -> None:
        should_transition = False
        effective_progress = progress
        with self.db.session() as session:
            job = session.get(Job, job_id)
            state = _parse_json(job.state_json, {})
            state.update(state_updates)
            job.state_json = _json(state)
            current_progress = float(job.progress or 0.0)
            effective_progress = max(current_progress, progress)
            should_transition = progress >= current_progress
            if should_transition:
                job.stage = stage.value
            job.status = JobStatus.RUNNING.value
            job.progress = effective_progress
            job.error_code = None
            job.error_detail = None
        # Parallel visual and drafting work may report completion out of order.
        # Ignore a late callback from an earlier phase instead of moving the
        # public timeline backwards or reopening an earlier stage.
        if should_transition:
            self.telemetry.transition_stage(job_id, stage.value, effective_progress, state_updates)

    def _invoke_agent(
        self,
        job_id: str,
        stage: JobStage,
        method,
        *args,
        note_key: str | None = None,
        **kwargs,
    ):
        with self.telemetry.bind(
            job_id=job_id,
            stage=stage.value,
            note_key=note_key,
        ):
            return method(*args, **kwargs)

    def _invoke_agent_with_task_retries(
        self,
        job_id: str,
        stage: JobStage,
        method,
        *args,
        note_key: str | None = None,
        **kwargs,
    ):
        """Retry high-value structured tasks that may return an empty body."""

        last_error: Exception | None = None
        for attempt in range(self.settings.generation_task_retries + 1):
            try:
                return self._invoke_agent(
                    job_id,
                    stage,
                    method,
                    *args,
                    note_key=note_key,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < self.settings.generation_task_retries:
                    time.sleep(min(2.0**attempt, 3.0))
        raise RuntimeError(
            f"Agent task {note_key or method.__name__} failed after retries: {last_error}"
        ) from last_error

    @staticmethod
    def _read_generation_artifact(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"schema_version": GENERATION_SCHEMA_VERSION, "drafts": {}}
        return _parse_json(path.read_text(encoding="utf-8"), {})

    @staticmethod
    def _write_generation_artifact(path: Path, payload: dict[str, Any]) -> None:
        _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _analyze_planned_visuals(
        self,
        job_id: str,
        document: ParsedDocument,
        note_set: NoteSetPlan,
    ) -> dict[str, dict[str, Any]]:
        # A resumed visual stage must replace, rather than accumulate, warnings
        # from an earlier attempt. Current failures are appended below and are
        # preserved by final publication.
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.warning_json = _json(
                    [
                        item
                        for item in _parse_json(job.warning_json, [])
                        if item.get("code") != "VISION_ANALYSIS_FAILED"
                    ]
                )
        if not getattr(self.vision, "enabled", False):
            return {}
        planned_ids = list(
            dict.fromkeys(block_id for spec in note_set.notes for block_id in spec.asset_block_ids)
        )
        blocks_by_id = {block.block_id: block for block in document.blocks}
        planned = [
            blocks_by_id[block_id]
            for block_id in planned_ids
            if block_id in blocks_by_id
            and blocks_by_id[block_id].asset_path
            and Path(str(blocks_by_id[block_id].asset_path)).is_file()
        ]
        if not planned:
            return {}

        with self.db.session() as session:
            job = session.get(Job, job_id)
            rows = session.execute(
                select(DocumentBlock.external_block_id, VisualAsset.id)
                .join(VisualAsset, VisualAsset.document_block_id == DocumentBlock.id)
                .where(DocumentBlock.parse_run_id == job.parse_run_id)
            ).all()
            visual_ids = {external_id: visual_id for external_id, visual_id in rows}
            cached_rows = session.scalars(
                select(VisualUnderstandingRecord).where(
                    VisualUnderstandingRecord.model == self.vision.model_name,
                    VisualUnderstandingRecord.prompt_version == self.vision.prompt_version,
                    VisualUnderstandingRecord.status == "COMPLETED",
                )
            ).all()
            cached_by_id = {
                row.visual_asset_id: VisualUnderstanding.model_validate_json(row.result_json)
                for row in cached_rows
            }
            cached_by_hash = {
                row.image_sha256: VisualUnderstanding.model_validate_json(row.result_json)
                for row in cached_rows
            }

        results: dict[str, VisualUnderstanding] = {}
        pending: list[dict[str, Any]] = []
        block_positions = {block.block_id: index for index, block in enumerate(document.blocks)}
        routes = route_planned_visuals(document, planned)
        local_decorative_count = 0
        local_metadata_count = 0
        cached_count = 0
        seen_pending_hash: dict[str, str] = {}
        duplicate_aliases: dict[str, list[tuple[Any, str]]] = {}
        for block in planned:
            visual_id = visual_ids.get(block.block_id, "")
            if not visual_id:
                continue
            route = routes[block.block_id]
            image_sha256 = route.image_sha256
            cached_result = cached_by_id.get(visual_id) or cached_by_hash.get(image_sha256)
            if cached_result is not None:
                results[block.block_id] = cached_result.model_copy(
                    update={
                        "visual_asset_id": visual_id,
                        "external_block_id": block.block_id,
                        "image_sha256": image_sha256,
                    }
                )
                cached_count += 1
                continue
            if route.route == "LOCAL_DECORATIVE":
                results[block.block_id] = VisualUnderstanding(
                    visual_asset_id=visual_id,
                    external_block_id=block.block_id,
                    image_sha256=image_sha256,
                    model="local-visual-router",
                    prompt_version="local-visual-router-v1",
                    asset_type="DECORATIVE",
                    is_decorative=True,
                    caption="",
                    teaching_role="",
                    confidence=0.99,
                    analysis_tier="LOCAL_DECORATIVE",
                    route_reason=route.reason,
                    is_fragment=False,
                    group_id=route.affinity_group,
                    group_type=route.group_type,
                    related_asset_ids=list(route.related_block_ids),
                )
                local_decorative_count += 1
                continue
            if route.route == "LOCAL_METADATA":
                results[block.block_id] = VisualUnderstanding(
                    visual_asset_id=visual_id,
                    external_block_id=block.block_id,
                    image_sha256=image_sha256,
                    model="mineru-metadata",
                    prompt_version="local-visual-router-v1",
                    asset_type="OTHER",
                    is_decorative=False,
                    essential=False,
                    caption=route.source_caption,
                    teaching_role="来源已提供图片说明",
                    confidence=0.9,
                    analysis_tier="LOCAL_METADATA",
                    route_reason=route.reason,
                    is_fragment=False,
                    group_id=route.affinity_group,
                    group_type=route.group_type,
                    related_asset_ids=list(route.related_block_ids),
                )
                local_metadata_count += 1
                continue
            representative = seen_pending_hash.get(image_sha256)
            if representative:
                duplicate_aliases.setdefault(representative, []).append((block, visual_id))
                continue
            seen_pending_hash[image_sha256] = block.block_id
            position = block_positions[block.block_id]
            neighbors = document.blocks[max(0, position - 3) : position + 4]
            context = {
                "document_title": document.title,
                "page": block.page_label,
                "bbox": block.bbox,
                "affinity_group": route.affinity_group,
                "group_type": route.group_type,
                "related_asset_ids": list(route.related_block_ids),
                "same_page_assets": [
                    {"asset_id": item.block_id, "bbox": item.bbox}
                    for item in planned
                    if item.page_index == block.page_index
                ],
                "neighbor_text": [
                    {
                        "type": item.block_type,
                        "text": item.text[:600],
                    }
                    for item in neighbors
                    if item.text
                ][:4],
                "planned_notes": [
                    {"stable_key": spec.stable_key, "title": spec.title}
                    for spec in note_set.notes
                    if block.block_id in spec.asset_block_ids
                ],
            }
            pending.append({"block": block, "visual_asset_id": visual_id, "context": context})

        if pending:
            # Crops can omit panel labels. Supply a shared original-page view
            # when a selected page contains multiple visual fragments.
            from collections import Counter

            import pymupdf as fitz
            counts = Counter(item["block"].page_index for item in pending)
            with self.db.session() as session:
                job = session.get(Job, job_id)
                version = session.get(SourceVersion, job.source_version_id)
                blob = session.get(BlobObject, version.original_blob_id)
                pdf_path = self.blobs.path_for(blob.storage_key)
                preview_dir = self.settings.artifact_dir / job.parse_run_id / "vision-context"
            preview_dir.mkdir(parents=True, exist_ok=True)
            with fitz.open(pdf_path) as pdf:
                for page_index in sorted(counts):
                    if counts[page_index] < 2:
                        continue
                    preview = preview_dir / f"page-{page_index + 1}.png"
                    if not preview.exists():
                        pdf[page_index].get_pixmap(matrix=fitz.Matrix(1, 1)).save(preview)
                    for item in pending:
                        if item["block"].page_index == page_index:
                            item["page_context_path"] = preview
            batches = batch_by_page_affinity(pending, self.settings.vision_batch_size)
            self._update_generation_stage(
                job_id,
                JobStage.ANALYZING_VISUALS,
                0.63,
                generation_step="understand_planned_visuals",
                planned_visual_count=len(planned),
                cached_visual_count=cached_count,
                local_decorative_count=local_decorative_count,
                local_metadata_count=local_metadata_count,
                api_visual_count=len(pending),
                api_batch_count=len(batches),
            )

            def analyze_batch(batch: list[dict[str, Any]]):
                items = [{
                    "image_path": Path(str(item["block"].asset_path)),
                    "external_block_id": item["block"].block_id,
                    "visual_asset_id": item["visual_asset_id"],
                    "context": item["context"],
                    "page_context_path": item.get("page_context_path"),
                } for item in batch]

                def call(part):
                    with self.telemetry.bind(
                        job_id=job_id, stage=JobStage.ANALYZING_VISUALS.value,
                        note_key="+".join(item["external_block_id"] for item in part),
                    ):
                        return self.vision.analyze_batch(part)

                return resilient_vision_batch(items, call, max_retries=self.settings.vision_max_retries)

            failures: list[dict[str, str]] = []
            with ThreadPoolExecutor(
                max_workers=min(self.settings.vision_concurrency, len(batches)),
                thread_name_prefix="noodle-vision",
            ) as executor:
                future_map = {executor.submit(analyze_batch, batch): batch for batch in batches}
                for future in as_completed(future_map):
                    batch = future_map[future]
                    try:
                        batch_results, batch_failures = future.result()
                        failures.extend(batch_failures)
                    except Exception as exc:  # noqa: BLE001
                        failures.extend(
                            {
                                "block_id": item["block"].block_id,
                                "error": str(exc)[:500],
                            }
                            for item in batch
                        )
                        continue
                    expanded: list[VisualUnderstanding] = list(batch_results.values())
                    for understanding in list(expanded):
                        for alias_block, alias_visual_id in duplicate_aliases.get(
                            understanding.external_block_id, []
                        ):
                            expanded.append(
                                understanding.model_copy(
                                    update={
                                        "visual_asset_id": alias_visual_id,
                                        "external_block_id": alias_block.block_id,
                                        "route_reason": "exact-image duplicate reused in batch",
                                    }
                                )
                            )
                    for understanding in expanded:
                        results[understanding.external_block_id] = understanding
                        result_json = understanding.model_dump_json()
                        with self.db.session() as session:
                            existing = session.scalar(
                                select(VisualUnderstandingRecord).where(
                                    VisualUnderstandingRecord.visual_asset_id
                                    == understanding.visual_asset_id,
                                    VisualUnderstandingRecord.model == understanding.model,
                                    VisualUnderstandingRecord.prompt_version
                                    == understanding.prompt_version,
                                )
                            )
                            if existing is None:
                                session.add(
                                    VisualUnderstandingRecord(
                                        visual_asset_id=understanding.visual_asset_id,
                                        image_sha256=understanding.image_sha256,
                                        model=understanding.model,
                                        prompt_version=understanding.prompt_version,
                                        result_json=result_json,
                                        result_sha256=_sha256_text(result_json),
                                        status="COMPLETED",
                                    )
                                )
            if failures:
                with self.db.session() as session:
                    job = session.get(Job, job_id)
                    warnings = _parse_json(job.warning_json, [])
                    warnings.extend(
                        {
                            "code": "VISION_ANALYSIS_FAILED",
                            "asset": item["block_id"],
                            "detail": item["error"],
                        }
                        for item in failures
                    )
                    job.warning_json = _json(warnings)
        return {key: item.model_dump() for key, item in results.items()}

    def _draft_specs_concurrently(
        self,
        *,
        job_id: str,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        specs: list[NoteSpec],
        vision_context: dict[str, dict[str, Any]],
        artifact_path: Path,
        payload: dict[str, Any],
        drafts: dict[str, Any],
    ) -> dict[str, Any]:
        """Draft a bounded subset; callers may overlap text-only work with vision."""

        if not specs:
            return drafts
        total = max(1, len(note_set.notes))
        self._update_generation_stage(
            job_id,
            JobStage.DRAFTING_NOTES,
            0.66,
            generation_artifact=str(artifact_path),
            generation_step="draft_notes_concurrently",
            drafted_note_count=len(drafts),
            planned_note_count=len(note_set.notes),
            draft_concurrency=min(self.settings.draft_concurrency, len(specs)),
        )

        def draft_one(spec: NoteSpec) -> NoteDraft:
            last_error: Exception | None = None
            for attempt in range(self.settings.generation_task_retries + 1):
                try:
                    return self._invoke_agent(
                        job_id,
                        JobStage.DRAFTING_NOTES,
                        self.agent.draft_note,
                        document,
                        profile,
                        knowledge_map,
                        note_set,
                        spec,
                        vision_context=vision_context,
                        note_key=spec.stable_key,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < self.settings.generation_task_retries:
                        time.sleep(min(2.0**attempt, 3.0))
            raise RuntimeError(
                f"Draft {spec.stable_key} failed after retries: {last_error}"
            ) from last_error

        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.settings.draft_concurrency, len(specs)),
            thread_name_prefix="noodle-draft",
        ) as executor:
            future_map = {executor.submit(draft_one, spec): spec for spec in specs}
            for future in as_completed(future_map):
                spec = future_map[future]
                try:
                    draft = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append({"stable_key": spec.stable_key, "error": str(exc)[:500]})
                    continue
                drafts[spec.stable_key] = draft.model_dump()
                payload["drafts"] = drafts
                self._write_generation_artifact(artifact_path, payload)
                self._update_generation_stage(
                    job_id,
                    JobStage.DRAFTING_NOTES,
                    0.66 + 0.16 * len(drafts) / total,
                    generation_artifact=str(artifact_path),
                    generation_step="draft_notes_concurrently",
                    current_note_key=spec.stable_key,
                    drafted_note_count=len(drafts),
                    planned_note_count=len(note_set.notes),
                )
        if failures:
            raise RuntimeError(
                "Draft failures after retries: "
                + "; ".join(f"{item['stable_key']}={item['error']}" for item in failures)
            )
        return drafts

    def _evaluate_notes_batched(
        self,
        job_id: str,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        """Judge small, bounded batches without changing final note order."""
        specs_by_key = {item.stable_key: item for item in note_set.notes}
        notes = list(plan.notes)
        batch_size = self.settings.judge_batch_size
        complex_kinds = {"FORMULA", "COMPARISON", "CASE", "PROCEDURE", "EXAMPLE"}

        def is_complex(note: NoteDraft) -> bool:
            spec = specs_by_key.get(note.stable_key)
            return bool(
                spec
                and (
                    spec.note_kind.upper() in complex_kinds
                    or spec.asset_block_ids
                    or len(spec.concept_keys) >= 3
                    or len(spec.evidence_block_ids) >= 10
                )
            )

        simple_notes = [note for note in notes if not is_complex(note)]
        complex_notes = [note for note in notes if is_complex(note)]
        batches = [
            simple_notes[index : index + batch_size]
            for index in range(0, len(simple_notes), batch_size)
        ]
        # Dense evidence and vision catalogs make cross-note omissions easy to
        # hallucinate. Complex notes are cheap to judge independently once
        # DeepSeek thinking is disabled, and can still run concurrently.
        batches.extend([[note] for note in complex_notes])
        if not batches:
            return []

        def judge_batch(batch_index: int, batch_notes: list[NoteDraft]):
            batch_keys = {item.stable_key for item in batch_notes}
            batch_specs = [specs_by_key[key] for key in batch_keys if key in specs_by_key]
            concept_keys = {
                concept_key for spec in batch_specs for concept_key in spec.concept_keys
            }
            batch_map = KnowledgeMap(
                document_summary=knowledge_map.document_summary,
                concepts=[
                    concept
                    for concept in knowledge_map.concepts
                    if concept.stable_key in concept_keys
                ],
                skipped_sections=[],
            )
            batch_set = NoteSetPlan(
                document_summary=note_set.document_summary,
                notes=batch_specs,
            )
            batch_plan = KnowledgePlan(
                document_summary=plan.document_summary,
                notes=batch_notes,
            )
            last_error: Exception | None = None
            for attempt in range(self.settings.generation_task_retries + 1):
                try:
                    return self._invoke_agent(
                        job_id,
                        JobStage.EVALUATING_NOTES,
                        self.agent.evaluate_plan,
                        document,
                        batch_set,
                        batch_plan,
                        batch_map,
                        vision_context,
                        note_key=f"judge-batch-{batch_index + 1}",
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < self.settings.generation_task_retries:
                        time.sleep(min(2.0**attempt, 3.0))
            raise RuntimeError(
                f"Judge batch {batch_index + 1} failed after retries: {last_error}"
            ) from last_error

        by_key: dict[str, NoteEvaluation] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.settings.judge_concurrency, len(batches)),
            thread_name_prefix="noodle-judge",
        ) as executor:
            futures = {
                executor.submit(judge_batch, index, batch): index
                for index, batch in enumerate(batches)
            }
            failures: list[str] = []
            for completed, future in enumerate(as_completed(futures), start=1):
                batch_index = futures[future]
                try:
                    batch_evaluations = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"batch-{batch_index + 1}: {exc}")
                    batch_evaluations = []
                for evaluation in batch_evaluations:
                    by_key[evaluation.stable_key] = evaluation
                self._update_generation_stage(
                    job_id,
                    JobStage.EVALUATING_NOTES,
                    0.84 + 0.08 * completed / len(batches),
                    generation_step="evaluate_note_batches",
                    completed_judge_batches=completed,
                    judge_batch_count=len(batches),
                )
        if failures:
            raise RuntimeError("; ".join(failures))
        return [
            by_key.get(
                note.stable_key,
                NoteEvaluation(
                    stable_key=note.stable_key,
                    passed=False,
                    issues=["JUDGE_MISSING_RESULT: 审校批次没有返回该候选"],
                    content_sha256=note_content_sha256(note),
                    evaluator_model=str(getattr(self.agent, "pro_model", self.agent.name)),
                    prompt_version="note-judge-v12",
                ),
            )
            for note in notes
        ]

    def _build_generation_resumable(
        self,
        job_id: str,
        document: ParsedDocument,
        normalized_path: Path,
    ) -> GenerationBundle:
        artifact_path = normalized_path.parent / "generation-v4.json"
        payload = self._read_generation_artifact(artifact_path)

        knowledge_dependents = {
            "local_concepts",
            "knowledge_skipped",
            "knowledge_map",
            "planning_bundle",
            "source_note_outline",
            "initial_note_set_plan",
            "initial_note_set_review",
            "note_set_plan",
            "note_set_review",
            "note_set_repaired",
            "note_boundary_changes",
            "note_asset_reclassifications",
            "visual_sequence_additions",
            "visual_understandings",
            "visual_analysis_complete",
            "drafts",
            "evaluations",
            "final_release",
        }
        planning_dependents = {
            "planning_bundle",
            "source_note_outline",
            "initial_note_set_plan",
            "initial_note_set_review",
            "note_set_plan",
            "note_set_review",
            "note_set_repaired",
            "note_boundary_changes",
            "note_asset_reclassifications",
            "visual_sequence_additions",
            "visual_understandings",
            "visual_analysis_complete",
            "drafts",
            "evaluations",
            "final_release",
        }
        writing_dependents = {"drafts", "evaluations", "final_release"}
        contracts_changed = False
        if payload.get("knowledge_map_contract_version") != KNOWLEDGE_MAP_CONTRACT_VERSION:
            for key in knowledge_dependents:
                payload.pop(key, None)
            payload["knowledge_map_contract_version"] = KNOWLEDGE_MAP_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("planning_contract_version") != PLANNING_CONTRACT_VERSION:
            for key in planning_dependents:
                payload.pop(key, None)
            payload["planning_contract_version"] = PLANNING_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("writing_contract_version") != WRITING_CONTRACT_VERSION:
            for key in writing_dependents:
                payload.pop(key, None)
            payload["writing_contract_version"] = WRITING_CONTRACT_VERSION
            contracts_changed = True
        if payload.get("judge_contract_version") != JUDGE_CONTRACT_VERSION:
            payload.pop("evaluations", None)
            payload.pop("final_release", None)
            payload["judge_contract_version"] = JUDGE_CONTRACT_VERSION
            contracts_changed = True
        payload["schema_version"] = GENERATION_SCHEMA_VERSION
        if contracts_changed:
            self._write_generation_artifact(artifact_path, payload)

        if not payload.get("profile"):
            self._update_generation_stage(
                job_id,
                JobStage.PROFILING_DOCUMENT,
                0.46,
                generation_artifact=str(artifact_path),
                generation_step="profile_document",
            )
            profile = self._invoke_agent(
                job_id, JobStage.PROFILING_DOCUMENT, self.agent.profile_document, document
            )
            payload["profile"] = profile.model_dump()
            self._write_generation_artifact(artifact_path, payload)
        else:
            profile = DocumentProfile.model_validate(payload["profile"])

        if not payload.get("knowledge_map"):
            window_builder = getattr(self.agent, "knowledge_windows", None)
            window_extractor = getattr(self.agent, "extract_knowledge_window", None)
            map_merger = getattr(self.agent, "merge_knowledge_map", None)
            if all(callable(item) for item in (window_builder, window_extractor, map_merger)):
                windows = [
                    KnowledgeWindow.model_validate(item)
                    for item in (
                        payload.get("knowledge_windows")
                        or [item.model_dump() for item in window_builder(document, profile)]
                    )
                ]
                payload["knowledge_windows"] = [item.model_dump() for item in windows]
                local_by_window = dict(payload.get("local_concepts") or {})
                skipped = list(payload.get("knowledge_skipped") or [])
                total_windows = max(1, len(windows))
                pending_windows = [
                    window for window in windows if window.stable_key not in local_by_window
                ]

                def extract_one(window: KnowledgeWindow):
                    last_error: Exception | None = None
                    for attempt in range(self.settings.generation_task_retries + 1):
                        try:
                            return self._invoke_agent(
                                job_id,
                                JobStage.BUILDING_KNOWLEDGE_MAP,
                                window_extractor,
                                document,
                                profile,
                                window,
                                note_key=window.stable_key,
                            )
                        except Exception as exc:  # noqa: BLE001
                            last_error = exc
                            if attempt < self.settings.generation_task_retries:
                                time.sleep(min(2.0**attempt, 8.0))
                    raise RuntimeError(
                        f"Knowledge window {window.stable_key} failed after retries: {last_error}"
                    ) from last_error

                if pending_windows:
                    self._update_generation_stage(
                        job_id,
                        JobStage.BUILDING_KNOWLEDGE_MAP,
                        0.47,
                        generation_artifact=str(artifact_path),
                        generation_step="extract_knowledge_windows_concurrently",
                        completed_window_count=len(local_by_window),
                        knowledge_window_count=len(windows),
                        knowledge_concurrency=min(
                            self.settings.knowledge_concurrency, len(pending_windows)
                        ),
                    )
                    with ThreadPoolExecutor(
                        max_workers=min(self.settings.knowledge_concurrency, len(pending_windows)),
                        thread_name_prefix="noodle-knowledge",
                    ) as executor:
                        futures = {
                            executor.submit(extract_one, window): window
                            for window in pending_windows
                        }
                        extraction_failures: list[str] = []
                        for future in as_completed(futures):
                            window = futures[future]
                            try:
                                concepts, skip_reason = future.result()
                            except Exception as exc:  # noqa: BLE001
                                extraction_failures.append(f"{window.stable_key}: {str(exc)[:500]}")
                                continue
                            local_by_window[window.stable_key] = [
                                item.model_dump() for item in concepts
                            ]
                            if not concepts:
                                skipped.append(
                                    {
                                        "section": window.segment_title,
                                        "reason": str(skip_reason or "LOW_INFORMATION"),
                                    }
                                )
                            payload["local_concepts"] = local_by_window
                            payload["knowledge_skipped"] = skipped
                            self._write_generation_artifact(artifact_path, payload)
                            self._update_generation_stage(
                                job_id,
                                JobStage.BUILDING_KNOWLEDGE_MAP,
                                0.47 + 0.07 * len(local_by_window) / total_windows,
                                generation_artifact=str(artifact_path),
                                generation_step="extract_knowledge_windows_concurrently",
                                current_window_key=window.stable_key,
                                completed_window_count=len(local_by_window),
                                knowledge_window_count=len(windows),
                            )
                    if extraction_failures:
                        raise RuntimeError(
                            "Knowledge extraction failures: " + "; ".join(extraction_failures)
                        )
                self._update_generation_stage(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    0.55,
                    generation_artifact=str(artifact_path),
                    generation_step="materialize_candidate_map",
                    current_window_key=None,
                )
                local_concepts = [
                    KnowledgeConceptCandidate.model_validate(item)
                    for window in windows
                    for item in local_by_window.get(window.stable_key, [])
                ]
                knowledge_map = map_merger(document, profile, local_concepts, skipped)
            else:
                self._update_generation_stage(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    0.5,
                    generation_artifact=str(artifact_path),
                    generation_step="build_knowledge_map",
                )
                knowledge_map = self._invoke_agent(
                    job_id,
                    JobStage.BUILDING_KNOWLEDGE_MAP,
                    self.agent.build_knowledge_map,
                    document,
                    profile,
                )
            payload["knowledge_map"] = knowledge_map.model_dump()
            self._write_generation_artifact(artifact_path, payload)
        else:
            knowledge_map = KnowledgeMap.model_validate(payload["knowledge_map"])

        if not payload.get("planning_bundle"):
            self._update_generation_stage(
                job_id,
                JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                0.56,
                generation_artifact=str(artifact_path),
                generation_step="plan_source_v2",
            )
            planning_bundle = self._invoke_agent_with_task_retries(
                job_id,
                JobStage.PLANNING_SOURCE_NOTE_OUTLINE,
                self.agent.plan_source_note_bundle,
                document,
                profile,
                knowledge_map,
                note_key="source-note-planning",
            )
            source_note_outline = planning_bundle.source_note_outline
            initial_note_set = planning_bundle.note_set_plan
            payload["planning_bundle"] = planning_bundle.model_dump()
            payload["source_note_outline"] = source_note_outline.model_dump()
            payload["initial_note_set_plan"] = initial_note_set.model_dump()
            self._write_generation_artifact(artifact_path, payload)
        else:
            planning_bundle = SourceNotePlanningBundle.model_validate(payload["planning_bundle"])
            source_note_outline = planning_bundle.source_note_outline
            initial_note_set = planning_bundle.note_set_plan

        source_note_outline, initial_note_set, outline_depth_repairs = (
            self._normalize_minimum_outline_depth(
                source_note_outline,
                initial_note_set,
            )
        )
        planning_bundle.source_note_outline = source_note_outline
        planning_bundle.note_set_plan = initial_note_set
        payload["planning_bundle"] = planning_bundle.model_dump()
        payload["source_note_outline"] = source_note_outline.model_dump()
        payload["initial_note_set_plan"] = initial_note_set.model_dump()
        if outline_depth_repairs:
            payload["deterministic_outline_depth_repairs"] = outline_depth_repairs
        self._write_generation_artifact(artifact_path, payload)

        # A knowledge unit may now contain several tightly related concepts.
        # Do not automatically split HISTORY from its parent topic; the combined
        # planner owns semantic grouping. Only deterministic asset corrections
        # and missing visual-sequence recovery remain mandatory.
        note_set = initial_note_set
        note_set, asset_reclassifications = normalize_note_set_assets(document, note_set)
        if asset_reclassifications:
            payload["note_set_plan"] = note_set.model_dump()
            payload["note_asset_reclassifications"] = asset_reclassifications
            payload.pop("evaluations", None)
            self._write_generation_artifact(artifact_path, payload)
        note_set, visual_additions = enrich_planned_visual_sequences(document, note_set)
        if visual_additions:
            concepts_by_key = {concept.stable_key: concept for concept in knowledge_map.concepts}
            specs_by_key = {spec.stable_key: spec for spec in note_set.notes}
            for note_key, added_ids in visual_additions.items():
                spec = specs_by_key.get(note_key)
                if spec is None or not spec.concept_keys:
                    continue
                added = set(added_ids)
                planned_anchors = set(spec.asset_block_ids) - added
                owners = [
                    concepts_by_key[key]
                    for key in spec.concept_keys
                    if key in concepts_by_key
                    and planned_anchors.intersection(concepts_by_key[key].asset_block_ids)
                ]
                if not owners:
                    first = concepts_by_key.get(spec.concept_keys[0])
                    owners = [first] if first is not None else []
                if owners:
                    owners[0].asset_block_ids = list(
                        dict.fromkeys([*owners[0].asset_block_ids, *added_ids])
                    )
            payload["note_set_plan"] = note_set.model_dump()
            # Fragment restoration is a deterministic expansion of a
            # planner-selected visual sequence. Persist it in the owning
            # Candidate so subsequent validation, writing and provenance all
            # share one explicit asset contract.
            payload["knowledge_map"] = knowledge_map.model_dump()
            payload["visual_sequence_additions"] = visual_additions
            self._write_generation_artifact(artifact_path, payload)

        source_note_outline, note_set, contract_repairs = self._normalize_combined_plan_contracts(
            document,
            knowledge_map,
            source_note_outline,
            note_set,
        )
        if contract_repairs:
            planning_bundle.source_note_outline = source_note_outline
            planning_bundle.note_set_plan = note_set
            payload["planning_bundle"] = planning_bundle.model_dump()
            payload["source_note_outline"] = source_note_outline.model_dump()
            payload["note_set_plan"] = note_set.model_dump()
            payload["deterministic_plan_repairs"] = contract_repairs
            self._write_generation_artifact(artifact_path, payload)

        source_note_outline = self._prune_unbound_outline_leaves(source_note_outline, note_set)
        planning_bundle.source_note_outline = source_note_outline
        planning_bundle.note_set_plan = note_set
        payload["planning_bundle"] = planning_bundle.model_dump()
        payload["source_note_outline"] = source_note_outline.model_dump()
        payload["note_set_plan"] = note_set.model_dump()
        self._write_generation_artifact(artifact_path, payload)

        outline_errors = self._validate_source_note_outline(source_note_outline)
        if outline_errors:
            raise RuntimeError(
                "Source Note planning bundle failed outline validation: "
                + "; ".join(outline_errors)
            )

        initial_review = self._deterministic_note_set_review(knowledge_map, note_set)
        plan_errors = self._validate_note_set(
            document,
            knowledge_map,
            note_set,
            initial_review,
            source_note_outline,
        )
        note_set_review = initial_review

        payload["initial_note_set_review"] = initial_review.model_dump()
        payload["note_set_plan"] = note_set.model_dump()
        payload["note_set_review"] = note_set_review.model_dump()
        self._write_generation_artifact(artifact_path, payload)
        if plan_errors:
            raise RuntimeError(
                "Final note-set plan failed deterministic validation: " + "; ".join(plan_errors)
            )

        if knowledge_map.concepts and not note_set.notes:
            raise RuntimeError(
                "The note-set planner returned no notes for a non-empty knowledge map"
            )

        drafts = dict(payload.get("drafts") or {})
        visual_context = dict(payload.get("visual_understandings") or {})
        if not payload.get("visual_analysis_complete"):
            # Text-only notes do not depend on image captions. Draft them while
            # the external vision provider works, instead of imposing a global
            # visual barrier on the whole document.
            text_only_specs = [
                spec
                for spec in note_set.notes
                if not spec.asset_block_ids and spec.stable_key not in drafts
            ]
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="noodle-visual-stage"
            ) as executor:
                visual_future = executor.submit(
                    self._analyze_planned_visuals, job_id, document, note_set
                )
                drafts = self._draft_specs_concurrently(
                    job_id=job_id,
                    document=document,
                    profile=profile,
                    knowledge_map=knowledge_map,
                    note_set=note_set,
                    specs=text_only_specs,
                    vision_context={},
                    artifact_path=artifact_path,
                    payload=payload,
                    drafts=drafts,
                )
                visual_context = visual_future.result()
            payload["visual_understandings"] = visual_context
            payload["visual_analysis_complete"] = True
            payload["schema_version"] = GENERATION_SCHEMA_VERSION
            self._write_generation_artifact(artifact_path, payload)

        pending_specs = [spec for spec in note_set.notes if spec.stable_key not in drafts]
        drafts = self._draft_specs_concurrently(
            job_id=job_id,
            document=document,
            profile=profile,
            knowledge_map=knowledge_map,
            note_set=note_set,
            specs=pending_specs,
            vision_context=visual_context,
            artifact_path=artifact_path,
            payload=payload,
            drafts=drafts,
        )

        final_plan = KnowledgePlan(
            document_summary=note_set.document_summary,
            notes=[NoteDraft.model_validate(drafts[spec.stable_key]) for spec in note_set.notes],
            skipped_sections=knowledge_map.skipped_sections,
        )
        final_plan, table_shape_repairs = self._normalize_table_shapes(final_plan)
        if table_shape_repairs:
            drafts.update({note.stable_key: note.model_dump() for note in final_plan.notes})
            payload["drafts"] = drafts
            payload["deterministic_table_shape_repairs"] = table_shape_repairs
            self._write_generation_artifact(artifact_path, payload)
        prevalidated_notes, deterministic_failures = self._validate_plan(
            document,
            final_plan,
            note_set,
            visual_context,
        )
        payload["prejudge_validation_failures"] = deterministic_failures
        judge_plan = KnowledgePlan(
            document_summary=final_plan.document_summary,
            notes=prevalidated_notes,
            skipped_sections=final_plan.skipped_sections,
        )
        if not payload.get("evaluations"):
            self._update_generation_stage(
                job_id,
                JobStage.EVALUATING_NOTES,
                0.82,
                generation_artifact=str(artifact_path),
                generation_step="evaluate_plan",
                current_note_key=None,
                drafted_note_count=len(drafts),
            )
            evaluations = self._evaluate_notes_batched(
                job_id,
                document,
                note_set,
                judge_plan,
                knowledge_map,
                visual_context,
            )
            valid_keys = {note.stable_key for note in prevalidated_notes}
            failures_by_key = {
                str(failure.get("candidate")): failure for failure in deterministic_failures
            }
            evaluations.extend(
                NoteEvaluation(
                    stable_key=note.stable_key,
                    passed=False,
                    issues=[
                        "DETERMINISTIC_VALIDATION_FAILED: "
                        + "; ".join(
                            str(item)
                            for item in (
                                failures_by_key.get(note.stable_key, {}).get("detail") or []
                            )
                        )
                    ],
                    content_sha256=note_content_sha256(note),
                    evaluator_model="deterministic",
                    prompt_version=JUDGE_CONTRACT_VERSION,
                )
                for note in final_plan.notes
                if note.stable_key not in valid_keys
            )
            payload["evaluations"] = [item.model_dump() for item in evaluations]
            self._write_generation_artifact(artifact_path, payload)
        else:
            cached_evaluations = [
                NoteEvaluation.model_validate(item) for item in payload["evaluations"]
            ]
            cached_by_key = {item.stable_key: item for item in cached_evaluations}
            stale_notes = [
                note
                for note in final_plan.notes
                if note.stable_key not in cached_by_key
                or cached_by_key[note.stable_key].content_sha256 != note_content_sha256(note)
                or cached_by_key[note.stable_key].prompt_version != JUDGE_CONTRACT_VERSION
            ]
            if stale_notes:
                self._update_generation_stage(
                    job_id,
                    JobStage.EVALUATING_NOTES,
                    0.82,
                    generation_artifact=str(artifact_path),
                    generation_step="reevaluate_stale_hashes",
                    current_note_key=None,
                    stale_evaluation_count=len(stale_notes),
                )
                refreshed = self._evaluate_notes_batched(
                    job_id,
                    document,
                    note_set,
                    KnowledgePlan(
                        document_summary=final_plan.document_summary,
                        notes=stale_notes,
                        skipped_sections=final_plan.skipped_sections,
                    ),
                    knowledge_map,
                    visual_context,
                )
                cached_by_key.update({item.stable_key: item for item in refreshed})
            evaluations = [
                cached_by_key[note.stable_key]
                for note in final_plan.notes
                if note.stable_key in cached_by_key
            ]
            payload["evaluations"] = [item.model_dump() for item in evaluations]
            self._write_generation_artifact(artifact_path, payload)

        return GenerationBundle(
            profile=profile,
            knowledge_map=knowledge_map,
            source_note_outline=source_note_outline,
            note_set_plan=note_set,
            note_set_review=note_set_review,
            final_plan=final_plan,
            evaluations=evaluations,
        )

    def _generate_and_activate(self, job_id: str) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            state = _parse_json(job.state_json, {})
            normalized_path = Path(state["normalized_path"])
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
            job.stage = JobStage.GENERATING.value
            job.progress = 0.5
        self.telemetry.transition_stage(job_id, JobStage.GENERATING.value, 0.5)
        document = ParsedDocument.model_validate_json(normalized_path.read_text(encoding="utf-8"))
        resumable_methods = (
            "profile_document",
            "build_knowledge_map",
            "plan_source_note_bundle",
            "repair_note_set",
            "draft_note",
            "evaluate_plan",
        )
        if all(callable(getattr(self.agent, name, None)) for name in resumable_methods):
            generation = self._build_generation_resumable(job_id, document, normalized_path)
        else:
            build_generation = getattr(self.agent, "build_generation", None)
            generation = build_generation(document) if callable(build_generation) else None
        plan = generation.final_plan if generation is not None else self.agent.build_plan(document)
        if generation is not None and generation.knowledge_map.concepts and not plan.notes:
            raise RuntimeError(
                "The note-set planner returned no notes for a non-empty knowledge map"
            )
        map_payload = (
            generation.model_dump_json() if generation is not None else plan.model_dump_json()
        )
        with self.db.session() as session:
            existing_map = session.scalar(
                select(KnowledgeMapRecord).where(
                    KnowledgeMapRecord.source_version_id == source_version.id,
                    KnowledgeMapRecord.parse_run_id == job.parse_run_id,
                )
            )
            if existing_map is None:
                existing_map = KnowledgeMapRecord(
                    source_version_id=source_version.id,
                    parse_run_id=job.parse_run_id,
                    summary=plan.document_summary,
                    map_json=map_payload,
                )
                session.add(existing_map)
                session.flush()
            for note in plan.notes:
                candidate = session.scalar(
                    select(NoteCandidate).where(
                        NoteCandidate.knowledge_map_id == existing_map.id,
                        NoteCandidate.stable_key == note.stable_key,
                    )
                )
                if candidate is None:
                    session.add(
                        NoteCandidate(
                            knowledge_map_id=existing_map.id,
                            stable_key=note.stable_key,
                            payload_json=note.model_dump_json(),
                        )
                    )
            knowledge_map_id = existing_map.id
            job = session.get(Job, job_id)
            job.stage = JobStage.VALIDATING_NOTES.value
            job.progress = 0.94
        self.telemetry.transition_stage(job_id, JobStage.VALIDATING_NOTES.value, 0.94)

        visual_context = (
            dict(
                self._read_generation_artifact(normalized_path.parent / "generation-v4.json").get(
                    "visual_understandings"
                )
                or {}
            )
            if generation is not None
            else {}
        )
        passed, failures = self._validate_plan(
            document,
            plan,
            generation.note_set_plan if generation is not None else None,
            visual_context,
        )
        quality_failures = []
        if generation is not None:
            quality_failures = [
                {
                    "code": "CANDIDATE_QUALITY_FAILED",
                    "candidate": evaluation.stable_key,
                    "detail": evaluation.issues,
                }
                for evaluation in generation.evaluations
                if not evaluation.passed
            ]
            quality_failed_keys = {str(item["candidate"]) for item in quality_failures}
            passed = [note for note in passed if note.stable_key not in quality_failed_keys]
            existing_failure_keys = {str(item.get("candidate")) for item in failures}
            failures.extend(
                item
                for item in quality_failures
                if str(item["candidate"]) not in existing_failure_keys
            )
        repaired_count = 0
        repair_attempted_keys: set[str] = set()
        final_evaluations_by_key = {
            evaluation.stable_key: evaluation
            for evaluation in (generation.evaluations if generation is not None else [])
        }
        repair = getattr(self.agent, "repair_plan", None)
        if failures and callable(repair):
            original_failures = {str(item.get("candidate")): item for item in failures}
            repair_attempted_keys = set(original_failures)
            repaired_plan = self._invoke_agent_with_task_retries(
                job_id,
                JobStage.VALIDATING_NOTES,
                repair,
                document,
                plan,
                failures,
                **({"note_set": generation.note_set_plan} if generation is not None else {}),
                **({"vision_context": visual_context} if generation is not None else {}),
                note_key="targeted-repair",
            )
            repaired, remaining_failures = self._validate_plan(
                document,
                repaired_plan,
                generation.note_set_plan if generation is not None else None,
                visual_context,
            )
            evaluate = getattr(self.agent, "evaluate_plan", None)
            if generation is not None and repaired and callable(evaluate):
                repaired_evaluations = self._invoke_agent(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    evaluate,
                    document,
                    generation.note_set_plan,
                    KnowledgePlan(
                        document_summary=plan.document_summary,
                        notes=repaired,
                        skipped_sections=[],
                    ),
                    generation.knowledge_map,
                    visual_context,
                    note_key="targeted-repair-judge",
                )
                repaired_quality_failures = [
                    {
                        "code": "CANDIDATE_QUALITY_FAILED",
                        "candidate": evaluation.stable_key,
                        "detail": evaluation.issues,
                    }
                    for evaluation in repaired_evaluations
                    if not evaluation.passed
                ]
                final_evaluations_by_key.update(
                    {evaluation.stable_key: evaluation for evaluation in repaired_evaluations}
                )
                repaired_failed_keys = {
                    str(item["candidate"]) for item in repaired_quality_failures
                }
                repaired = [
                    note for note in repaired if note.stable_key not in repaired_failed_keys
                ]
                remaining_failures.extend(repaired_quality_failures)
            existing_keys = {note.stable_key for note in passed}
            repaired = [
                note
                for note in repaired
                if note.stable_key in repair_attempted_keys and note.stable_key not in existing_keys
            ]
            passed.extend(repaired)
            repaired_count = len(repaired)
            if generation is not None:
                specs_by_key = {spec.stable_key: spec for spec in generation.note_set_plan.notes}
                for repaired_note in repaired:
                    spec = specs_by_key.get(repaired_note.stable_key)
                    if spec is None:
                        continue
                    if spec.title != repaired_note.title:
                        spec.title = repaired_note.title
                        spec.question_answered = f"来源材料如何说明{repaired_note.title}？"
                    spec.target_thesis = repaired_note.thesis
            returned_keys = {note.stable_key for note in repaired_plan.notes}
            remaining_failures.extend(
                failure for key, failure in original_failures.items() if key not in returned_keys
            )
            failures = remaining_failures

        source_note_outline = (
            generation.source_note_outline
            if generation is not None
            else self._fallback_source_note_plan(source.title, plan)[0]
        )
        note_specs = (
            generation.note_set_plan
            if generation is not None
            else self._fallback_source_note_plan(source.title, plan)[1]
        )
        release_review = None
        release_problem_keys: set[str] = set()
        review_release = getattr(self.agent, "review_source_release", None)
        # Source-level LLM approval duplicated the per-note Judge and could turn
        # a local repair into a global rewrite. MVP publication is therefore
        # deterministic; whole-source review remains an explicit CLI action.
        requires_release_review = False
        if (
            generation is not None
            and passed
            and requires_release_review
            and callable(review_release)
        ):
            release_review = self._invoke_agent(
                job_id,
                JobStage.VALIDATING_NOTES,
                review_release,
                document,
                source_note_outline,
                note_specs,
                passed,
                note_key="source-release-review",
            )
            source_note_outline = apply_outline_title_patches(source_note_outline, release_review)
            release_problem_keys = {item.stable_key for item in release_review.note_issues}
            if release_problem_keys and callable(repair):
                release_failures = [
                    {
                        "code": "SOURCE_RELEASE_QUALITY_FAILED",
                        "candidate": item.stable_key,
                        "detail": item.issues,
                    }
                    for item in release_review.note_issues
                ]
                release_repair_plan = self._invoke_agent_with_task_retries(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    repair,
                    document,
                    KnowledgePlan(
                        document_summary=plan.document_summary,
                        notes=passed,
                    ),
                    release_failures,
                    note_set=note_specs,
                    vision_context=visual_context,
                    note_key="source-release-repair",
                )
                release_repaired, release_validation_failures = self._validate_plan(
                    document,
                    release_repair_plan,
                    note_specs,
                    visual_context,
                )
                release_evaluations = (
                    self._invoke_agent(
                        job_id,
                        JobStage.VALIDATING_NOTES,
                        self.agent.evaluate_plan,
                        document,
                        note_specs,
                        KnowledgePlan(
                            document_summary=plan.document_summary,
                            notes=release_repaired,
                        ),
                        generation.knowledge_map,
                        visual_context,
                        note_key="source-release-repair-judge",
                    )
                    if release_repaired
                    else []
                )
                final_evaluations_by_key.update(
                    {evaluation.stable_key: evaluation for evaluation in release_evaluations}
                )
                failed_release_keys = {
                    str(item.get("candidate")) for item in release_validation_failures
                } | {
                    evaluation.stable_key
                    for evaluation in release_evaluations
                    if not evaluation.passed
                }
                replacements = {
                    note.stable_key: note
                    for note in release_repaired
                    if note.stable_key in release_problem_keys
                    and note.stable_key not in failed_release_keys
                }
                release_issues_by_key = {
                    item.stable_key: item.issues for item in release_review.note_issues
                }
                specs_by_key = {spec.stable_key: spec for spec in note_specs.notes}
                for key, replacement in replacements.items():
                    spec = specs_by_key.get(key)
                    if spec is None:
                        continue
                    spec.title = replacement.title
                    spec.target_thesis = replacement.thesis
                    if any(
                        "TITLE_BODY_ALIGNMENT" in issue
                        for issue in release_issues_by_key.get(key, [])
                    ):
                        # The planner's original question can itself contain an
                        # unsupported relation (for example, asking how two
                        # merely adjacent strategies "combine"). Once a
                        # grounded release repair is accepted, keep retrieval
                        # metadata aligned with the corrected note instead of
                        # letting stale planning text fail every recheck.
                        spec.question_answered = f"来源材料如何说明{replacement.title}？"
                passed = [
                    replacements.get(note.stable_key, note)
                    for note in passed
                    if note.stable_key not in release_problem_keys
                    or note.stable_key in replacements
                ]
                repaired_count += len(replacements)
                unresolved_release_keys = release_problem_keys - set(replacements)
                failures.extend(release_validation_failures)
                failures.extend(
                    {
                        "code": "SOURCE_RELEASE_QUALITY_FAILED",
                        "candidate": key,
                        "detail": next(
                            item.issues
                            for item in release_review.note_issues
                            if item.stable_key == key
                        ),
                    }
                    for key in unresolved_release_keys
                )
                release_review = self._invoke_agent(
                    job_id,
                    JobStage.VALIDATING_NOTES,
                    review_release,
                    document,
                    source_note_outline,
                    note_specs,
                    passed,
                    note_key="source-release-recheck",
                )
                source_note_outline = apply_outline_title_patches(
                    source_note_outline, release_review
                )
                still_bad = {item.stable_key for item in release_review.note_issues}
                if still_bad:
                    passed = [note for note in passed if note.stable_key not in still_bad]
                    failures.extend(
                        {
                            "code": "SOURCE_RELEASE_QUALITY_FAILED",
                            "candidate": item.stable_key,
                            "detail": item.issues,
                        }
                        for item in release_review.note_issues
                    )
            if release_review.global_issues:
                failures.append(
                    {
                        "code": "SOURCE_RELEASE_GLOBAL_WARNING",
                        "candidate": "__source__",
                        "detail": release_review.global_issues,
                    }
                )

        repaired_or_released_keys = repair_attempted_keys | {
            note.stable_key for note in passed if note.stable_key in release_problem_keys
        }
        specs_by_key = {spec.stable_key: spec for spec in note_specs.notes}
        for note in passed:
            if note.stable_key not in repaired_or_released_keys:
                continue
            spec = specs_by_key.get(note.stable_key)
            if spec is None:
                continue
            if spec.title != note.title:
                spec.title = note.title
                spec.question_answered = f"来源材料如何说明{note.title}？"
            spec.target_thesis = note.thesis

        passed_keys = {note.stable_key for note in passed}
        released_note_set = note_specs.model_copy(
            update={"notes": [spec for spec in note_specs.notes if spec.stable_key in passed_keys]}
        )
        source_note_outline = self._prune_unbound_outline_leaves(
            source_note_outline, released_note_set
        )

        final_evaluations = list(final_evaluations_by_key.values())
        release_manifest = build_release_manifest(
            outline=source_note_outline,
            note_set=note_specs,
            notes=passed,
            evaluations=final_evaluations,
            failures=failures,
            review=release_review,
        )
        release_path = normalized_path.parent / "release-v1.json"
        self._write_generation_artifact(release_path, release_manifest)
        generation_path = normalized_path.parent / "generation-v4.json"
        generation_payload = self._read_generation_artifact(generation_path)
        generation_payload["note_set_plan"] = note_specs.model_dump()
        persisted_drafts = dict(generation_payload.get("drafts") or {})
        for note in passed:
            persisted_drafts[note.stable_key] = note.model_dump()
        generation_payload["drafts"] = persisted_drafts
        generation_payload["final_release"] = release_manifest
        generation_payload["evaluations"] = [item.model_dump() for item in final_evaluations]
        self._write_generation_artifact(generation_path, generation_payload)
        with self.db.session() as session:
            knowledge_map_record = session.get(KnowledgeMapRecord, knowledge_map_id)
            knowledge_map_record.summary = note_specs.document_summary

        self._record_candidate_results(
            knowledge_map_id,
            passed=passed,
            failures=failures,
            repair_attempted_keys=repair_attempted_keys,
        )
        self._record_quality_evaluations(
            knowledge_map_id,
            final_evaluations,
        )
        if not passed and plan.notes:
            raise RuntimeError("All planned notes failed local validation")
        activated = self._persist_notes(job_id, source.id, source_version.id, passed)
        source_note_id = self._persist_source_note(
            job_id=job_id,
            source_id=source.id,
            source_version_id=source_version.id,
            outline=source_note_outline,
            note_set=note_specs,
            passed_notes=passed,
        )
        with self.db.session() as session:
            job = session.get(Job, job_id)
            generation_warning_codes = {
                "CANDIDATE_VALIDATION_FAILED",
                "CANDIDATE_QUALITY_FAILED",
                "SOURCE_RELEASE_QUALITY_FAILED",
                "SOURCE_RELEASE_GLOBAL_WARNING",
                "SKIPPED_SECTION",
            }
            warnings = [
                item
                for item in _parse_json(job.warning_json, [])
                if item.get("code") not in generation_warning_codes
            ]
            warnings.extend(failures)
            warnings.extend(
                {
                    "code": "SKIPPED_SECTION",
                    "section": item.get("section"),
                    "detail": item.get("reason"),
                }
                for item in plan.skipped_sections
            )
            job.warning_json = _json(warnings)
            job.stage = JobStage.COMPLETED.value
            job.progress = 1.0
            job.next_attempt_at = None
            job.status = (
                JobStatus.COMPLETED_WITH_WARNINGS.value
                if _has_material_warnings(warnings)
                else JobStatus.COMPLETED.value
            )
            job.state_json = _json(
                {
                    **_parse_json(job.state_json, {}),
                    "activated_note_ids": activated,
                    "source_note_id": source_note_id,
                    "generation_schema": (
                        generation.schema_version if generation is not None else "legacy"
                    ),
                    "prompt_version": (
                        generation.prompt_version if generation is not None else "legacy"
                    ),
                    "planned_note_count": len(plan.notes),
                    "quality_failure_count": len(quality_failures),
                    "repaired_candidate_count": repaired_count,
                    "failed_candidate_count": len(failures),
                }
            )
        try:
            exports = self.export_markdown(source_version.id)
        except Exception as exc:  # noqa: BLE001
            with self.db.session() as session:
                job = session.get(Job, job_id)
                warnings = _parse_json(job.warning_json, [])
                warnings.append(
                    {
                        "code": "MARKDOWN_EXPORT_FAILED",
                        "severity": "WARNING",
                        "detail": str(exc),
                    }
                )
                job.warning_json = _json(warnings)
                job.status = JobStatus.COMPLETED_WITH_WARNINGS.value
        else:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                state = _parse_json(job.state_json, {})
                state["markdown_exports"] = exports
                job.state_json = _json(state)
        final_job = self.get_job(job_id)
        self.telemetry.finish_stage(
            job_id,
            status="COMPLETED",
            detail={"job_status": final_job["status"], "warning_count": len(final_job["warnings"])},
        )

    def _record_candidate_results(
        self,
        knowledge_map_id: str,
        *,
        passed: list,
        failures: list[dict[str, Any]],
        repair_attempted_keys: set[str],
    ) -> None:
        failure_by_key = {str(item.get("candidate")): item for item in failures}
        passed_keys = {note.stable_key for note in passed}
        with self.db.session() as session:
            candidates = session.scalars(
                select(NoteCandidate).where(NoteCandidate.knowledge_map_id == knowledge_map_id)
            ).all()
            for candidate in candidates:
                if candidate.stable_key in passed_keys:
                    candidate.validation_status = "PASSED"
                    candidate.failure_json = "[]"
                    candidate.payload_json = next(
                        note.model_dump_json()
                        for note in passed
                        if note.stable_key == candidate.stable_key
                    )
                elif candidate.stable_key in failure_by_key:
                    candidate.validation_status = "FAILED"
                    candidate.failure_json = _json(
                        failure_by_key[candidate.stable_key].get("detail", [])
                    )
                if candidate.stable_key in repair_attempted_keys:
                    candidate.repair_attempts = 1

    def _record_quality_evaluations(
        self,
        knowledge_map_id: str,
        evaluations: list[NoteEvaluation],
    ) -> None:
        with self.db.session() as session:
            for evaluation in evaluations:
                if not evaluation.content_sha256:
                    continue
                existing = session.scalar(
                    select(QualityEvaluationRecord).where(
                        QualityEvaluationRecord.knowledge_map_id == knowledge_map_id,
                        QualityEvaluationRecord.stable_key == evaluation.stable_key,
                        QualityEvaluationRecord.content_sha256 == evaluation.content_sha256,
                        QualityEvaluationRecord.evaluator_model
                        == (evaluation.evaluator_model or self.agent.name),
                        QualityEvaluationRecord.prompt_version
                        == (evaluation.prompt_version or "unknown"),
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    QualityEvaluationRecord(
                        knowledge_map_id=knowledge_map_id,
                        stable_key=evaluation.stable_key,
                        content_sha256=evaluation.content_sha256,
                        evaluator_model=evaluation.evaluator_model or self.agent.name,
                        prompt_version=evaluation.prompt_version or "unknown",
                        passed=evaluation.passed,
                        issues_json=_json(evaluation.issues),
                    )
                )

    @staticmethod
    def _deterministic_note_set_review(
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
    ) -> NoteSetReview:
        """Build complete concept coverage without a redundant approval call."""

        notes_by_concept: dict[str, list[str]] = {}
        for spec in note_set.notes:
            for concept_key in spec.concept_keys:
                notes_by_concept.setdefault(concept_key, []).append(spec.stable_key)
        skipped = {
            str(item.get("concept_key")): str(item.get("reason") or "")
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        coverage: list[ConceptCoverage] = []
        issues: list[PlanIssue] = []
        for concept in knowledge_map.concepts:
            note_keys = list(dict.fromkeys(notes_by_concept.get(concept.stable_key, [])))
            if note_keys:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="COVERED",
                        note_keys=note_keys,
                        reason="由规划结果确定性映射。",
                    )
                )
            elif concept.stable_key in skipped:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="SKIPPED",
                        reason=skipped[concept.stable_key],
                    )
                )
            else:
                coverage.append(
                    ConceptCoverage(
                        concept_key=concept.stable_key,
                        status="MISSING",
                        reason="规划未覆盖且未说明跳过原因。",
                    )
                )
                issues.append(
                    PlanIssue(
                        code="UNACCOUNTED_CONCEPT",
                        detail=f"知识概念 {concept.stable_key} 未被规划覆盖。",
                        concept_keys=[concept.stable_key],
                    )
                )
        return NoteSetReview(
            passed=not issues,
            issues=issues,
            coverage=coverage,
        )

    @staticmethod
    def _normalize_combined_plan_contracts(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        """Repair mechanical planner mistakes without a second global LLM call.

        Semantic grouping remains owned by the combined planner.  This pass only
        enforces facts already present in the planner inputs: valid block ids,
        complete concept accounting, and placement on an outline leaf.
        """

        repairs: list[dict[str, Any]] = []
        known_blocks = {block.block_id for block in document.blocks}
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        concept_order = {
            concept.stable_key: index for index, concept in enumerate(knowledge_map.concepts)
        }
        notes = [spec.model_copy(deep=True) for spec in note_set.notes]

        for spec in notes:
            original_concepts = list(spec.concept_keys)
            spec.concept_keys = list(
                dict.fromkeys(key for key in spec.concept_keys if key in concepts)
            )
            spec.evidence_block_ids = list(
                dict.fromkeys(key for key in spec.evidence_block_ids if key in known_blocks)
            )
            spec.asset_block_ids = list(
                dict.fromkeys(key for key in spec.asset_block_ids if key in known_blocks)
            )
            if spec.concept_keys != original_concepts:
                repairs.append(
                    {
                        "type": "DROP_UNKNOWN_CONCEPT",
                        "note_key": spec.stable_key,
                    }
                )

        covered = {key for spec in notes for key in spec.concept_keys}
        skipped = {
            str(item.get("concept_key"))
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        for missing_key in [key for key in concepts if key not in covered and key not in skipped]:
            missing = concepts[missing_key]

            def note_score(
                spec: NoteSpec,
                missing_concept: Any = missing,
                missing_concept_key: str = missing_key,
            ) -> tuple[int, int, int]:
                evidence_overlap = len(
                    set(spec.evidence_block_ids) & set(missing_concept.evidence_block_ids)
                ) + len(set(spec.asset_block_ids) & set(missing_concept.asset_block_ids))
                section_overlap = len(
                    {item.casefold() for item in spec.source_sections}
                    & {item.casefold() for item in missing_concept.source_sections}
                )
                positions = [concept_order[key] for key in spec.concept_keys]
                distance = min(
                    (abs(concept_order[missing_concept_key] - position) for position in positions),
                    default=len(concepts),
                )
                return (evidence_overlap, section_overlap, -distance)

            if not notes:
                break
            target = max(notes, key=note_score)
            target.concept_keys.append(missing_key)
            target.evidence_block_ids = list(
                dict.fromkeys([*target.evidence_block_ids, *missing.evidence_block_ids])
            )
            target.asset_block_ids = list(
                dict.fromkeys([*target.asset_block_ids, *missing.asset_block_ids])
            )
            target.source_sections = list(
                dict.fromkeys([*target.source_sections, *missing.source_sections])
            )
            repairs.append(
                {
                    "type": "MERGE_UNACCOUNTED_CONCEPT",
                    "concept_key": missing_key,
                    "note_key": target.stable_key,
                }
            )

        nodes = [node.model_copy(deep=True) for node in outline.nodes]
        node_by_key = {node.stable_key: node for node in nodes}
        parent_keys = {node.parent_key for node in nodes if node.parent_key}
        used_node_keys = set(node_by_key)
        for spec in notes:
            node = node_by_key.get(spec.outline_node_key)
            if node is None:
                leaves = [item for item in nodes if item.stable_key not in parent_keys]
                if leaves:
                    spec.outline_node_key = leaves[0].stable_key
                    repairs.append(
                        {
                            "type": "REBIND_UNKNOWN_OUTLINE_NODE",
                            "note_key": spec.stable_key,
                            "outline_key": spec.outline_node_key,
                        }
                    )
                continue
            if node.stable_key not in parent_keys:
                continue
            if node.level >= 3:
                continue
            base_key = f"{node.stable_key}--{spec.stable_key}"
            leaf_key = base_key
            suffix = 2
            while leaf_key in used_node_keys:
                leaf_key = f"{base_key}-{suffix}"
                suffix += 1
            leaf = SourceNoteOutlineNodeSpec(
                stable_key=leaf_key,
                title=spec.title,
                level=node.level + 1,
                parent_key=node.stable_key,
                learning_goal="",
                source_sections=spec.source_sections,
            )
            nodes.append(leaf)
            node_by_key[leaf_key] = leaf
            used_node_keys.add(leaf_key)
            spec.outline_node_key = leaf_key
            repairs.append(
                {
                    "type": "CREATE_NOTE_LEAF",
                    "note_key": spec.stable_key,
                    "outline_key": leaf_key,
                }
            )

        return (
            outline.model_copy(update={"nodes": nodes}),
            note_set.model_copy(update={"notes": notes}),
            repairs,
        )

    @staticmethod
    def _normalize_minimum_outline_depth(
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        """Make a model-produced flat outline satisfy the persisted hierarchy.

        The SourceNote title is already the document root.  When a model returns
        only topic nodes, wrap them in one structural root that the Markdown
        renderer deterministically elides.  This preserves the visible outline
        and note bindings while giving persistence a stable two-level tree.

        An empty outline can still be recovered when the same planning response
        contains NoteSpecs: each note becomes a level-two topic.  Semantic
        regrouping remains a planner concern; this pass only repairs structure.
        """

        nodes = [node.model_copy(deep=True) for node in outline.nodes]
        notes = [spec.model_copy(deep=True) for spec in note_set.notes]
        repairs: list[dict[str, Any]] = []
        if nodes and any(node.level >= 2 for node in nodes):
            return outline, note_set, repairs
        if nodes and any(node.level != 1 or node.parent_key for node in nodes):
            return outline, note_set, repairs
        if not nodes and not notes:
            return outline, note_set, repairs

        used_keys = {node.stable_key for node in nodes}
        root_key = "source-note-root"
        suffix = 2
        while root_key in used_keys:
            root_key = f"source-note-root-{suffix}"
            suffix += 1

        source_sections = list(
            dict.fromkeys(
                section for item in [*nodes, *notes] for section in item.source_sections if section
            )
        )
        normalized_title = outline.title.strip() or note_set.document_summary.strip() or "知识笔记"
        root = SourceNoteOutlineNodeSpec(
            stable_key=root_key,
            title=normalized_title,
            level=1,
            parent_key=None,
            learning_goal="",
            source_sections=source_sections,
        )

        if nodes:
            wrapped = [
                node.model_copy(update={"level": 2, "parent_key": root_key}) for node in nodes
            ]
            repairs.append(
                {
                    "type": "WRAP_SINGLE_LEVEL_OUTLINE",
                    "root_key": root_key,
                    "topic_count": len(wrapped),
                }
            )
        else:
            wrapped = []
            used_keys = {root_key}
            for spec in notes:
                base_key = spec.outline_node_key.strip() or f"topic--{spec.stable_key}"
                leaf_key = base_key
                suffix = 2
                while leaf_key in used_keys:
                    leaf_key = f"{base_key}-{suffix}"
                    suffix += 1
                used_keys.add(leaf_key)
                wrapped.append(
                    SourceNoteOutlineNodeSpec(
                        stable_key=leaf_key,
                        title=spec.title,
                        level=2,
                        parent_key=root_key,
                        learning_goal="",
                        source_sections=spec.source_sections,
                    )
                )
                spec.outline_node_key = leaf_key
            repairs.append(
                {
                    "type": "BUILD_OUTLINE_FROM_NOTE_SET",
                    "root_key": root_key,
                    "topic_count": len(wrapped),
                }
            )

        return (
            outline.model_copy(update={"title": normalized_title, "nodes": [root, *wrapped]}),
            note_set.model_copy(update={"notes": notes}),
            repairs,
        )

    @staticmethod
    def _prune_unbound_outline_leaves(
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> SourceNoteOutlinePlan:
        """Remove planner-created empty branches without another approval call."""

        nodes = list(outline.nodes)
        referenced = {spec.outline_node_key for spec in note_set.notes if spec.outline_node_key}
        while nodes:
            parent_keys = {node.parent_key for node in nodes if node.parent_key}
            removable = {
                node.stable_key
                for node in nodes
                if node.stable_key not in referenced and node.stable_key not in parent_keys
            }
            if not removable:
                break
            nodes = [node for node in nodes if node.stable_key not in removable]
        return outline.model_copy(update={"nodes": nodes})

    @staticmethod
    def _validate_source_note_outline(
        outline: SourceNoteOutlinePlan,
    ) -> list[str]:
        errors: list[str] = []
        nodes = {node.stable_key: node for node in outline.nodes}
        if not outline.title.strip() or not outline.nodes:
            errors.append("EMPTY_SOURCE_NOTE_OUTLINE")
            return errors
        if len(nodes) != len(outline.nodes):
            errors.append("DUPLICATE_OUTLINE_KEY")
        child_counts = {key: 0 for key in nodes}
        for node in outline.nodes:
            if node.level == 1:
                if node.parent_key is not None:
                    errors.append(f"ROOT_HAS_PARENT:{node.stable_key}")
                continue
            parent = nodes.get(node.parent_key or "")
            if parent is None:
                errors.append(f"UNKNOWN_OUTLINE_PARENT:{node.stable_key}")
                continue
            if parent.level != node.level - 1:
                errors.append(f"INVALID_OUTLINE_LEVEL:{node.stable_key}")
            child_counts[parent.stable_key] += 1
        if not any(node.level >= 2 for node in outline.nodes):
            errors.append("OUTLINE_REQUIRES_TWO_LEVELS")
        return errors

    @staticmethod
    def _validate_note_set(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        review: NoteSetReview,
        source_note_outline: SourceNoteOutlinePlan | None = None,
    ) -> list[str]:
        errors: list[str] = []
        known_blocks = {block.block_id for block in document.blocks}
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        seen_notes: set[str] = set()
        covered: set[str] = set()
        concept_use_counts: dict[str, int] = {}
        outline_nodes = {
            node.stable_key: node
            for node in (source_note_outline.nodes if source_note_outline else [])
        }
        non_leaf_outline_keys = {
            node.parent_key for node in outline_nodes.values() if node.parent_key is not None
        }
        for spec in note_set.notes:
            if spec.stable_key in seen_notes:
                errors.append(f"DUPLICATE_NOTE_KEY:{spec.stable_key}")
            seen_notes.add(spec.stable_key)
            if not spec.question_answered.strip() or not spec.concept_keys:
                errors.append(f"INCOMPLETE_NOTE_SPEC:{spec.stable_key}")
            if source_note_outline:
                if spec.outline_node_key not in outline_nodes:
                    errors.append(
                        f"UNKNOWN_NOTE_OUTLINE_NODE:{spec.stable_key}:{spec.outline_node_key}"
                    )
                elif spec.outline_node_key in non_leaf_outline_keys:
                    errors.append(
                        f"NOTE_NOT_ON_OUTLINE_LEAF:{spec.stable_key}:{spec.outline_node_key}"
                    )
            unknown_concepts = set(spec.concept_keys) - set(concepts)
            if unknown_concepts:
                errors.append(
                    f"UNKNOWN_CONCEPT:{spec.stable_key}:{','.join(sorted(unknown_concepts))}"
                )
            covered.update(set(spec.concept_keys) & set(concepts))
            for concept_key in set(spec.concept_keys) & set(concepts):
                concept_use_counts[concept_key] = concept_use_counts.get(concept_key, 0) + 1
            allowed_assets = {
                asset_id
                for concept_key in spec.concept_keys
                if concept_key in concepts
                for asset_id in concepts[concept_key].asset_block_ids
            }
            unbound_assets = set(spec.asset_block_ids) - allowed_assets
            if unbound_assets:
                errors.append(f"UNBOUND_ASSET:{spec.stable_key}:{','.join(sorted(unbound_assets))}")
            unknown_blocks = (
                set(spec.evidence_block_ids) | set(spec.asset_block_ids)
            ) - known_blocks
            if unknown_blocks:
                errors.append(f"UNKNOWN_BLOCK:{spec.stable_key}:{','.join(sorted(unknown_blocks))}")
        skipped = {
            str(item.get("concept_key"))
            for item in note_set.skipped_concepts
            if item.get("concept_key")
        }
        for key, concept in concepts.items():
            if concept.importance == "CORE" and key not in covered:
                errors.append(f"UNCOVERED_CORE_CONCEPT:{key}")
            if key not in covered and key not in skipped:
                errors.append(f"UNACCOUNTED_CONCEPT:{key}")
            if concept_use_counts.get(key, 0) > 1:
                errors.append(f"DUPLICATE_CANDIDATE_ASSIGNMENT:{key}")
        review_keys = {item.concept_key for item in review.coverage}
        if review_keys != set(concepts):
            missing = set(concepts) - review_keys
            unknown = review_keys - set(concepts)
            if missing:
                errors.append(f"INCOMPLETE_REVIEW_COVERAGE:{','.join(sorted(missing))}")
            if unknown:
                errors.append(f"UNKNOWN_REVIEW_CONCEPT:{','.join(sorted(unknown))}")
        return errors

    @staticmethod
    def _normalize_table_shapes(
        plan: KnowledgePlan,
    ) -> tuple[KnowledgePlan, list[dict[str, Any]]]:
        """Make rectangular, headerless matrices renderable without losing cells.

        Models correctly use TABLE for small matrices but may omit semantic
        headers because the source itself has none. Markdown still requires a
        header row. Empty presentation headers preserve every source value and
        avoid an unnecessary repair call or dropped note. Ragged tables remain
        invalid and are handled by the normal repair path.
        """

        normalized = plan.model_copy(deep=True)
        repairs: list[dict[str, Any]] = []
        for note in normalized.notes:
            for index, block in enumerate(note.blocks):
                if block.block_type.upper() != "TABLE" or block.table_headers:
                    continue
                widths = {len(row) for row in block.table_rows if row}
                if len(widths) != 1:
                    continue
                width = next(iter(widths), 0)
                if width <= 0:
                    continue
                block.table_headers = [""] * width
                repairs.append(
                    {
                        "note_key": note.stable_key,
                        "block_index": index,
                        "type": "ADD_EMPTY_PRESENTATION_HEADERS",
                        "column_count": width,
                    }
                )
        return normalized, repairs

    @staticmethod
    def _validate_plan(
        document: ParsedDocument,
        plan: KnowledgePlan,
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list, list[dict[str, Any]]]:
        known = {block.block_id for block in document.blocks}
        block_types = {block.block_id: block.block_type.upper() for block in document.blocks}
        renderable_assets = {
            block.block_id
            for block in document.blocks
            if block.asset_path or block.block_type.upper() in {"IMAGE", "FIGURE", "CHART"}
        }
        specs = {spec.stable_key: spec for spec in (note_set.notes if note_set else [])}
        passed = []
        failures = []
        seen_keys: set[str] = set()
        for raw_note in plan.notes:
            note = deduplicate_asset_placements(raw_note)
            reasons = []
            seen_asset_ids: set[str] = set()
            if not note.title.strip() or not note.thesis.strip() or not note.blocks:
                reasons.append("EMPTY_NOTE")
            if any(character in f"{note.title}\n{note.thesis}" for character in "\ufffc\ufffd\x00"):
                reasons.append("INVALID_TEXT_CHARACTER")
            if note.stable_key in seen_keys:
                reasons.append("DUPLICATE_STABLE_KEY")
            allowed_block_types = {
                "PARAGRAPH",
                "BULLETED_LIST",
                "NUMBERED_LIST",
                "TABLE",
                "FORMULA",
                "CODE",
                "EXAMPLE",
                "CALLOUT",
                "IMAGE",
            }
            for block in note.blocks:
                block_type = block.block_type.upper()
                block_text = block.plain_text()
                if block_type not in allowed_block_types:
                    reasons.append(f"UNKNOWN_BLOCK_TYPE:{block.block_type}")
                if not block_text and not block.asset_block_ids:
                    reasons.append("EMPTY_BLOCK")
                if any(character in block_text for character in "\ufffc\ufffd\x00"):
                    reasons.append("INVALID_TEXT_CHARACTER")
                if block_type == "TABLE":
                    width = len(block.table_headers)
                    if not width or any(len(row) != width for row in block.table_rows):
                        reasons.append("INVALID_TABLE_SHAPE")
                if block_type in {"BULLETED_LIST", "NUMBERED_LIST"} and not any(
                    item.strip() for item in block.items
                ):
                    reasons.append("EMPTY_LIST")
                if block_type == "IMAGE" and not block.asset_block_ids:
                    reasons.append("IMAGE_WITHOUT_ASSET")
                if block_type != "IMAGE" and "核心结论" in block_text:
                    reasons.append("VISIBLE_THESIS_LABEL")
                if not block.evidence_block_ids and not block.asset_block_ids:
                    reasons.append("MISSING_EVIDENCE")
                if unknown := set(block.evidence_block_ids) - known:
                    reasons.append(f"UNKNOWN_EVIDENCE:{','.join(sorted(unknown))}")
                if unknown_assets := set(block.asset_block_ids) - known:
                    reasons.append(f"UNKNOWN_ASSET:{','.join(sorted(unknown_assets))}")
                if non_visual_assets := {
                    block_id
                    for block_id in block.asset_block_ids
                    if block_id in block_types and block_id not in renderable_assets
                }:
                    reasons.append(f"NON_VISUAL_ASSET:{','.join(sorted(non_visual_assets))}")
                if duplicate_assets := set(block.asset_block_ids) & seen_asset_ids:
                    reasons.append(
                        f"DUPLICATE_ASSET_PLACEMENT:{','.join(sorted(duplicate_assets))}"
                    )
                seen_asset_ids.update(block.asset_block_ids)
            spec = specs.get(note.stable_key)
            if spec is not None:
                missing_assets = set(required_asset_ids(spec, vision_context)) - used_asset_ids(
                    note
                )
                if missing_assets:
                    reasons.append(f"MISSING_REQUIRED_ASSET:{','.join(sorted(missing_assets))}")
            if reasons:
                failures.append(
                    {
                        "code": "CANDIDATE_VALIDATION_FAILED",
                        "candidate": note.stable_key,
                        "detail": sorted(set(reasons)),
                    }
                )
            else:
                passed.append(note)
                seen_keys.add(note.stable_key)
        return passed, failures

    @staticmethod
    def _fallback_source_note_plan(
        source_title: str, plan: KnowledgePlan
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan]:
        root_key = "core-content"
        nodes = [
            SourceNoteOutlineNodeSpec(
                stable_key=root_key,
                title="核心内容",
                level=1,
                learning_goal="按来源主题系统掌握关键知识",
            )
        ]
        section_keys: dict[str, str] = {}
        specs: list[NoteSpec] = []
        for note in plan.notes:
            section = note.source_sections[0] if note.source_sections else "知识要点"
            if section not in section_keys:
                key = f"section-{_sha256_text(section)[:12]}"
                section_keys[section] = key
                nodes.append(
                    SourceNoteOutlineNodeSpec(
                        stable_key=key,
                        title=section,
                        level=2,
                        parent_key=root_key,
                        source_sections=[section],
                    )
                )
            evidence_ids = list(
                dict.fromkeys(
                    block_id for block in note.blocks for block_id in block.evidence_block_ids
                )
            )
            asset_ids = list(
                dict.fromkeys(
                    block_id for block in note.blocks for block_id in block.asset_block_ids
                )
            )
            specs.append(
                NoteSpec(
                    stable_key=note.stable_key,
                    title=note.title,
                    question_answered=note.title,
                    target_thesis=note.thesis,
                    outline_node_key=section_keys[section],
                    concept_keys=[],
                    evidence_block_ids=evidence_ids,
                    asset_block_ids=asset_ids,
                    tags=note.tags,
                    source_sections=note.source_sections,
                    rationale="由兼容生成路径按来源章节放置",
                )
            )
        return (
            SourceNoteOutlinePlan(
                title=source_title,
                summary=plan.document_summary,
                learning_goal="",
                nodes=nodes,
            ),
            NoteSetPlan(
                document_summary=plan.document_summary,
                notes=specs,
            ),
        )

    def _persist_source_note(
        self,
        *,
        job_id: str,
        source_id: str,
        source_version_id: str,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        passed_notes: list[NoteDraft],
    ) -> str:
        passed_keys = {note.stable_key for note in passed_notes}
        spec_by_key = {
            spec.stable_key: spec for spec in note_set.notes if spec.stable_key in passed_keys
        }
        original_nodes = {node.stable_key: node for node in outline.nodes}
        used_keys = {
            spec.outline_node_key
            for spec in spec_by_key.values()
            if spec.outline_node_key in original_nodes
        }
        retained_keys = set(used_keys)
        for key in list(used_keys):
            node = original_nodes[key]
            while node.parent_key and node.parent_key in original_nodes:
                retained_keys.add(node.parent_key)
                node = original_nodes[node.parent_key]
        retained_nodes = [node for node in outline.nodes if node.stable_key in retained_keys]
        if passed_notes and not retained_nodes:
            fallback_outline, fallback_set = self._fallback_source_note_plan(
                outline.title,
                KnowledgePlan(
                    document_summary=outline.summary,
                    notes=passed_notes,
                ),
            )
            outline = fallback_outline
            original_nodes = {node.stable_key: node for node in outline.nodes}
            retained_nodes = outline.nodes
            spec_by_key = {spec.stable_key: spec for spec in fallback_set.notes}

        with self.db.session() as session:
            job = session.get(Job, job_id)
            source_note = session.scalar(
                select(SourceNote).where(SourceNote.source_id == source_id)
            )
            if source_note is None:
                source_note = SourceNote(source_id=source_id)
                session.add(source_note)
                session.flush()

            note_rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.stable_key.in_(passed_keys),
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            note_by_key = {note.stable_key: (note, version) for note, version in note_rows}
            composition = [
                {
                    "outline_key": node.stable_key,
                    "parent_key": node.parent_key,
                    "level": node.level,
                    "title": node.title,
                    "note_versions": [
                        note_by_key[key][1].id
                        for key, spec in spec_by_key.items()
                        if spec.outline_node_key == node.stable_key and key in note_by_key
                    ],
                }
                for node in retained_nodes
            ]
            content_hash = _sha256_text(
                _json(
                    {
                        "title": outline.title,
                        "summary": outline.summary,
                        "composition": composition,
                    }
                )
            )
            active_version = (
                session.get(SourceNoteVersion, source_note.active_version_id)
                if source_note.active_version_id
                else None
            )
            if active_version and active_version.content_sha256 == content_hash:
                return source_note.id

            version_no = (
                session.scalar(
                    select(func.max(SourceNoteVersion.version_no)).where(
                        SourceNoteVersion.source_note_id == source_note.id
                    )
                )
                or 0
            ) + 1
            version = SourceNoteVersion(
                source_note_id=source_note.id,
                source_version_id=source_version_id,
                parse_run_id=job.parse_run_id,
                version_no=version_no,
                parent_version_id=source_note.active_version_id,
                title=outline.title,
                summary=outline.summary,
                learning_goal="",
                content_sha256=content_hash,
                created_by=self.agent.name,
            )
            session.add(version)
            session.flush()

            persisted_nodes: dict[str, SourceNoteOutlineNode] = {}
            for order_index, node in sorted(
                enumerate(retained_nodes), key=lambda item: (item[1].level, item[0])
            ):
                parent = persisted_nodes.get(node.parent_key or "")
                record = SourceNoteOutlineNode(
                    source_note_version_id=version.id,
                    stable_key=node.stable_key,
                    parent_id=parent.id if parent else None,
                    title=node.title,
                    level=node.level,
                    order_index=order_index,
                    learning_goal="",
                    source_sections_json=_json(node.source_sections),
                )
                session.add(record)
                session.flush()
                persisted_nodes[node.stable_key] = record

            order_by_outline: dict[str, int] = {}
            for spec in note_set.notes:
                if spec.stable_key not in note_by_key or spec.stable_key not in spec_by_key:
                    continue
                outline_record = persisted_nodes.get(spec_by_key[spec.stable_key].outline_node_key)
                if outline_record is None:
                    continue
                note, note_version = note_by_key[spec.stable_key]
                placement_order = order_by_outline.get(outline_record.stable_key, 0)
                session.add(
                    SourceNoteUnitPlacement(
                        source_note_version_id=version.id,
                        outline_node_id=outline_record.id,
                        note_id=note.id,
                        note_version_id=note_version.id,
                        order_index=placement_order,
                    )
                )
                order_by_outline[outline_record.stable_key] = placement_order + 1

            source = session.get(Source, source_id)
            if source is None or source.visibility != "ACTIVE" or job.status == "CANCELLED":
                raise ValueError("Source was deleted while generation was running")
            old_version_ids = session.scalars(select(NoteVersion.id).join(AtomicNote, AtomicNote.id == NoteVersion.note_id).where(
                AtomicNote.source_id == source_id, NoteVersion.source_version_id != source_version_id)).all()
            source.current_version_id = source_version_id
            for old_note in session.scalars(select(AtomicNote).where(AtomicNote.source_id == source_id,
                AtomicNote.source_version_id != source_version_id, AtomicNote.visibility == "ACTIVE")):
                old_note.visibility = "SUPERSEDED_SOURCE"
                delete_fts_for_note(session, old_note.id)
            source_note.active_version_id = version.id
            source_note.visibility = "ACTIVE"
            source_note.updated_at = utcnow()
            if self.settings.knowledge_auto_sync:
                event_id = self.knowledge_sync.enqueue(session, source_id, "SOURCE_PUBLISHED",
                    {"source_version_id": source_version_id, "source_note_version_id": version.id}, key=f"source-publish:{version.id}")
                self.knowledge_sync.invalidate(session, old_version_ids, event_id)
            session.add(
                AuditEvent(
                    actor=self.agent.name,
                    action="ACTIVATE_SOURCE_NOTE_VERSION",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=version.id,
                    detail_json=_json(
                        {
                            "outline_node_count": len(retained_nodes),
                            "unit_count": len(note_by_key),
                        }
                    ),
                )
            )
            return source_note.id

    @staticmethod
    def _reconcile_note_version_assets(
        session: Session,
        *,
        note_version: NoteVersion,
        parse_run_id: str,
        note: NoteDraft,
    ) -> int:
        blocks = session.scalars(
            select(NoteBlock)
            .where(NoteBlock.note_version_id == note_version.id)
            .order_by(NoteBlock.order_index)
        ).all()
        existing_assets = session.scalars(
            select(NoteAsset).where(NoteAsset.note_version_id == note_version.id)
        ).all()
        existing_visual_ids = {item.visual_asset_id for item in existing_assets}
        linked_blob_ids = {
            asset.blob_id
            for item in existing_assets
            if (asset := session.get(VisualAsset, item.visual_asset_id)) is not None
        }
        next_order = max((item.order_index for item in existing_assets), default=-1) + 1
        added = 0
        for block_order, draft_block in enumerate(note.blocks):
            if block_order >= len(blocks):
                break
            for external_block_id in dict.fromkeys(draft_block.asset_block_ids):
                visual_asset = session.scalar(
                    select(VisualAsset)
                    .join(DocumentBlock, DocumentBlock.id == VisualAsset.document_block_id)
                    .where(
                        VisualAsset.parse_run_id == parse_run_id,
                        DocumentBlock.external_block_id == external_block_id,
                    )
                )
                if (
                    visual_asset is None
                    or visual_asset.id in existing_visual_ids
                    or visual_asset.blob_id in linked_blob_ids
                ):
                    continue
                session.add(
                    NoteAsset(
                        note_version_id=note_version.id,
                        visual_asset_id=visual_asset.id,
                        note_block_id=blocks[block_order].id,
                        order_index=next_order,
                        placement="INLINE",
                        purpose=draft_block.purpose
                        or f"支持笔记“{note.title}”正文块 {block_order + 1} 的视觉解释或证据",
                        alt_text=draft_block.caption
                        or visual_asset.caption
                        or f"{note.title}的来源图片",
                    )
                )
                existing_visual_ids.add(visual_asset.id)
                linked_blob_ids.add(visual_asset.blob_id)
                next_order += 1
                added += 1
        return added

    def _persist_notes(
        self,
        job_id: str,
        source_id: str,
        source_version_id: str,
        notes: list,
        *,
        publish_release: bool = True,
    ) -> list[str]:
        activated: list[str] = []
        for note in notes:
            body = "\n\n".join(text for block in note.blocks if (text := block.plain_text()))
            content_hash = _sha256_text(
                _json(
                    {
                        "title": note.title,
                        "thesis": note.thesis,
                        "blocks": [
                            {
                                "type": block.block_type,
                                "content": block.content_payload(),
                                "evidence": block.evidence_block_ids,
                                "assets": block.asset_block_ids,
                            }
                            for block in note.blocks
                        ],
                        "tags": note.tags,
                    }
                )
            )
            with self.db.session() as session:
                job = session.get(Job, job_id)
                existing_version = session.scalar(
                    select(NoteVersion)
                    .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                    .where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.stable_key == note.stable_key,
                        NoteVersion.source_version_id == source_version_id,
                        NoteVersion.parse_run_id == job.parse_run_id,
                        NoteVersion.content_sha256 == content_hash,
                    )
                )
                if existing_version is not None:
                    atomic = session.get(AtomicNote, existing_version.note_id)
                    self._reconcile_note_version_assets(
                        session,
                        note_version=existing_version,
                        parse_run_id=job.parse_run_id,
                        note=note,
                    )
                    delete_fts_for_note(session, atomic.id)
                    insert_fts(
                        session,
                        note_version_id=existing_version.id,
                        note_id=atomic.id,
                        title=note.title,
                        thesis=note.thesis,
                        body=body,
                        tags=" ".join(note.tags),
                    )
                    atomic.active_version_id = existing_version.id
                    atomic.visibility = "ACTIVE"
                    atomic.updated_at = utcnow()
                    activated.append(atomic.id)
                    continue

                atomic = session.scalar(
                    select(AtomicNote).where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.stable_key == note.stable_key,
                    )
                )
                if atomic is None:
                    atomic = AtomicNote(
                        source_id=source_id,
                        source_version_id=source_version_id,
                        stable_key=note.stable_key,
                    )
                    session.add(atomic)
                    session.flush()
                version_no = (
                    session.scalar(
                        select(func.max(NoteVersion.version_no)).where(
                            NoteVersion.note_id == atomic.id
                        )
                    )
                    or 0
                ) + 1
                version = NoteVersion(
                    note_id=atomic.id,
                    source_version_id=source_version_id,
                    parse_run_id=job.parse_run_id,
                    version_no=version_no,
                    parent_version_id=atomic.active_version_id,
                    title=note.title,
                    thesis=note.thesis,
                    tags_json=_json(note.tags),
                    quality_status=QualityStatus.AUTO_VALIDATED.value,
                    review_status=ReviewStatus.UNREVIEWED.value,
                    content_sha256=content_hash,
                    created_by=self.agent.name,
                )
                session.add(version)
                session.flush()
                linked_asset_blob_ids: set[str] = set()
                asset_index = 0
                for order, draft_block in enumerate(note.blocks):
                    block_text = draft_block.plain_text()
                    block_payload = draft_block.content_payload()
                    block_hash_input = _json(
                        {"type": draft_block.block_type, "content": block_payload}
                    )
                    block = NoteBlock(
                        note_version_id=version.id,
                        block_key=f"b-{order}-{_sha256_text(block_hash_input)[:12]}",
                        block_type=draft_block.block_type,
                        order_index=order,
                        content_text=block_text,
                        content_json=_json(block_payload),
                        content_hash=_sha256_text(block_hash_input),
                        origin=BlockOrigin.AI_GENERATED.value,
                    )
                    session.add(block)
                    session.flush()
                    claim = None
                    if block_text:
                        claim = Claim(
                            note_version_id=version.id,
                            note_block_id=block.id,
                            order_index=order,
                            claim_text=block_text,
                            support_status="SUPPORTED",
                        )
                        session.add(claim)
                        session.flush()
                    for external_block_id in dict.fromkeys(draft_block.evidence_block_ids):
                        document_block = session.scalar(
                            select(DocumentBlock).where(
                                DocumentBlock.parse_run_id == job.parse_run_id,
                                DocumentBlock.external_block_id == external_block_id,
                            )
                        )
                        if document_block is None:
                            continue
                        span = EvidenceSpan(
                            parse_run_id=job.parse_run_id,
                            page_id=document_block.page_id,
                            document_block_id=document_block.id,
                            quoted_text=document_block.text_content,
                            normalized_text_hash=_sha256_text(
                                " ".join(document_block.text_content.split()).lower()
                            ),
                            evidence_level="MINERU_TEXT_MATCHED",
                            confidence=1.0,
                        )
                        session.add(span)
                        session.flush()
                        if claim is not None:
                            session.add(ClaimEvidence(claim_id=claim.id, span_id=span.id))
                    for external_block_id in dict.fromkeys(draft_block.asset_block_ids):
                        visual_asset = session.scalar(
                            select(VisualAsset)
                            .join(
                                DocumentBlock,
                                DocumentBlock.id == VisualAsset.document_block_id,
                            )
                            .where(
                                VisualAsset.parse_run_id == job.parse_run_id,
                                DocumentBlock.external_block_id == external_block_id,
                            )
                        )
                        if visual_asset is not None:
                            if visual_asset.blob_id in linked_asset_blob_ids:
                                continue
                            exists = session.scalar(
                                select(NoteAsset).where(
                                    NoteAsset.note_version_id == version.id,
                                    NoteAsset.visual_asset_id == visual_asset.id,
                                )
                            )
                            if exists is None:
                                session.add(
                                    NoteAsset(
                                        note_version_id=version.id,
                                        visual_asset_id=visual_asset.id,
                                        note_block_id=block.id,
                                        order_index=asset_index,
                                        placement="INLINE",
                                        purpose=draft_block.purpose
                                        or f"支持笔记“{note.title}”正文块 {order + 1} 的视觉解释或证据",
                                        alt_text=draft_block.caption
                                        or visual_asset.caption
                                        or f"{note.title}的来源图片",
                                    )
                                )
                                linked_asset_blob_ids.add(visual_asset.blob_id)
                                asset_index += 1

                vector = self.embedding.embed([f"{note.title}\n{note.thesis}\n{body}"])[0]
                session.add(
                    NoteEmbedding(
                        note_version_id=version.id,
                        model_name=self.embedding.model_name,
                        dimension=len(vector),
                        vector_json=_json(vector),
                        content_hash=content_hash,
                    )
                )
                delete_fts_for_note(session, atomic.id)
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=atomic.id,
                    title=note.title,
                    thesis=note.thesis,
                    body=body,
                    tags=" ".join(note.tags),
                )
                atomic.active_version_id = version.id
                atomic.visibility = "ACTIVE"
                atomic.updated_at = utcnow()
                activated.append(atomic.id)
        if activated and publish_release:
            with self.db.session() as session:
                source = session.get(Source, source_id)
                if source.visibility != "ACTIVE":
                    raise ValueError("Cannot publish notes for a deleted source")
                source.updated_at = utcnow()
                previous_notes = session.scalars(
                    select(AtomicNote).where(
                        AtomicNote.source_id == source_id,
                        AtomicNote.source_version_id != source_version_id,
                        AtomicNote.visibility == "ACTIVE",
                    )
                ).all()
                obsolete_current_notes = session.scalars(
                    select(AtomicNote).where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.id.not_in(activated),
                        AtomicNote.visibility == "ACTIVE",
                    )
                ).all()
                hidden_notes = [*previous_notes, *obsolete_current_notes]
                for previous in hidden_notes:
                    previous.visibility = "HIDDEN"
                    previous.updated_at = utcnow()
                    delete_fts_for_note(session, previous.id)
                session.add(
                    AuditEvent(
                        actor=self.agent.name,
                        action="ACTIVATE_SOURCE_VERSION",
                        risk_level="L1",
                        target_type="SourceVersion",
                        target_id=source_version_id,
                        detail_json=_json(
                            {
                                "activated_note_count": len(activated),
                                "hidden_previous_note_count": len(previous_notes),
                                "hidden_obsolete_note_count": len(obsolete_current_notes),
                            }
                        ),
                    )
                )
        return activated

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return
            job.status = JobStatus.FAILED.value
            job.error_code = exc.__class__.__name__
            job.error_detail = str(exc)
            job.attempt_count += 1
            if job.parse_run_id:
                parse_run = session.get(ParseRun, job.parse_run_id)
                if parse_run and parse_run.status != "SUCCEEDED":
                    parse_run.status = "FAILED"
                    parse_run.error_code = exc.__class__.__name__
                    parse_run.error_detail = str(exc)
        self.telemetry.finish_stage(
            job_id,
            status="FAILED",
            detail={"error_code": exc.__class__.__name__, "error_detail": str(exc)[:2_000]},
        )

    def backfill_source_notes(self) -> dict[str, int]:
        with self.db.session() as session:
            source_rows = session.execute(
                select(Source, SourceVersion, Job)
                .join(SourceVersion, SourceVersion.id == Source.current_version_id)
                .join(Job, Job.source_version_id == SourceVersion.id)
                .where(
                    Job.status.in_(
                        [
                            JobStatus.COMPLETED.value,
                            JobStatus.COMPLETED_WITH_WARNINGS.value,
                        ]
                    ),
                    Job.parse_run_id.is_not(None),
                )
                .order_by(Source.id, Job.updated_at.desc())
            ).all()
            selected: dict[str, tuple[str, str, str]] = {}
            for source, source_version, job in source_rows:
                selected.setdefault(
                    source.id,
                    (source.title, source_version.id, job.id),
                )

        created = 0
        skipped = 0
        for source_id, (source_title, source_version_id, job_id) in selected.items():
            with self.db.session() as session:
                existing = session.scalar(
                    select(SourceNote.id).where(SourceNote.source_id == source_id)
                )
                note_ids = list(
                    session.scalars(
                        select(AtomicNote.id).where(
                            AtomicNote.source_version_id == source_version_id,
                            AtomicNote.visibility == "ACTIVE",
                            AtomicNote.active_version_id.is_not(None),
                        )
                    ).all()
                )
            if existing or not note_ids:
                skipped += 1
                continue
            drafts: list[NoteDraft] = []
            for note_id in note_ids:
                note = self.get_note(note_id)
                drafts.append(
                    NoteDraft(
                        stable_key=next(
                            item["stable_key"]
                            for item in self._active_note_identities(source_version_id)
                            if item["note_id"] == note_id
                        ),
                        title=note["title"],
                        thesis=note["thesis"],
                        blocks=[
                            NoteBlockDraft(
                                block_type=block["type"],
                                text=block["text"],
                                evidence_block_ids=[],
                            )
                            for block in note["blocks"]
                        ],
                        tags=note["tags"],
                        source_sections=["知识要点"],
                    )
                )
            plan = KnowledgePlan(
                document_summary=f"{source_title} 的完整来源笔记。",
                notes=drafts,
            )
            outline, note_set = self._fallback_source_note_plan(source_title, plan)
            self._persist_source_note(
                job_id=job_id,
                source_id=source_id,
                source_version_id=source_version_id,
                outline=outline,
                note_set=note_set,
                passed_notes=drafts,
            )
            created += 1
        return {"created": created, "skipped": skipped}

    def _active_note_identities(self, source_version_id: str) -> list[dict[str, str]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                    AtomicNote.active_version_id.is_not(None),
                )
            ).all()
            return [{"note_id": note.id, "stable_key": note.stable_key} for note in rows]

    @staticmethod
    def _attachment_payload(attachment: AgentAttachment, blob: BlobObject) -> dict[str, Any]:
        return {
            "attachment_id": attachment.id,
            "conversation_id": attachment.conversation_id,
            "message_id": attachment.message_id,
            "original_name": attachment.original_name,
            "attachment_type": attachment.attachment_type,
            "status": attachment.status,
            "media_type": blob.media_type,
            "byte_size": blob.byte_size,
            "storage_key": blob.storage_key,
            "blob_url": f"/internal/v1/blobs/{blob.storage_key}",
            "vision": _parse_json(attachment.vision_json, {}),
        }

    def create_agent_attachment(
        self,
        image_path: Path,
        *,
        original_name: str | None = None,
        media_type: str | None = None,
        conversation_id: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        image_path = image_path.resolve()
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        resolved_media_type = media_type or mimetypes.guess_type(image_path.name)[0] or ""
        if resolved_media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Agent attachments currently accept PNG, JPEG, or WebP images")
        if image_path.stat().st_size > 15 * 1024 * 1024:
            raise ValueError("Agent image attachment exceeds the 15 MB limit")
        storage_key, sha256, byte_size = self.blobs.put(image_path, resolved_media_type)
        with self.db.session() as session:
            blob = session.scalar(
                select(BlobObject).where(
                    BlobObject.content_sha256 == sha256,
                    BlobObject.media_type == resolved_media_type,
                )
            )
            if blob is None:
                blob = BlobObject(
                    storage_key=storage_key,
                    content_sha256=sha256,
                    media_type=resolved_media_type,
                    byte_size=byte_size,
                )
                session.add(blob)
                session.flush()
            attachment = AgentAttachment(
                conversation_id=conversation_id,
                blob_id=blob.id,
                original_name=Path(original_name or image_path.name).name,
                attachment_type="IMAGE",
                status="ANALYZING" if analyze and self.vision.enabled else "READY",
            )
            session.add(attachment)
            session.flush()
            attachment_id = attachment.id

        if analyze and self.vision.enabled:
            try:
                understanding = self.vision.analyze(
                    image_path,
                    external_block_id=f"agent-attachment-{attachment_id}",
                    context={
                        "origin": "USER_UPLOAD",
                        "instruction": "概括图片内容、教学作用，并判断是否适合作为完整笔记插图。",
                    },
                )
                vision_payload = understanding.model_dump()
                attachment_status = "READY"
            except Exception as exc:  # noqa: BLE001
                vision_payload = {
                    "status": "FAILED",
                    "error_code": exc.__class__.__name__,
                    "error_detail": str(exc)[:1_000],
                }
                attachment_status = "READY_WITH_WARNING"
            with self.db.session() as session:
                attachment = session.get(AgentAttachment, attachment_id)
                attachment.vision_json = _json(vision_payload)
                attachment.status = attachment_status
        return self.get_agent_attachments([attachment_id])[0]

    def get_agent_attachments(self, attachment_ids: list[str]) -> list[dict[str, Any]]:
        ordered_ids = list(dict.fromkeys(attachment_ids))
        if not ordered_ids:
            return []
        with self.db.session() as session:
            rows = session.execute(
                select(AgentAttachment, BlobObject)
                .join(BlobObject, BlobObject.id == AgentAttachment.blob_id)
                .where(AgentAttachment.id.in_(ordered_ids))
            ).all()
            by_id = {
                attachment.id: self._attachment_payload(attachment, blob)
                for attachment, blob in rows
            }
        missing = [attachment_id for attachment_id in ordered_ids if attachment_id not in by_id]
        if missing:
            raise ValueError(f"Unknown agent attachment: {missing[0]}")
        return [by_id[attachment_id] for attachment_id in ordered_ids]

    def claim_agent_attachments(
        self,
        attachment_ids: list[str],
        *,
        conversation_id: str,
        message_id: str,
    ) -> None:
        if not attachment_ids:
            return
        with self.db.session() as session:
            attachments = session.scalars(
                select(AgentAttachment).where(AgentAttachment.id.in_(attachment_ids))
            ).all()
            by_id = {attachment.id: attachment for attachment in attachments}
            for attachment_id in dict.fromkeys(attachment_ids):
                attachment = by_id.get(attachment_id)
                if attachment is None:
                    raise ValueError(f"Unknown agent attachment: {attachment_id}")
                if attachment.conversation_id not in {None, conversation_id}:
                    raise ValueError("Attachment belongs to another conversation")
                if attachment.status not in {"READY", "READY_WITH_WARNING", "CONSUMED"}:
                    raise ValueError(f"Attachment is not ready: {attachment.status}")
                attachment.conversation_id = conversation_id
                attachment.message_id = message_id

    @staticmethod
    def _numeric_page_label(value: str) -> int | None:
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None

    @staticmethod
    def _select_edit_target(
        affected: list[tuple[AtomicNote, NoteVersion]],
        *,
        target_note_title: str | None,
        vision_text: str,
    ) -> tuple[AtomicNote, NoteVersion]:
        if target_note_title:
            expected = target_note_title.casefold().strip()
            exact = [item for item in affected if item[1].title.casefold().strip() == expected]
            if exact:
                return exact[0]
            partial = [
                item
                for item in affected
                if expected in item[1].title.casefold() or item[1].title.casefold() in expected
            ]
            if len(partial) == 1:
                return partial[0]
            raise ValueError(f"Target atomic note is ambiguous: {target_note_title}")

        def score(item: tuple[AtomicNote, NoteVersion]) -> tuple[int, int, str]:
            title = item[1].title
            semantic = sum(
                weight
                for token, weight in (("示例", 8), ("计算", 5), ("案例", 4), ("图", 1))
                if token in title and (not vision_text or token in vision_text)
            )
            overlap = sum(
                1 for character in set(title) if character.strip() and character in vision_text
            )
            return semantic, overlap, title

        return max(affected, key=score)

    def _clone_note_for_image_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        removed_visual_ids: set[str],
        replacement_visual: VisualAsset,
        add_replacement: bool,
        attachment: AgentAttachment,
        caption: str,
        purpose: str,
        actor: str,
    ) -> NoteVersion:
        old_blocks = list(
            session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == parent.id)
                .order_by(NoteBlock.order_index)
            ).all()
        )
        old_assets = list(
            session.scalars(
                select(NoteAsset)
                .where(NoteAsset.note_version_id == parent.id)
                .order_by(NoteAsset.order_index)
            ).all()
        )
        assets_by_block: dict[str, list[NoteAsset]] = {}
        for link in old_assets:
            if link.note_block_id:
                assets_by_block.setdefault(link.note_block_id, []).append(link)
        dropped_block_ids = {
            block.id
            for block in old_blocks
            if block.block_type == "IMAGE"
            and (
                (assets_by_block.get(block.id) and all(
                    link.visual_asset_id in removed_visual_ids for link in assets_by_block[block.id]
                ))
                or (add_replacement and not assets_by_block.get(block.id))
            )
        }
        removed_orders = [
            block.order_index for block in old_blocks if block.id in dropped_block_ids
        ]
        replacement_order = min(removed_orders, default=len(old_blocks))
        kept_blocks = [block for block in old_blocks if block.id not in dropped_block_ids]
        remaining_assets = [
            link for link in old_assets if link.visual_asset_id not in removed_visual_ids
        ]
        signature = {
            "parent": parent.content_sha256,
            "blocks": [
                {
                    "key": block.block_key,
                    "type": block.block_type,
                    "text": block.content_text,
                    "json": _parse_json(block.content_json, {}),
                }
                for block in kept_blocks
            ],
            "assets": sorted(link.visual_asset_id for link in remaining_assets),
            "replacement": replacement_visual.id if add_replacement else None,
            "attachment": attachment.id,
        }
        content_hash = _sha256_text(_json(signature))
        version_no = (
            session.scalar(
                select(func.max(NoteVersion.version_no)).where(NoteVersion.note_id == atomic.id)
            )
            or 0
        ) + 1
        version = NoteVersion(
            note_id=atomic.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            thesis=parent.thesis,
            tags_json=parent.tags_json,
            quality_status=QualityStatus.AUTO_VALIDATED.value,
            review_status=ReviewStatus.UNREVIEWED.value,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()

        sequence: list[tuple[int, NoteBlock | None]] = [
            (block.order_index * 2, block) for block in kept_blocks
        ]
        if add_replacement:
            sequence.append((replacement_order * 2, None))
        sequence.sort(key=lambda item: (item[0], item[1] is not None))
        block_map: dict[str, NoteBlock] = {}
        replacement_block: NoteBlock | None = None
        for order_index, (_sort_key, old_block) in enumerate(sequence):
            if old_block is None:
                replacement_block = NoteBlock(
                    note_version_id=version.id,
                    block_key=f"b-user-image-{attachment.id[:12]}",
                    previous_block_id=next(
                        (
                            block.id
                            for block in old_blocks
                            if block.order_index == replacement_order
                        ),
                        None,
                    ),
                    block_type="IMAGE",
                    order_index=order_index,
                    content_text="",
                    content_json=_json({"caption": caption, "purpose": purpose}),
                    content_hash=_sha256_text(
                        _json({"type": "IMAGE", "visual_asset_id": replacement_visual.id})
                    ),
                    origin=BlockOrigin.HUMAN_REVISED.value,
                )
                session.add(replacement_block)
                session.flush()
                continue
            cloned = NoteBlock(
                note_version_id=version.id,
                block_key=old_block.block_key,
                previous_block_id=old_block.id,
                block_type=old_block.block_type,
                order_index=order_index,
                content_text=old_block.content_text,
                content_json=old_block.content_json,
                content_hash=old_block.content_hash,
                origin=old_block.origin,
            )
            session.add(cloned)
            session.flush()
            block_map[old_block.id] = cloned
        for old_block in kept_blocks:
            if old_block.parent_block_id and old_block.parent_block_id in block_map:
                block_map[old_block.id].parent_block_id = block_map[old_block.parent_block_id].id

        old_claims = list(
            session.scalars(
                select(Claim).where(Claim.note_version_id == parent.id).order_by(Claim.order_index)
            ).all()
        )
        for old_claim in old_claims:
            cloned_block = block_map.get(old_claim.note_block_id)
            if cloned_block is None:
                continue
            new_claim = Claim(
                note_version_id=version.id,
                note_block_id=cloned_block.id,
                order_index=old_claim.order_index,
                claim_text=old_claim.claim_text,
                claim_type=old_claim.claim_type,
                support_status=old_claim.support_status,
            )
            session.add(new_claim)
            session.flush()
            span_ids = session.scalars(
                select(ClaimEvidence.span_id).where(ClaimEvidence.claim_id == old_claim.id)
            ).all()
            for span_id in span_ids:
                session.add(ClaimEvidence(claim_id=new_claim.id, span_id=span_id))

        for order_index, old_link in enumerate(remaining_assets):
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=old_link.visual_asset_id,
                    note_block_id=(
                        block_map[old_link.note_block_id].id
                        if old_link.note_block_id in block_map
                        else None
                    ),
                    order_index=order_index,
                    placement=old_link.placement,
                    purpose=old_link.purpose,
                    alt_text=old_link.alt_text,
                )
            )
        if add_replacement and replacement_block is not None:
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=replacement_visual.id,
                    note_block_id=replacement_block.id,
                    order_index=len(remaining_assets),
                    placement="INLINE",
                    purpose=purpose,
                    alt_text=caption,
                )
            )

        old_embeddings = session.scalars(
            select(NoteEmbedding).where(NoteEmbedding.note_version_id == parent.id)
        ).all()
        for embedding in old_embeddings:
            session.add(
                NoteEmbedding(
                    note_version_id=version.id,
                    model_name=embedding.model_name,
                    dimension=embedding.dimension,
                    vector_json=embedding.vector_json,
                    content_hash=content_hash,
                    ready=embedding.ready,
                )
            )
        body = "\n\n".join(block.content_text for block in kept_blocks if block.content_text)
        delete_fts_for_note(session, atomic.id)
        insert_fts(
            session,
            note_version_id=version.id,
            note_id=atomic.id,
            title=version.title,
            thesis=version.thesis,
            body=body,
            tags=" ".join(_parse_json(version.tags_json, [])),
        )
        atomic.active_version_id = version.id
        atomic.updated_at = utcnow()
        return version

    def _clone_note_for_block_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        block_key: str,
        new_text: str,
        actor: str,
    ) -> NoteVersion:
        """Create an immutable note version with one human-revised text block.

        Claims on untouched blocks retain their evidence.  The edited block is
        deliberately detached from its previous machine claim because the
        original quote can no longer prove user-authored wording.
        """
        old_blocks = list(
            session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == parent.id)
                .order_by(NoteBlock.order_index)
            ).all()
        )
        target = next((block for block in old_blocks if block.block_key == block_key), None)
        if target is None:
            raise ValueError(f"Unknown note block: {block_key}")
        if target.block_type == "IMAGE":
            raise ValueError("Image blocks must be changed with the image replacement operation")
        if target.content_text == new_text:
            return parent

        signature = {
            "parent": parent.content_sha256,
            "edit": {"block_key": block_key, "text": new_text},
        }
        content_hash = _sha256_text(_json(signature))
        version_no = (
            session.scalar(
                select(func.max(NoteVersion.version_no)).where(NoteVersion.note_id == atomic.id)
            )
            or 0
        ) + 1
        version = NoteVersion(
            note_id=atomic.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            thesis=parent.thesis,
            tags_json=parent.tags_json,
            quality_status=QualityStatus.AUTO_VALIDATED.value,
            review_status=ReviewStatus.UNREVIEWED.value,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()

        block_map: dict[str, NoteBlock] = {}
        for old_block in old_blocks:
            edited = old_block.id == target.id
            text_content = new_text if edited else old_block.content_text
            cloned = NoteBlock(
                note_version_id=version.id,
                block_key=old_block.block_key,
                previous_block_id=old_block.id,
                block_type=old_block.block_type,
                order_index=old_block.order_index,
                content_text=text_content,
                content_json=old_block.content_json,
                content_hash=_sha256_text(
                    _json(
                        {
                            "type": old_block.block_type,
                            "text": text_content,
                            "json": _parse_json(old_block.content_json, {}),
                        }
                    )
                ),
                origin=(BlockOrigin.HUMAN_REVISED.value if edited else old_block.origin),
            )
            session.add(cloned)
            session.flush()
            block_map[old_block.id] = cloned
        for old_block in old_blocks:
            if old_block.parent_block_id and old_block.parent_block_id in block_map:
                block_map[old_block.id].parent_block_id = block_map[old_block.parent_block_id].id

        old_claims = list(
            session.scalars(
                select(Claim).where(Claim.note_version_id == parent.id).order_by(Claim.order_index)
            ).all()
        )
        for old_claim in old_claims:
            if old_claim.note_block_id == target.id:
                continue
            cloned_block = block_map.get(old_claim.note_block_id)
            if cloned_block is None:
                continue
            new_claim = Claim(
                note_version_id=version.id,
                note_block_id=cloned_block.id,
                order_index=old_claim.order_index,
                claim_text=old_claim.claim_text,
                claim_type=old_claim.claim_type,
                support_status=old_claim.support_status,
            )
            session.add(new_claim)
            session.flush()
            span_ids = session.scalars(
                select(ClaimEvidence.span_id).where(ClaimEvidence.claim_id == old_claim.id)
            ).all()
            for span_id in span_ids:
                session.add(ClaimEvidence(claim_id=new_claim.id, span_id=span_id))

        old_assets = list(
            session.scalars(
                select(NoteAsset)
                .where(NoteAsset.note_version_id == parent.id)
                .order_by(NoteAsset.order_index)
            ).all()
        )
        for old_link in old_assets:
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=old_link.visual_asset_id,
                    note_block_id=(
                        block_map[old_link.note_block_id].id
                        if old_link.note_block_id in block_map
                        else None
                    ),
                    order_index=old_link.order_index,
                    placement=old_link.placement,
                    purpose=old_link.purpose,
                    alt_text=old_link.alt_text,
                )
            )

        body = "\n\n".join(
            new_text if block.id == target.id else block.content_text
            for block in old_blocks
            if (new_text if block.id == target.id else block.content_text)
        )
        vector = self.embedding.embed([f"{version.title}\n{version.thesis}\n{body}"])[0]
        session.add(
            NoteEmbedding(
                note_version_id=version.id,
                model_name=self.embedding.model_name,
                dimension=len(vector),
                vector_json=_json(vector),
                content_hash=content_hash,
            )
        )
        delete_fts_for_note(session, atomic.id)
        insert_fts(
            session,
            note_version_id=version.id,
            note_id=atomic.id,
            title=version.title,
            thesis=version.thesis,
            body=body,
            tags=" ".join(_parse_json(version.tags_json, [])),
        )
        atomic.active_version_id = version.id
        atomic.updated_at = utcnow()
        return version

    def edit_note_block(
        self,
        note_id: str,
        *,
        expected_version_id: str,
        block_key: str,
        new_text: str,
        actor: str = "user-via-harness",
    ) -> dict[str, Any]:
        """Patch one block using optimistic locking and publish a new release."""
        normalized_text = new_text.strip()
        if not normalized_text:
            raise ValueError("Edited block text must not be blank")
        with self.db.session() as session:
            atomic = session.get(AtomicNote, note_id)
            if atomic is None or atomic.active_version_id is None:
                raise ValueError(f"Unknown active note: {note_id}")
            if atomic.active_version_id != expected_version_id:
                raise ValueError(
                    "Note version conflict: refresh the note before editing "
                    f"(expected {expected_version_id}, active {atomic.active_version_id})"
                )
            parent = session.get(NoteVersion, atomic.active_version_id)
            if parent is None:
                raise ValueError(f"Unknown note version: {atomic.active_version_id}")
            source_note_version = session.scalar(
                select(SourceNoteVersion)
                .join(SourceNote, SourceNote.active_version_id == SourceNoteVersion.id)
                .join(
                    SourceNoteUnitPlacement,
                    SourceNoteUnitPlacement.source_note_version_id == SourceNoteVersion.id,
                )
                .where(
                    SourceNoteUnitPlacement.note_id == atomic.id,
                    SourceNoteUnitPlacement.note_version_id == parent.id,
                )
            )
            if source_note_version is None:
                raise ValueError("Atomic note is not part of an active SourceNote release")
            source_note = session.get(SourceNote, source_note_version.source_note_id)
            if source_note is None:
                raise ValueError("SourceNote release owner is unavailable")

            version = self._clone_note_for_block_edit(
                session,
                atomic=atomic,
                parent=parent,
                block_key=block_key,
                new_text=normalized_text,
                actor=actor,
            )
            if version.id == parent.id:
                return {
                    "status": "UNCHANGED",
                    "note_id": atomic.id,
                    "previous_version_id": parent.id,
                    "version_id": parent.id,
                    "version_no": parent.version_no,
                    "source_note_id": source_note.id,
                    "source_note_version_id": source_note_version.id,
                    "block_key": block_key,
                    "origin": "UNCHANGED",
                }
            new_source_version = self._clone_source_note_release(
                session,
                source_note=source_note,
                parent=source_note_version,
                actor=actor,
                edit_signature={
                    "operation": "EDIT_NOTE_BLOCK",
                    "note_id": atomic.id,
                    "block_key": block_key,
                    "note_version_id": version.id,
                },
            )
            if self.settings.knowledge_auto_sync:
                event_id = self.knowledge_sync.enqueue(session, atomic.source_id, "NOTE_UPDATED",
                    {"note_id": atomic.id, "previous_version_id": parent.id, "version_id": version.id}, key=f"note-edit:{version.id}")
                self.knowledge_sync.invalidate(session, [parent.id], event_id)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="EDIT_NOTE_BLOCK",
                    risk_level="L1",
                    target_type="NoteVersion",
                    target_id=version.id,
                    detail_json=_json(
                        {
                            "note_id": atomic.id,
                            "block_key": block_key,
                            "parent_version_id": parent.id,
                            "source_note_version_id": new_source_version.id,
                        }
                    ),
                )
            )
            return {
                "status": "UPDATED",
                "note_id": atomic.id,
                "previous_version_id": parent.id,
                "version_id": version.id,
                "version_no": version.version_no,
                "source_note_id": source_note.id,
                "source_note_version_id": new_source_version.id,
                "block_key": block_key,
                "origin": BlockOrigin.HUMAN_REVISED.value,
            }

    @staticmethod
    def _clone_source_note_release(
        session: Session,
        *,
        source_note: SourceNote,
        parent: SourceNoteVersion,
        actor: str,
        edit_signature: dict[str, Any],
    ) -> SourceNoteVersion:
        old_nodes = list(
            session.scalars(
                select(SourceNoteOutlineNode)
                .where(SourceNoteOutlineNode.source_note_version_id == parent.id)
                .order_by(SourceNoteOutlineNode.order_index)
            ).all()
        )
        old_placements = list(
            session.scalars(
                select(SourceNoteUnitPlacement)
                .where(SourceNoteUnitPlacement.source_note_version_id == parent.id)
                .order_by(
                    SourceNoteUnitPlacement.outline_node_id,
                    SourceNoteUnitPlacement.order_index,
                )
            ).all()
        )
        active_versions: dict[str, str] = {}
        for placement in old_placements:
            atomic = session.get(AtomicNote, placement.note_id)
            if atomic is None or atomic.active_version_id is None:
                raise ValueError(f"Atomic note is not active: {placement.note_id}")
            active_versions[placement.note_id] = atomic.active_version_id
        content_hash = _sha256_text(
            _json(
                {
                    "parent": parent.content_sha256,
                    "placements": active_versions,
                    "edit": edit_signature,
                }
            )
        )
        version_no = (
            session.scalar(
                select(func.max(SourceNoteVersion.version_no)).where(
                    SourceNoteVersion.source_note_id == source_note.id
                )
            )
            or 0
        ) + 1
        version = SourceNoteVersion(
            source_note_id=source_note.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            summary=parent.summary,
            learning_goal=parent.learning_goal,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()
        node_map: dict[str, SourceNoteOutlineNode] = {}
        for old_node in sorted(old_nodes, key=lambda item: (item.level, item.order_index)):
            cloned = SourceNoteOutlineNode(
                source_note_version_id=version.id,
                stable_key=old_node.stable_key,
                parent_id=(
                    node_map[old_node.parent_id].id if old_node.parent_id in node_map else None
                ),
                title=old_node.title,
                level=old_node.level,
                order_index=old_node.order_index,
                learning_goal=old_node.learning_goal,
                source_sections_json=old_node.source_sections_json,
            )
            session.add(cloned)
            session.flush()
            node_map[old_node.id] = cloned
        for old_placement in old_placements:
            session.add(
                SourceNoteUnitPlacement(
                    source_note_version_id=version.id,
                    outline_node_id=node_map[old_placement.outline_node_id].id,
                    note_id=old_placement.note_id,
                    note_version_id=active_versions[old_placement.note_id],
                    order_index=old_placement.order_index,
                )
            )
        source_note.active_version_id = version.id
        source_note.updated_at = utcnow()
        return version

    def replace_source_note_images(
        self,
        *,
        attachment_id: str,
        page_start: int,
        page_end: int,
        source_note_id: str | None = None,
        source_ref: str | None = None,
        target_note_title: str | None = None,
        caption: str | None = None,
        purpose: str | None = None,
        expected_source_note_version_id: str | None = None,
        actor: str = "user-via-agent",
    ) -> dict[str, Any]:
        if page_start < 1 or page_end < page_start:
            raise ValueError("Invalid replacement page range")
        with self.db.session() as session:
            if source_note_id:
                source_note = session.get(SourceNote, source_note_id)
                if source_note is None or source_note.active_version_id is None:
                    raise ValueError(f"Unknown active source note: {source_note_id}")
                source_note_version = session.get(SourceNoteVersion, source_note.active_version_id)
                source_version = session.get(SourceVersion, source_note_version.source_version_id)
            else:
                expected = (source_ref or "").strip().casefold()
                if not expected:
                    raise ValueError("source_note_id or source_ref is required")
                candidates = session.execute(
                    select(SourceNote, SourceNoteVersion, SourceVersion, Source)
                    .join(SourceNoteVersion, SourceNoteVersion.id == SourceNote.active_version_id)
                    .join(SourceVersion, SourceVersion.id == SourceNoteVersion.source_version_id)
                    .join(Source, Source.id == SourceNote.source_id)
                    .where(SourceNote.visibility == "ACTIVE")
                ).all()
                exact = [
                    row
                    for row in candidates
                    if row[2].original_name.casefold() == expected
                    or row[3].title.casefold() == expected
                ]
                partial = [
                    row
                    for row in candidates
                    if expected in row[2].original_name.casefold()
                    or expected in row[3].title.casefold()
                    or row[2].original_name.casefold() in expected
                ]
                matches = exact or partial
                if len(matches) != 1:
                    raise ValueError(f"Source reference is unknown or ambiguous: {source_ref}")
                source_note, source_note_version, source_version, _source = matches[0]

            if (
                expected_source_note_version_id
                and source_note_version.id != expected_source_note_version_id
            ):
                raise ValueError(
                    "SourceNote version conflict: refresh before replacing images "
                    f"(expected {expected_source_note_version_id}, "
                    f"active {source_note_version.id})"
                )

            attachment = session.get(AgentAttachment, attachment_id)
            if attachment is None:
                raise ValueError(f"Unknown agent attachment: {attachment_id}")
            attachment_blob = session.get(BlobObject, attachment.blob_id)
            if attachment_blob is None or not attachment_blob.media_type.startswith("image/"):
                raise ValueError("Replacement attachment is not an image")
            parse_run_id = source_note_version.parse_run_id
            synthetic_external_id = f"user-attachment-{attachment.id}"
            synthetic_block = session.scalar(
                select(DocumentBlock).where(
                    DocumentBlock.parse_run_id == parse_run_id,
                    DocumentBlock.external_block_id == synthetic_external_id,
                )
            )
            replacement_visual = (
                session.scalar(
                    select(VisualAsset).where(VisualAsset.document_block_id == synthetic_block.id)
                )
                if synthetic_block is not None
                else None
            )
            if replacement_visual is not None:
                active_link = session.scalar(
                    select(NoteAsset)
                    .join(NoteVersion, NoteVersion.id == NoteAsset.note_version_id)
                    .join(AtomicNote, AtomicNote.active_version_id == NoteVersion.id)
                    .where(NoteAsset.visual_asset_id == replacement_visual.id)
                )
                if active_link is not None:
                    return {
                        "status": "ALREADY_APPLIED",
                        "source_note_id": source_note.id,
                        "source_note_version_id": source_note.active_version_id,
                        "source_title": source_note_version.title,
                        "source_file": source_version.original_name,
                        "page_start": page_start,
                        "page_end": page_end,
                        "attachment_id": attachment.id,
                        "visual_asset_id": replacement_visual.id,
                    }

            linked_rows = session.execute(
                select(AtomicNote, NoteVersion, NoteAsset, VisualAsset, SourcePage)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .join(NoteAsset, NoteAsset.note_version_id == NoteVersion.id)
                .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                .join(SourcePage, SourcePage.id == VisualAsset.page_id)
                .where(
                    AtomicNote.source_version_id == source_version.id,
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            selected_rows = [
                row
                for row in linked_rows
                if (page := self._numeric_page_label(row[4].page_label)) is not None
                and page_start <= page <= page_end
                and (
                    row[3].extraction_method != "USER_UPLOAD"
                    or (target_note_title and row[1].title.casefold().strip() == target_note_title.casefold().strip())
                )
            ]
            if not selected_rows:
                raise ValueError(f"No active note images found for pages {page_start}-{page_end}")
            affected_by_note: dict[str, tuple[AtomicNote, NoteVersion]] = {}
            removed_visual_ids: set[str] = set()
            for atomic, version, _link, visual, _page in selected_rows:
                affected_by_note.setdefault(atomic.id, (atomic, version))
                removed_visual_ids.add(visual.id)
            affected = list(affected_by_note.values())
            vision = _parse_json(attachment.vision_json, {})
            vision_text = " ".join(
                str(vision.get(key) or "") for key in ("caption", "teaching_role", "asset_type")
            )
            target_atomic, target_parent = self._select_edit_target(
                affected,
                target_note_title=target_note_title,
                vision_text=vision_text,
            )
            resolved_caption = (
                (caption or "").strip()
                or str(vision.get("caption") or "").strip()
                or f"第 {page_start}–{page_end} 页的合并教学图示"
            )
            resolved_purpose = (
                (purpose or "").strip()
                or str(vision.get("teaching_role") or "").strip()
                or f"替代第 {page_start}–{page_end} 页的 MinerU 图片碎片"
            )
            pages = list(
                session.scalars(
                    select(SourcePage)
                    .where(SourcePage.parse_run_id == parse_run_id)
                    .order_by(SourcePage.page_index)
                ).all()
            )
            anchor_page = next(
                (page for page in pages if self._numeric_page_label(page.page_label) == page_start),
                next((page for page in pages if page.page_index == page_start - 1), None),
            )
            if anchor_page is None:
                raise ValueError(f"Source page {page_start} does not exist")
            if synthetic_block is None:
                reading_order = (
                    session.scalar(
                        select(func.max(DocumentBlock.reading_order)).where(
                            DocumentBlock.parse_run_id == parse_run_id
                        )
                    )
                    or 0
                ) + 1
                synthetic_block = DocumentBlock(
                    parse_run_id=parse_run_id,
                    page_id=anchor_page.id,
                    external_block_id=synthetic_external_id,
                    block_type="IMAGE",
                    reading_order=reading_order,
                    text_content=resolved_caption,
                    metadata_json=_json(
                        {
                            "origin": "USER_UPLOAD",
                            "attachment_id": attachment.id,
                            "replaces_pages": [
                                str(page) for page in range(page_start, page_end + 1)
                            ],
                        }
                    ),
                )
                session.add(synthetic_block)
                session.flush()
            if replacement_visual is None:
                replacement_visual = VisualAsset(
                    parse_run_id=parse_run_id,
                    page_id=anchor_page.id,
                    document_block_id=synthetic_block.id,
                    blob_id=attachment_blob.id,
                    asset_type="FIGURE",
                    caption=resolved_caption,
                    extraction_method="USER_UPLOAD",
                    quality_status="USABLE",
                )
                session.add(replacement_visual)
                session.flush()

            updated_notes: list[dict[str, Any]] = []
            for atomic, parent_version in affected:
                new_version = self._clone_note_for_image_edit(
                    session,
                    atomic=atomic,
                    parent=parent_version,
                    removed_visual_ids=removed_visual_ids,
                    replacement_visual=replacement_visual,
                    add_replacement=atomic.id == target_atomic.id,
                    attachment=attachment,
                    caption=resolved_caption,
                    purpose=resolved_purpose,
                    actor=actor,
                )
                updated_notes.append(
                    {
                        "note_id": atomic.id,
                        "title": new_version.title,
                        "previous_version_id": parent_version.id,
                        "version_id": new_version.id,
                        "version_no": new_version.version_no,
                        "contains_replacement": atomic.id == target_atomic.id,
                    }
                )
            new_source_version = self._clone_source_note_release(
                session,
                source_note=source_note,
                parent=source_note_version,
                actor=actor,
                edit_signature={
                    "operation": "REPLACE_SOURCE_IMAGES",
                    "attachment_id": attachment.id,
                    "pages": [page_start, page_end],
                    "removed_visual_ids": sorted(removed_visual_ids),
                    "replacement_visual_id": replacement_visual.id,
                },
            )
            attachment.status = "CONSUMED"
            attachment.consumed_at = utcnow()
            session.add(
                AuditEvent(
                    actor=actor,
                    action="AGENT_REPLACE_SOURCE_IMAGES",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=new_source_version.id,
                    detail_json=_json(
                        {
                            "parent_source_note_version_id": source_note_version.id,
                            "attachment_id": attachment.id,
                            "page_start": page_start,
                            "page_end": page_end,
                            "removed_visual_count": len(removed_visual_ids),
                            "target_note_id": target_atomic.id,
                            "updated_notes": updated_notes,
                        }
                    ),
                )
            )
            return {
                "status": "UPDATED",
                "source_note_id": source_note.id,
                "source_note_version_id": new_source_version.id,
                "source_note_version_no": new_source_version.version_no,
                "source_title": new_source_version.title,
                "source_file": source_version.original_name,
                "page_start": page_start,
                "page_end": page_end,
                "removed_visual_count": len(removed_visual_ids),
                "attachment_id": attachment.id,
                "visual_asset_id": replacement_visual.id,
                "target_note_id": target_atomic.id,
                "target_note_title": target_parent.title,
                "updated_notes": updated_notes,
            }

    def list_source_notes(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(
                select(SourceNote, SourceNoteVersion, Source)
                .join(SourceNoteVersion, SourceNoteVersion.id == SourceNote.active_version_id)
                .join(Source, Source.id == SourceNote.source_id)
                .where(SourceNote.visibility == "ACTIVE")
                .order_by(SourceNoteVersion.created_at.desc())
            ).all()
            return [
                {
                    "source_note_id": source_note.id,
                    "version_id": version.id,
                    "source_id": source.id,
                    "source_version_id": version.source_version_id,
                    "title": version.title,
                    "summary": version.summary,
                    "learning_goal": version.learning_goal,
                    "version_no": version.version_no,
                }
                for source_note, version, source in rows
            ]

    def get_source_note(self, source_note_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            source_note = session.get(SourceNote, source_note_id)
            if source_note is None or source_note.visibility != "ACTIVE" or source_note.active_version_id is None:
                raise ValueError(f"Unknown active source note: {source_note_id}")
            version = session.get(SourceNoteVersion, source_note.active_version_id)
            source = session.get(Source, source_note.source_id)
            source_version = session.get(SourceVersion, version.source_version_id)
            nodes = session.scalars(
                select(SourceNoteOutlineNode)
                .where(SourceNoteOutlineNode.source_note_version_id == version.id)
                .order_by(SourceNoteOutlineNode.order_index)
            ).all()
            placements = session.scalars(
                select(SourceNoteUnitPlacement)
                .where(SourceNoteUnitPlacement.source_note_version_id == version.id)
                .order_by(
                    SourceNoteUnitPlacement.outline_node_id,
                    SourceNoteUnitPlacement.order_index,
                )
            ).all()
            placement_data = [
                (
                    item.outline_node_id,
                    item.note_id,
                    item.note_version_id,
                    item.order_index,
                )
                for item in placements
            ]
            payload = {
                "source_note_id": source_note.id,
                "version_id": version.id,
                "source_id": source.id,
                "source_version_id": source_version.id,
                "source_file": source_version.original_name,
                "source_media_type": source_version.media_type,
                "source_kind": "PDF" if source_version.media_type == "application/pdf" else "TEXTBOOK",
                "original_view_available": source_version.media_type == "application/pdf",
                "title": version.title,
                "summary": version.summary,
                "learning_goal": version.learning_goal,
                "version_no": version.version_no,
            }

        by_id: dict[str, dict[str, Any]] = {
            node.id: {
                "outline_node_id": node.id,
                "stable_key": node.stable_key,
                "title": node.title,
                "level": node.level,
                "learning_goal": node.learning_goal,
                "source_sections": _parse_json(node.source_sections_json, []),
                "children": [],
                "notes": [],
                "_parent_id": node.parent_id,
                "_order": node.order_index,
            }
            for node in nodes
        }
        navigation_anchors: list[dict[str, Any]] = []
        navigation_keys: set[tuple[str, str, str, int, str]] = set()

        def add_navigation_anchor(
            *,
            outline_node_id: str,
            note_id: str,
            block_key: str | None,
            page_index: int | None,
            page_label: str | None,
            anchor_kind: str,
            evidence_role: str = "PRIMARY",
        ) -> None:
            if page_index is None or page_index < 0:
                return
            key = (
                outline_node_id,
                note_id,
                block_key or "",
                page_index,
                anchor_kind,
            )
            if key in navigation_keys:
                return
            navigation_keys.add(key)
            navigation_anchors.append(
                {
                    "outline_node_id": outline_node_id,
                    "note_id": note_id,
                    "block_key": block_key,
                    "page_index": page_index,
                    "page_label": str(page_label or page_index + 1),
                    "anchor_kind": anchor_kind,
                    "evidence_role": evidence_role,
                    "confidence": "EXACT" if block_key else "NOTE",
                }
            )

        for outline_node_id, note_id, note_version_id, order_index in placement_data:
            if outline_node_id in by_id:
                note = self.get_note(note_id, version_id=note_version_id)
                note["placement_order"] = order_index
                by_id[outline_node_id]["notes"].append(note)
                for block in note.get("blocks", []):
                    block_key = str(block.get("block_key") or "") or None
                    for evidence in block.get("evidence", []):
                        add_navigation_anchor(
                            outline_node_id=outline_node_id,
                            note_id=note_id,
                            block_key=block_key,
                            page_index=evidence.get("page_index"),
                            page_label=evidence.get("page_label"),
                            anchor_kind="EVIDENCE",
                            evidence_role=str(evidence.get("role") or "PRIMARY"),
                        )
                    for asset in block.get("assets", []):
                        add_navigation_anchor(
                            outline_node_id=outline_node_id,
                            note_id=note_id,
                            block_key=block_key,
                            page_index=asset.get("page_index"),
                            page_label=asset.get("page_label"),
                            anchor_kind="ASSET",
                        )
                for evidence in note.get("evidence", []):
                    if evidence.get("block_key"):
                        continue
                    add_navigation_anchor(
                        outline_node_id=outline_node_id,
                        note_id=note_id,
                        block_key=None,
                        page_index=evidence.get("page_index"),
                        page_label=evidence.get("page_label"),
                        anchor_kind="EVIDENCE",
                        evidence_role=str(evidence.get("role") or "PRIMARY"),
                    )
                for asset in note.get("assets", []):
                    add_navigation_anchor(
                        outline_node_id=outline_node_id,
                        note_id=note_id,
                        block_key=None,
                        page_index=asset.get("page_index"),
                        page_label=asset.get("page_label"),
                        anchor_kind="ASSET",
                    )
        roots: list[dict[str, Any]] = []
        for node in sorted(by_id.values(), key=lambda item: item["_order"]):
            parent_id = node.pop("_parent_id")
            node.pop("_order")
            node["notes"].sort(key=lambda item: item["placement_order"])
            if parent_id and parent_id in by_id:
                by_id[parent_id]["children"].append(node)
            else:
                roots.append(node)
        payload["outline"] = roots
        payload["unit_count"] = len(placement_data)
        payload["navigation_anchors"] = sorted(
            navigation_anchors,
            key=lambda item: (
                item["page_index"],
                item["outline_node_id"],
                item["note_id"],
                item.get("block_key") or "",
                0 if item["anchor_kind"] == "EVIDENCE" else 1,
            ),
        )
        return payload

    def list_notes(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(AtomicNote.visibility == "ACTIVE")
                .order_by(NoteVersion.created_at.desc())
            ).all()
            return [
                {
                    "note_id": note.id,
                    "version_id": version.id,
                    "title": version.title,
                    "thesis": version.thesis,
                    "quality": version.quality_status,
                    "review": version.review_status,
                    "version_no": version.version_no,
                }
                for note, version in rows
            ]

    def get_note(self, note_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        with self.db.session() as session:
            note = session.get(AtomicNote, note_id)
            if note is None or note.active_version_id is None or (not version_id and note.visibility != "ACTIVE"):
                raise ValueError(f"Unknown active note: {note_id}")
            resolved_version_id = version_id or note.active_version_id
            version = session.get(NoteVersion, resolved_version_id)
            if version is None or version.note_id != note.id:
                raise ValueError(f"Unknown note version: {resolved_version_id}")
            blocks = session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == version.id)
                .order_by(NoteBlock.order_index)
            ).all()
            evidence = self._evidence_for_version(session, version.id)
            assets = self._assets_for_version(session, version.id)
            block_keys = {block.id: block.block_key for block in blocks}
            evidence = [
                {
                    **item,
                    "block_key": block_keys.get(str(item.get("note_block_id") or "")),
                }
                for item in evidence
            ]
            evidence_by_block: dict[str, list[dict[str, Any]]] = {}
            for item in evidence:
                block_id = str(item.get("note_block_id") or "")
                if block_id:
                    evidence_by_block.setdefault(block_id, []).append(item)
            assets_by_block: dict[str, list[dict[str, Any]]] = {}
            for asset in assets:
                anchor = str(asset.get("note_block_id") or "")
                if anchor:
                    assets_by_block.setdefault(anchor, []).append(asset)
            return {
                "note_id": note.id,
                "version_id": version.id,
                "title": version.title,
                "thesis": version.thesis,
                "tags": _parse_json(version.tags_json, []),
                "quality": version.quality_status,
                "review": version.review_status,
                "blocks": [
                    {
                        **_parse_json(block.content_json, {}),
                        "block_key": block.block_key,
                        "type": block.block_type,
                        "text": block.content_text,
                        "origin": block.origin,
                        "evidence": evidence_by_block.get(block.id, []),
                        "assets": assets_by_block.get(block.id, []),
                    }
                    for block in blocks
                ],
                "evidence": evidence,
                "assets": [item for item in assets if not item.get("note_block_id")],
            }

    def reconcile_source_note_release(self, source_note_id: str) -> dict[str, int]:
        """Restore the active atomic-note set from deterministic placements.

        SourceNoteVersion placements are the release manifest.  This operation
        is intentionally separate from note persistence so a targeted block or
        asset repair cannot accidentally publish a two-note partial release.
        """
        with self.db.session() as session:
            source_note = session.get(SourceNote, source_note_id)
            if source_note is None or source_note.active_version_id is None:
                raise ValueError(f"Unknown or inactive source_note_id: {source_note_id}")
            source_version = session.get(SourceNoteVersion, source_note.active_version_id)
            placements = session.scalars(
                select(SourceNoteUnitPlacement).where(
                    SourceNoteUnitPlacement.source_note_version_id == source_version.id
                )
            ).all()
            release_note_ids = {item.note_id for item in placements}
            restored = 0
            for placement in placements:
                note = session.get(AtomicNote, placement.note_id)
                version = session.get(NoteVersion, placement.note_version_id)
                if note is None or version is None:
                    continue
                if note.visibility != "ACTIVE" or note.active_version_id != version.id:
                    restored += 1
                note.visibility = "ACTIVE"
                note.active_version_id = version.id
                note.updated_at = utcnow()
                body = "\n\n".join(
                    session.scalars(
                        select(NoteBlock.content_text)
                        .where(NoteBlock.note_version_id == version.id)
                        .order_by(NoteBlock.order_index)
                    ).all()
                )
                delete_fts_for_note(session, note.id)
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=note.id,
                    title=version.title,
                    thesis=version.thesis,
                    body=body,
                    tags=" ".join(_parse_json(version.tags_json, [])),
                )
            hidden = 0
            other_notes = session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_version_id == source_version.source_version_id,
                    AtomicNote.id.not_in(release_note_ids),
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            for note in other_notes:
                note.visibility = "HIDDEN"
                note.updated_at = utcnow()
                delete_fts_for_note(session, note.id)
                hidden += 1
            session.add(
                AuditEvent(
                    actor="reconcile",
                    action="RECONCILE_SOURCE_NOTE_RELEASE",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=source_version.id,
                    detail_json=_json(
                        {
                            "release_note_count": len(release_note_ids),
                            "restored_note_count": restored,
                            "hidden_non_release_count": hidden,
                        }
                    ),
                )
            )
            return {
                "release_note_count": len(release_note_ids),
                "restored_note_count": restored,
                "hidden_non_release_count": hidden,
            }

    def export_markdown(self, source_version_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.session() as session:
            query = (
                select(SourceVersion.id)
                .join(AtomicNote, AtomicNote.source_version_id == SourceVersion.id)
                .where(
                    AtomicNote.visibility == "ACTIVE",
                    AtomicNote.active_version_id.is_not(None),
                )
                .distinct()
            )
            if source_version_id:
                query = query.where(SourceVersion.id == source_version_id)
            version_ids = list(session.scalars(query).all())
        if source_version_id and not version_ids:
            raise ValueError(f"No active notes for source_version_id: {source_version_id}")
        return [self._export_source_markdown(version_id) for version_id in version_ids]

    def _export_source_markdown(self, source_version_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            source_version = session.get(SourceVersion, source_version_id)
            if source_version is None:
                raise ValueError(f"Unknown source_version_id: {source_version_id}")
            source = session.get(Source, source_version.source_id)
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                )
                .order_by(NoteVersion.title)
            ).all()
            note_ids = [note.id for note, _version in rows]
            release_version_id = session.scalar(
                select(SourceNoteVersion.id)
                .join(SourceNote, SourceNote.id == SourceNoteVersion.source_note_id)
                .where(
                    SourceNote.source_id == source.id,
                    SourceNote.active_version_id == SourceNoteVersion.id,
                )
            )

        directory_name = (
            f"{_safe_filename(source.title, fallback='source')}--{source_version_id[:8]}"
            f"--{(release_version_id or 'atomic')[:8]}"
        )
        destination = self.settings.export_dir / directory_name
        assets_dir = destination / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        index_links: list[str] = []
        exported_files: list[str] = []

        def render_asset(asset: dict[str, Any], fallback_title: str) -> list[str]:
            source_asset = self.blobs.path_for(asset["storage_key"])
            asset_name = Path(asset["storage_key"]).name
            target_asset = assets_dir / asset_name
            if not target_asset.exists():
                shutil.copy2(source_asset, target_asset)
            alt = str(asset.get("alt_text") or asset.get("caption") or fallback_title)
            alt = " ".join(re.sub(r"<[^>]+>", " ", alt).split())
            if not alt or len(alt) > 160 or re.fullmatch(r"(?:0\s*)+", alt):
                alt = fallback_title
            alt = alt.replace("[", "(").replace("]", ")")
            return [
                f"![{alt}](assets/{asset_name})",
                f"*来源页：{asset.get('page_label', '')}*",
                "",
            ]

        def render_block(
            block: dict[str, Any],
            fallback_title: str,
            rendered_assets: set[str] | None = None,
        ) -> list[str]:
            kind = str(block.get("type") or "PARAGRAPH").upper()
            lines: list[str] = []
            if kind == "BULLETED_LIST":
                lines.extend(f"- {item}" for item in block.get("items", []) if str(item).strip())
            elif kind == "NUMBERED_LIST":
                lines.extend(
                    f"{index}. {item}"
                    for index, item in enumerate(block.get("items", []), start=1)
                    if str(item).strip()
                )
            elif kind == "TABLE":

                def table_cell(value: Any) -> str:
                    # A LaTeX norm uses ``\|`` while Markdown tables also use
                    # pipe as a cell delimiter. Convert the math command to an
                    # equivalent pipe-free LaTeX command before escaping any
                    # remaining literal table pipes.
                    cell = str(value).replace(r"\|", r"\Vert{}")
                    return cell.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>")

                headers = [table_cell(item) for item in block.get("table_headers", [])]
                rows = block.get("table_rows", [])
                if headers:
                    lines.extend(
                        [
                            "| " + " | ".join(headers) + " |",
                            "| " + " | ".join("---" for _ in headers) + " |",
                        ]
                    )
                    lines.extend(
                        "| " + " | ".join(table_cell(item) for item in row) + " |" for row in rows
                    )
            elif kind == "FORMULA":
                if block.get("text"):
                    lines.extend(["$$", str(block["text"]).strip(), "$$"])
            elif kind == "CODE":
                if block.get("text"):
                    language = re.sub(r"[^A-Za-z0-9_+.-]", "", str(block.get("language") or ""))
                    lines.extend([f"```{language}", str(block["text"]).strip(), "```"])
            elif kind == "CALLOUT":
                lines.extend(
                    f"> {line}" for line in str(block.get("text") or "").splitlines() if line
                )
            elif kind != "IMAGE" and block.get("text"):
                lines.append(str(block["text"]).strip())
            if lines:
                lines.append("")
            for asset in block.get("assets", []):
                asset_key = str(asset.get("storage_key") or "")
                if rendered_assets is not None and asset_key in rendered_assets:
                    continue
                lines.extend(render_asset(asset, fallback_title))
                if rendered_assets is not None and asset_key:
                    rendered_assets.add(asset_key)
            return lines

        for order, note_id in enumerate(note_ids, start=1):
            note = self.get_note(note_id)
            filename = f"{order:02d}-{_safe_filename(note['title'])}--{note['version_id'][:8]}.md"
            evidence_pages = list(
                dict.fromkeys(
                    str(item["page_label"])
                    for item in note["evidence"]
                    if item.get("page_label") is not None
                )
            )
            frontmatter = [
                "---",
                f"note_id: {json.dumps(note['note_id'], ensure_ascii=False)}",
                f"version_id: {json.dumps(note['version_id'], ensure_ascii=False)}",
                f"source_id: {json.dumps(source.id, ensure_ascii=False)}",
                f"source_version_id: {json.dumps(source_version.id, ensure_ascii=False)}",
                f"source_title: {json.dumps(source.title, ensure_ascii=False)}",
                f"source_file: {json.dumps(source_version.original_name, ensure_ascii=False)}",
                f"quality_status: {json.dumps(note['quality'], ensure_ascii=False)}",
                f"review_status: {json.dumps(note['review'], ensure_ascii=False)}",
                f"tags: {json.dumps(note['tags'], ensure_ascii=False)}",
                f"evidence_pages: {json.dumps(evidence_pages, ensure_ascii=False)}",
                "---",
            ]
            body = [*frontmatter, "", f"# {note['title']}", ""]
            rendered_note_assets: set[str] = set()
            for block in note["blocks"]:
                body.extend(render_block(block, note["title"], rendered_note_assets))

            remaining_note_assets = [
                asset
                for asset in note["assets"]
                if str(asset.get("storage_key") or "") not in rendered_note_assets
            ]
            if remaining_note_assets:
                body.extend(["", "## 相关图片", ""])
                grouped_assets: dict[str, dict[str, Any]] = {}
                for asset in remaining_note_assets:
                    group = grouped_assets.setdefault(
                        asset["storage_key"], {"asset": asset, "pages": []}
                    )
                    if asset.get("page_label") is not None:
                        group["pages"].append(str(asset["page_label"]))
                for group in grouped_assets.values():
                    asset = group["asset"]
                    rendered = render_asset(asset, note["title"])
                    pages = "、".join(dict.fromkeys(group["pages"]))
                    if pages:
                        rendered[-2] = f"*来源页：{pages}*"
                    body.extend(rendered)

            if note["evidence"]:
                body.extend(["", "## 来源与证据", ""])
                for evidence in note["evidence"]:
                    quote = " ".join(str(evidence.get("quote") or "").split())
                    body.append(f"- 第 {evidence['page_label']} 页：{quote or '图片或结构化证据'}")

            file_path = destination / filename
            _write_text_atomic(file_path, "\n".join(body).rstrip() + "\n")
            index_links.append(f"- [{note['title']}]({filename.replace(' ', '%20')})")
            exported_files.append(str(file_path))

        with self.db.session() as session:
            source_note_id = session.scalar(
                select(SourceNote.id).where(SourceNote.source_id == source.id)
            )
        source_note_path: Path | None = None
        if source_note_id:
            source_note = self.get_source_note(source_note_id)
            source_note_lines = [
                "---",
                f"source_note_id: {json.dumps(source_note['source_note_id'], ensure_ascii=False)}",
                f"version_id: {json.dumps(source_note['version_id'], ensure_ascii=False)}",
                f"source_id: {json.dumps(source.id, ensure_ascii=False)}",
                f"source_version_id: {json.dumps(source_version.id, ensure_ascii=False)}",
                f"unit_count: {source_note['unit_count']}",
                "---",
                "",
                f"# {source_note['title']}",
                "",
                f"> {source_note['summary']}",
                "",
            ]
            rendered_source_assets: set[str] = set()

            def render_outline(
                nodes: list[dict[str, Any]],
                *,
                level_offset: int = 0,
                number_prefix: tuple[int, ...] = (),
            ) -> None:
                for node_index, node in enumerate(nodes, start=1):
                    node_number = (*number_prefix, node_index)
                    number_path = ".".join(str(value) for value in node_number)
                    number_label = f"{number_path}." if len(node_number) == 1 else number_path
                    heading_level = min(6, node["level"] + 1 - level_offset)
                    source_note_lines.extend(
                        [f"{'#' * heading_level} {number_label} {node['title']}", ""]
                    )
                    notes = node.get("notes", [])
                    for note_index, note in enumerate(notes, start=1):
                        # A leaf with one atomic unit reads naturally as one
                        # section. Multiple units retain numbered subheadings.
                        same_heading = (
                            str(note["title"]).strip().casefold()
                            == str(node["title"]).strip().casefold()
                        )
                        if len(notes) > 1 and not same_heading:
                            note_heading = min(6, heading_level + 1)
                            note_number = f"{number_path}.{note_index}"
                            source_note_lines.extend(
                                [
                                    f"{'#' * note_heading} {note_number} {note['title']}",
                                    "",
                                ]
                            )
                        for block in note["blocks"]:
                            source_note_lines.extend(
                                render_block(
                                    block,
                                    note["title"],
                                    rendered_source_assets,
                                )
                            )
                        for asset in note.get("assets", []):
                            asset_key = str(asset.get("storage_key") or "")
                            if asset_key in rendered_source_assets:
                                continue
                            source_note_lines.extend(render_asset(asset, note["title"]))
                            if asset_key:
                                rendered_source_assets.add(asset_key)
                    render_outline(
                        node.get("children", []),
                        level_offset=level_offset,
                        number_prefix=node_number,
                    )

            rendered_outline = source_note["outline"]
            level_offset = 0
            if (
                len(rendered_outline) == 1
                and rendered_outline[0]["title"].strip().casefold()
                == source_note["title"].strip().casefold()
                and not rendered_outline[0].get("notes")
            ):
                rendered_outline = rendered_outline[0].get("children", [])
                level_offset = 1
            render_outline(rendered_outline, level_offset=level_offset)
            source_note_path = destination / "source-note.md"
            _write_text_atomic(
                source_note_path,
                "\n".join(source_note_lines).rstrip() + "\n",
            )

        index = [
            f"# {source.title}",
            "",
            f"- 原文件：`{source_version.original_name}`",
            f"- 来源版本：`{source_version.id}`",
            f"- 原子笔记：{len(exported_files)} 篇",
            "",
            "## 完整笔记",
            "",
            "- [按学习大纲组织的完整笔记](source-note.md)" if source_note_path else "- 尚未生成",
            "",
            "## 笔记目录",
            "",
            *index_links,
            "",
        ]
        _write_text_atomic(destination / "index.md", "\n".join(index))
        return {
            "source_version_id": source_version_id,
            "directory": str(destination),
            "index": str(destination / "index.md"),
            "note_count": len(exported_files),
            "source_note": str(source_note_path) if source_note_path else None,
            "files": exported_files,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        fts_scores: dict[str, float] = {}
        match_query = self._fts_query(query)
        with self.db.session() as session:
            if match_query:
                rows = session.execute(
                    text(
                        """
                        SELECT f.note_version_id, bm25(note_fts, 1.0, 1.0, 0.7, 0.2) AS rank
                        FROM note_fts f
                        JOIN atomic_notes n ON n.id = f.note_id
                        WHERE note_fts MATCH :query
                          AND n.active_version_id = f.note_version_id
                          AND n.visibility = 'ACTIVE'
                          AND (:source_id IS NULL OR n.source_version_id = :source_id)
                        ORDER BY rank
                        LIMIT :limit
                        """
                    ),
                    {"query": match_query, "limit": max(limit * 4, 20), "source_id": source_version_id},
                ).all()
                for version_id, rank in rows:
                    fts_scores[version_id] = -float(rank) / (1.0 - float(rank))

            embedded_candidates = session.execute(
                select(AtomicNote, NoteVersion, NoteEmbedding)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .join(NoteEmbedding, NoteEmbedding.note_version_id == NoteVersion.id)
                .where(
                    AtomicNote.visibility == "ACTIVE",
                    *([AtomicNote.source_version_id == source_version_id] if source_version_id else []),
                    NoteEmbedding.model_name == self.embedding.model_name,
                )
            ).all()
            candidates: dict[str, tuple[AtomicNote, NoteVersion, NoteEmbedding | None]] = {
                version.id: (note, version, embedding)
                for note, version, embedding in embedded_candidates
            }
            if fts_scores:
                lexical_candidates = session.execute(
                    select(AtomicNote, NoteVersion)
                    .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                    .where(
                        AtomicNote.visibility == "ACTIVE",
                    *([AtomicNote.source_version_id == source_version_id] if source_version_id else []),
                        NoteVersion.id.in_(fts_scores),
                    )
                ).all()
                for note, version in lexical_candidates:
                    candidates.setdefault(version.id, (note, version, None))

            # A deployment may switch embedding providers before the asynchronous
            # re-index has finished.  Keep search usable during that window without
            # paying for a query embedding that cannot be compared with any stored
            # vector.  The lightweight title/thesis scan also covers CJK queries
            # that SQLite's default FTS tokenizer may not split as users expect.
            if not embedded_candidates:
                lexical_candidates = session.execute(
                    select(AtomicNote, NoteVersion)
                    .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                    .where(AtomicNote.visibility == "ACTIVE", *([AtomicNote.source_version_id == source_version_id] if source_version_id else []))
                ).all()
                for note, version in lexical_candidates:
                    candidates.setdefault(version.id, (note, version, None))

            query_vector = self.embedding.embed([query])[0] if embedded_candidates else []
            scored: list[tuple[float, AtomicNote, NoteVersion]] = []
            for note, version, embedding in candidates.values():
                vector = _parse_json(embedding.vector_json, []) if embedding is not None else []
                cosine = self._cosine(query_vector, vector)
                title_match = self._lexical_similarity(query, version.title)
                thesis_match = self._lexical_similarity(query, version.thesis)
                keyword = max(
                    fts_scores.get(version.id, 0.0),
                    title_match,
                    0.6 * thesis_match,
                )
                score = 0.55 * max(cosine, 0.0) + 0.45 * keyword
                if score > 0.0:
                    scored.append((score, note, version))
            scored.sort(key=lambda item: item[0], reverse=True)
            hits = []
            for score, note, version in (item for item in scored if item[0] >= min_score):
                if len(hits) >= limit:
                    break
                blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                hits.append(
                    SearchHit(
                        note_id=note.id,
                        note_version_id=version.id,
                        title=version.title,
                        thesis=version.thesis,
                        body="\n\n".join(block.content_text for block in blocks),
                        score=score,
                        evidence=self._evidence_for_version(session, version.id),
                        assets=self._assets_for_version(session, version.id),
                    )
                )
            return hits

    def search_knowledge(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        """Search both source-grounded AtomicNotes and reusable Concepts.

        Scores are produced by the same hybrid cosine/lexical recipe.  A small
        AtomicNote discount lets a canonical Concept win near-ties while still
        retaining source-level evidence in the result set.
        """
        query = query.strip()
        if not query:
            return []
        pool_size = max(limit * 3, 12)
        atomic_hits = self.search(query, limit=pool_size, min_score=0.0, source_version_id=source_version_id)
        concept_hits = [] if source_version_id else self.knowledge_curation.search_concepts(query, limit=pool_size)
        atomic_ranked = [
            hit.model_copy(update={"score": round(hit.score * 0.97, 6)})
            for hit in atomic_hits
            if hit.score >= min_score
        ]
        concept_ranked = [hit for hit in concept_hits if hit.score >= min_score]
        if not atomic_ranked:
            return concept_ranked[:limit]
        if not concept_ranked:
            return atomic_ranked[:limit]

        # Concept and AtomicNote scores come from different corpora and should
        # not be treated as globally calibrated probabilities.  Interleaving
        # preserves both the canonical synthesis and page-level source layer.
        concept_slots = max(1, limit // 2)
        atomic_slots = max(1, limit - concept_slots)
        selected_concepts = concept_ranked[:concept_slots]
        selected_atomic = atomic_ranked[:atomic_slots]
        ranked: list[SearchHit] = []
        for index in range(max(len(selected_concepts), len(selected_atomic))):
            if index < len(selected_concepts):
                ranked.append(selected_concepts[index])
            if index < len(selected_atomic):
                ranked.append(selected_atomic[index])
        return ranked[:limit]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        cleaned = [token.replace('"', "") for token in tokens if token]
        return " OR ".join(f'"{token}"' for token in cleaned[:12])

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        def normalize(value: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold(), flags=re.UNICODE)

        left_value = normalize(left)
        right_value = normalize(right)
        if not left_value or not right_value:
            return 0.0
        if left_value in right_value or right_value in left_value:
            return 1.0

        def bigrams(value: str) -> set[str]:
            if len(value) == 1:
                return {value}
            return {value[index : index + 2] for index in range(len(value) - 1)}

        left_parts = bigrams(left_value)
        right_parts = bigrams(right_value)
        return 2.0 * len(left_parts & right_parts) / (len(left_parts) + len(right_parts))

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _evidence_for_version(session: Session, version_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(EvidenceSpan, SourcePage, Claim, ClaimEvidence)
            .join(ClaimEvidence, ClaimEvidence.span_id == EvidenceSpan.id)
            .join(Claim, Claim.id == ClaimEvidence.claim_id)
            .join(SourcePage, SourcePage.id == EvidenceSpan.page_id)
            .where(Claim.note_version_id == version_id)
            .order_by(SourcePage.page_index)
        ).all()
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for span, page, claim, link in rows:
            source_identity = span.document_block_id or f"{page.id}:{span.normalized_text_hash}"
            unique.setdefault(
                (claim.note_block_id, source_identity),
                {
                    "span_id": span.id,
                    "note_block_id": claim.note_block_id,
                    "page_index": page.page_index,
                    "page_label": page.page_label,
                    "quote": span.quoted_text,
                    "evidence_level": span.evidence_level,
                    "role": link.role,
                },
            )
        return list(unique.values())

    def _assets_for_version(self, session: Session, version_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(NoteAsset, VisualAsset, BlobObject, SourcePage, DocumentBlock)
            .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
            .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
            .join(SourcePage, SourcePage.id == VisualAsset.page_id)
            .join(DocumentBlock, DocumentBlock.id == VisualAsset.document_block_id)
            .where(NoteAsset.note_version_id == version_id)
            .order_by(NoteAsset.order_index)
        ).all()
        return [
            {
                **_image_dimensions(str(self.blobs.path_for(blob.storage_key))),
                "visual_asset_id": visual.id,
                "note_block_id": link.note_block_id,
                "placement": link.placement,
                "page_index": page.page_index,
                "page_label": page.page_label,
                "source_pages": _parse_json(block.metadata_json, {}).get(
                    "replaces_pages", [page.page_label]
                ),
                "origin": visual.extraction_method,
                "storage_key": blob.storage_key,
                "media_type": blob.media_type,
                "caption": visual.caption,
                "purpose": link.purpose,
                "alt_text": link.alt_text,
            }
            for link, visual, blob, page, block in rows
        ]

    def ask(self, question: str, *, limit: int = 5, source_version_id: str | None = None) -> Answer:
        hits = self.search_knowledge(
            question,
            limit=limit,
            min_score=self.settings.qa_min_relevance_score,
            source_version_id=source_version_id,
        )
        answer = self.agent.answer(question, hits)
        with self.db.session() as session:
            blocked = blocked_concepts(session)
            stale = False
            for hit in hits:
                if hit.entity_type == "CONCEPT":
                    concept = session.get(Concept, hit.concept_id)
                    stale = stale or not concept or concept.visibility != "ACTIVE" or concept.id in blocked or concept.active_version_id != hit.note_version_id
                else:
                    version = session.get(NoteVersion, hit.note_version_id)
                    stale = stale or not version or not eligible_note(session, version)
            if stale:
                return Answer(answer="回答期间资料发生修改或删除，请在知识同步后重新提问。", hits=[], coverage_warning="SOURCE_CHANGED")
            incomplete_count = session.scalar(
                select(func.count(Job.id)).where(
                    Job.status == JobStatus.COMPLETED_WITH_WARNINGS.value
                )
            )
        if incomplete_count and answer.coverage_warning is None:
            answer.coverage_warning = (
                f"知识库中有 {incomplete_count} 个文档作业带覆盖警告，答案可能未覆盖全部来源。"
            )
        return answer

    def rebuild_index(self) -> int:
        with self.db.session() as session:
            session.execute(text("DELETE FROM note_fts"))
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(AtomicNote.visibility == "ACTIVE")
            ).all()
            count = 0
            embedding_inputs: list[str] = []
            embedding_records: list[tuple[NoteVersion, str]] = []
            for note, version in rows:
                blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=note.id,
                    title=version.title,
                    thesis=version.thesis,
                    body="\n\n".join(block.content_text for block in blocks),
                    tags=" ".join(_parse_json(version.tags_json, [])),
                )
                embedding_text = f"{version.title}\n{version.thesis}\n" + "\n\n".join(
                    block.content_text for block in blocks
                )
                embedding_inputs.append(embedding_text)
                embedding_records.append((version, _sha256_text(embedding_text)))
                count += 1
            vectors = self.embedding.embed(embedding_inputs) if embedding_inputs else []
            for (version, content_hash), vector in zip(embedding_records, vectors, strict=True):
                current = session.scalar(
                    select(NoteEmbedding).where(
                        NoteEmbedding.note_version_id == version.id,
                        NoteEmbedding.model_name == self.embedding.model_name,
                    )
                )
                if current is None:
                    current = NoteEmbedding(
                        note_version_id=version.id,
                        model_name=self.embedding.model_name,
                        dimension=len(vector),
                        vector_json=_json(vector),
                        content_hash=content_hash,
                    )
                    session.add(current)
                else:
                    current.dimension = len(vector)
                    current.vector_json = _json(vector)
                    current.content_hash = content_hash
                    current.ready = True
            return count
