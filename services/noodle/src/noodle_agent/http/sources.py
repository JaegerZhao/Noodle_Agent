from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from .context import ApiContext
from .gateway import harness_actor
from .models import (
    FolderCreateRequest,
    FolderRenameRequest,
    IngestResponse,
    MoveSourceRequest,
    ReorderSourcesRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service
    resolved = context.settings

    @router.get("/library")
    def get_library() -> dict:
        return service.get_library_overview()

    @router.get("/folders")
    def list_folders() -> list[dict]:
        return service.list_library_folders()

    @router.post("/folders", status_code=201)
    def create_folder(payload: FolderCreateRequest) -> dict:
        try:
            return service.create_library_folder(
                payload.name,
                parent_id=payload.parent_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/folders/{folder_id}")
    def rename_folder(folder_id: str, payload: FolderRenameRequest) -> dict:
        try:
            return service.rename_library_folder(folder_id, payload.name)
        except ValueError as exc:
            status = 404 if "Unknown folder" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.put("/sources/{source_id}/folder")
    def move_source(source_id: str, payload: MoveSourceRequest) -> dict:
        try:
            return service.move_library_source(source_id, folder_id=payload.folder_id)
        except ValueError as exc:
            status = 404 if "Unknown" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: str, request: Request) -> dict:
        try:
            return service.delete_source_local_data(
                source_id,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 404 if "Unknown source" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/sources/{source_id}/pdf")
    def get_source_pdf(source_id: str) -> FileResponse:
        try:
            pdf = service.get_source_pdf(source_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            pdf["path"],
            media_type=pdf["media_type"],
            filename=pdf["filename"],
            content_disposition_type="inline",
        )

    @router.get("/sources/{source_id}/original-content")
    def get_source_original_content(source_id: str) -> FileResponse:
        """Return opaque bytes for in-app viewing without invoking PDF download handlers."""
        try:
            pdf = service.get_source_pdf(source_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            pdf["path"],
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Noodle-Media-Type": "application/pdf",
            },
        )

    @router.put("/folders/{folder_id}/source-order")
    def reorder_sources(folder_id: str, payload: ReorderSourcesRequest) -> dict:
        try:
            return service.reorder_library_sources(folder_id, payload.source_ids)
        except ValueError as exc:
            status = 404 if "Unknown folder" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.post("/sources/pdf", response_model=IngestResponse, status_code=202)
    def ingest_pdf(
        request: Request,
        file: Annotated[UploadFile, File()],
        title: str | None = None,
        source_id: str | None = None,
        new_version: bool = False,
    ) -> dict:
        if file.content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(status_code=415, detail="Only PDF uploads are accepted")
        upload_dir = resolved.data_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename or "upload.pdf").name
        request_dir = upload_dir / uuid.uuid4().hex
        request_dir.mkdir(parents=True, exist_ok=False)
        target = request_dir / safe_name
        try:
            byte_count = 0
            with target.open("wb") as output:
                while chunk := file.file.read(1024 * 1024):
                    byte_count += len(chunk)
                    if byte_count > resolved.upload_max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "PDF exceeds configured upload limit of "
                                f"{resolved.upload_max_bytes} bytes"
                            ),
                        )
                    output.write(chunk)
            result = service.ingest_pdf(
                target,
                title=title,
                source_id=source_id,
                force_new_version=new_version,
            )
            job_id = result["job_id"]
            service.link_job_to_harness(
                job_id,
                session_id=request.headers.get("x-harness-session-id"),
                tool_call_id=request.headers.get("x-harness-tool-call-id"),
            )
            return {
                **result,
                "job_url": f"/internal/v1/jobs/{job_id}",
                "timeline_url": f"/internal/v1/jobs/{job_id}/timeline",
                "result_url": f"/internal/v1/jobs/{job_id}/result",
            }
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    return router
