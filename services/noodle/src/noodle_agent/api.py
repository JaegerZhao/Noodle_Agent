"""Archived LangGraph compatibility API.

Noodle_Agent production and local MVP startup use ``noodle_agent.internal_api``.
This module remains only for migration regression tests and explicit ``serve-legacy`` use.
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import AgentHarness
from .config import Settings
from .services import NoodleService


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=20)


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    active_source_note_id: str | None = None


class IngestResponse(BaseModel):
    source_id: str
    source_version_id: str
    job_id: str
    deduplicated: str
    job_url: str
    timeline_url: str
    result_url: str


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    service = NoodleService(resolved)
    service.init()
    harness = AgentHarness(service, resolved)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        harness.close()
        service.close()

    app = FastAPI(
        title="Noodle Agent",
        version="0.1.0",
        description="Local-first provenance-aware PDF knowledge agent",
        lifespan=lifespan,
    )
    app.state.noodle = service
    app.state.agent_harness = harness

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "database": service.db.healthcheck(),
            "parser": service.parser.name,
            "knowledge_agent": service.agent.name,
            "embedding": service.embedding.model_name,
            "agent_runtime": "langgraph",
            "agent_tools": len(harness.registry.manifests()),
        }

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    @app.post("/sources/pdf", response_model=IngestResponse, status_code=202)
    def ingest_pdf(
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
            return {
                **result,
                "job_url": f"/jobs/{job_id}",
                "timeline_url": f"/jobs/{job_id}/timeline",
                "result_url": f"/jobs/{job_id}/result",
            }
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    @app.get("/jobs")
    def list_jobs(limit: int = Query(default=10, ge=1, le=50)) -> list[dict]:
        return service.list_jobs(limit)

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            return service.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/jobs/{job_id}/timeline")
    def get_job_timeline(job_id: str) -> dict:
        try:
            return service.get_job_timeline(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/jobs/{job_id}/result")
    def get_job_result(job_id: str) -> dict:
        try:
            result = service.get_job_result(job_id)
            if result.get("source_note_id"):
                result["source_note_url"] = (
                    f"/source-notes/{result['source_note_id']}"
                )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/run")
    def run_job(
        job_id: str,
        wait: bool = Query(default=False),
        timeout_seconds: float = Query(default=1800, ge=1, le=7200),
    ) -> dict:
        try:
            if wait:
                return service.run_job_until_terminal(job_id, timeout_seconds=timeout_seconds)
            return service.run_job_once(job_id, force_poll=True)
        except (ValueError, TimeoutError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict:
        try:
            return service.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/notes")
    def list_notes() -> list[dict]:
        return service.list_notes()

    @app.get("/source-notes")
    def list_source_notes() -> list[dict]:
        return service.list_source_notes()

    @app.get("/source-notes/{source_note_id}")
    def get_source_note(source_note_id: str) -> dict:
        try:
            return service.get_source_note(source_note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/source-notes/{source_note_id}/reconcile")
    def reconcile_source_note(source_note_id: str) -> dict:
        try:
            return service.reconcile_source_note_release(source_note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/notes/{note_id}")
    def get_note(note_id: str) -> dict:
        try:
            return service.get_note(note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/search")
    def search(q: str = Query(min_length=1), limit: int = Query(default=5, ge=1, le=20)):
        return [hit.model_dump() for hit in service.search(q, limit=limit)]

    @app.post("/ask")
    def ask(request: AskRequest) -> dict:
        return service.ask(request.question, limit=request.limit).model_dump()

    @app.post("/agent/chat")
    def agent_chat(request: AgentChatRequest) -> dict:
        try:
            return harness.chat(
                request.message,
                conversation_id=request.conversation_id,
                attachment_ids=request.attachment_ids,
                active_source_note_id=request.active_source_note_id,
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/agent/attachments", status_code=201)
    def upload_agent_attachment(
        file: Annotated[UploadFile, File()],
        conversation_id: str | None = None,
        analyze: bool = True,
    ) -> dict:
        accepted_types = {"image/png", "image/jpeg", "image/webp"}
        if file.content_type not in accepted_types:
            raise HTTPException(
                status_code=415,
                detail="Only PNG, JPEG, or WebP agent attachments are accepted",
            )
        upload_dir = resolved.data_dir / "uploads" / "agent"
        upload_dir.mkdir(parents=True, exist_ok=True)
        request_dir = upload_dir / uuid.uuid4().hex
        request_dir.mkdir(parents=True, exist_ok=False)
        safe_name = Path(file.filename or "attachment.png").name
        target = request_dir / safe_name
        try:
            byte_count = 0
            with target.open("wb") as output:
                while chunk := file.file.read(1024 * 1024):
                    byte_count += len(chunk)
                    if byte_count > 15 * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail="Agent image attachment exceeds the 15 MB limit",
                        )
                    output.write(chunk)
            return service.create_agent_attachment(
                target,
                original_name=safe_name,
                media_type=file.content_type,
                conversation_id=conversation_id,
                analyze=analyze,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    @app.get("/agent/runs/{run_id}")
    def get_agent_run(run_id: str) -> dict:
        try:
            return harness.get_run(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/agent/conversations/{conversation_id}")
    def get_agent_conversation(conversation_id: str) -> dict:
        try:
            return harness.get_conversation(conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/admin/rebuild-index")
    def rebuild_index() -> dict:
        return {"indexed_notes": service.rebuild_index()}

    @app.post("/admin/backfill-source-notes")
    def backfill_source_notes() -> dict:
        return service.backfill_source_notes()

    @app.post("/admin/export-markdown")
    def export_markdown(source_version_id: str | None = None) -> list[dict]:
        try:
            return service.export_markdown(source_version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/blobs/{storage_key:path}")
    def get_blob(storage_key: str) -> FileResponse:
        try:
            path = service.blobs.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Blob not found")
        return FileResponse(path)

    web_dir = Path(__file__).with_name("web")
    app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")

    return app
