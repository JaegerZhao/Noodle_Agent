from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..adapters.mineru import validate_pdf
from ..composition import Component
from ..db import (
    AuditEvent,
    BlobObject,
    Job,
    LibraryEntry,
    LibraryFolder,
    LlmCallEvent,
    Source,
    SourceNote,
    SourceNoteVersion,
    SourceVersion,
    VisionCallEvent,
    utcnow,
)
from .common import (
    TERMINAL_STATUSES,
    _json,
    _parse_json,
    _utc_iso,
)


class SourcesModule(Component):
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
        storage_key, sha256, byte_size = self.owner.blobs.put(pdf_path, "application/pdf")
        if self.owner.settings.parser == "fixture":
            sidecar = pdf_path.with_suffix(".mineru.json")
            if sidecar.exists():
                blob_pdf = self.owner.blobs.path_for(storage_key)
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
        with self.owner.db.session() as session:
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

    def list_library_folders(self) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
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
        with self.owner.db.session() as session:
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
        with self.owner.db.session() as session:
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
        with self.owner.db.session() as session:
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
        with self.owner.db.session() as session:
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
        with self.owner.db.session() as session:
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
                job_payload = self.owner._job_dict(job) if job else None
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
                        "source_kind": "PDF"
                        if version.media_type == "application/pdf"
                        else "TEXTBOOK",
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
                "folders": self.owner.list_library_folders(),
                "sources": rows,
                "jobs": [self.owner._job_dict(job) for job in all_jobs[:50]],
            }

    def get_source_pdf(self, source_id: str) -> dict[str, Any]:
        """Resolve the active source's original PDF without exposing blob storage keys."""
        with self.owner.db.session() as session:
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
            path = self.owner.blobs.path_for(blob.storage_key)
            if not path.is_file():
                raise FileNotFoundError("Source PDF file is missing")
            return {
                "path": path,
                "filename": version.original_name,
                "media_type": version.media_type or "application/pdf",
                "source_version_id": version.id,
            }

    def delete_source_local_data(
        self, source_id: str, *, actor: str = "local-user"
    ) -> dict[str, Any]:
        return self.owner.knowledge_sync.delete_source(source_id, actor)
