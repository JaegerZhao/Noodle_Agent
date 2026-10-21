"""Explicit next-only API and minimal Concept reader, isolated from legacy data."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi import Query as Param
from fastapi.responses import FileResponse, JSONResponse
from openai import APIConnectionError, APIStatusError
from sqlalchemy import text

from ..config import Settings
from .concepts.review import Resolution
from .concepts.review import resolve as resolve_contribution
from .contracts import sha
from .planning import QualityFailure
from .repository import Conflict, InvalidKnowledge, one, rows
from .retrieval.contracts import AnswerResponse, Query, SearchResponse
from .runtime import Application, backend_from_env, real_agent
from .store import Store


def create_app(root=None, *, application=None):
    if application:
        app_service = application
        store = application.store
        owned = False
    else:
        settings = Settings.from_env()
        store = Store(Path(root or "./data/noodle-next"))
        owned = True
        try:
            agent = real_agent(settings)
        except ValueError:
            agent = None
        app_service = Application(store, settings=settings, agent=agent, backend=backend_from_env())

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            store.close()

    app = FastAPI(title="Noodle next knowledge API", version="next-v1", lifespan=lifespan)
    app.state.application = app_service

    @app.exception_handler(InvalidKnowledge)
    async def invalid(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def no_stale_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def provider_error(request, exc):
        return JSONResponse(
            status_code=503, content={"status": "PROVIDER_ERROR", "detail": type(exc).__name__}
        )

    for error in (
        httpx.TransportError,
        APIConnectionError,
        APIStatusError,
        ConnectionError,
        TimeoutError,
    ):
        app.add_exception_handler(error, provider_error)

    @app.exception_handler(QualityFailure)
    async def quality_error(request, exc):
        return JSONResponse(
            status_code=409, content={"status": "REVIEW_REQUIRED", "detail": str(exc)}
        )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "contract": "next-v1",
            "schema": store.db.last_migration["current"],
            "legacy_loaded": False,
        }

    @app.post("/internal/next/search", response_model=SearchResponse)
    def search(query: Query):
        return app_service.searcher.search(query)

    @app.post("/internal/next/answer", response_model=AnswerResponse)
    def answer(query: Query):
        return app_service.answerer.answer(query)

    @app.get("/internal/next/concepts")
    def concepts(limit: int = Param(50, ge=1, le=200), offset: int = Param(0, ge=0), q: str = ""):
        with store.db.engine.connect() as c:
            result = rows(
                c,
                "SELECT n.*,w.status maintenance_status,w.desired_generation,w.completed_generation FROM notes n LEFT JOIN concept_work_state w ON w.concept_id=n.id WHERE n.kind='CONCEPT' AND (n.title LIKE :q OR n.aliases_json LIKE :q) ORDER BY n.created_at,n.id LIMIT :limit OFFSET :offset",
                limit=limit,
                offset=offset,
                q="%" + q + "%",
            )
            return {
                "items": [
                    {
                        **dict(n),
                        "eligible": bool(n["current_revision_id"])
                        and store.content.valid_revision(c, n["current_revision_id"]),
                    }
                    for n in result
                ],
                "total": c.execute(
                    text(
                        "SELECT count(*) FROM notes WHERE kind='CONCEPT' AND (title LIKE :q OR aliases_json LIKE :q)"
                    ),
                    {"q": "%" + q + "%"},
                ).scalar_one(),
            }

    @app.get("/internal/next/notes/{identity}")
    def note(identity: str, source_revision_id: str | None = None):
        try:
            result = store.get_note(identity)
            if result["kind"] not in {"ATOMIC", "CONCEPT"}:
                raise HTTPException(409, "MOC is navigation, not a fact note")
            with store.db.engine.connect() as c:
                evidence = rows(
                    c,
                    "SELECT se.section_id,e.source_revision_id,e.page_number FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=:r",
                    r=result["revision"]["id"],
                )
            if source_revision_id and (
                result["kind"] != "ATOMIC"
                or any(e["source_revision_id"] != source_revision_id for e in evidence)
            ):
                raise HTTPException(409, "Note exceeds the selected source revision scope")
            result["sections"] = [
                {
                    **s,
                    "provenance": [
                        {"source_revision_id": e["source_revision_id"], "page": e["page_number"]}
                        for e in evidence
                        if e["section_id"] == s["section_id"]
                    ],
                }
                for s in result["sections"]
            ]
            return result
        except KeyError as exc:
            raise HTTPException(404, "Unknown note") from exc

    @app.get("/internal/next/concepts/{identity}")
    def concept(identity: str, version_id: str | None = None):
        result = note(identity)
        if version_id and version_id != result["revision"]["id"]:
            raise HTTPException(409, "Pinned Concept revision is not current")
        if result["kind"] != "CONCEPT":
            raise HTTPException(404, "Unknown Concept")
        return result

    @app.get("/internal/next/concepts/{identity}/maintenance")
    def maintenance(identity: str):
        with store.db.engine.connect() as c:
            state = one(c, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=identity)
            decisions = rows(
                c,
                "SELECT id,atomic_revision_id,fragment_id,action,status,decision_json,applied_revision_id FROM contribution_decisions WHERE concept_id=:id ORDER BY created_at",
                id=identity,
            )
            return {
                "state": dict(state) if state else None,
                "decisions": [
                    {**dict(d), "decision_sha256": sha(d["decision_json"])} for d in decisions
                ],
            }

    @app.post("/internal/next/maintenance/decisions/{identity}/resolve")
    def resolve_decision(identity: str, value: Resolution):
        try:
            return resolve_contribution(store, identity, value)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/internal/next/source-revisions/{revision}")
    def resolve(revision: str):
        with store.db.engine.connect() as c:
            row = one(c, "SELECT source_id FROM source_revisions WHERE id=:id", id=revision)
            if not row:
                raise HTTPException(404, "Unknown source revision")
            if not store.sources.source_valid(c, revision):
                raise HTTPException(409, "Source revision changed or was deleted")
            return {"source_id": row["source_id"], "revision_id": revision}

    @app.get("/internal/next/sources/{identity}/file")
    def source_file(identity: str):
        with store.db.engine.connect() as c:
            row = one(
                c,
                "SELECT r.*,s.kind FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.id=:id AND s.deleted_at IS NULL",
                id=identity,
            )
        if not row or row["kind"] != "PDF":
            raise HTTPException(404, "Unknown active PDF")
        store.read(row["blob_ref"], row["sha256"])
        return FileResponse(store.path(row["blob_ref"]), media_type="application/pdf")

    @app.post("/internal/next/sources")
    async def upload(file: Annotated[UploadFile, File()]):
        import fitz

        content = await file.read(100 * 1024 * 1024 + 1)
        if len(content) > 100 * 1024 * 1024:
            raise HTTPException(413, "PDF exceeds 100 MiB")
        try:
            with fitz.open(stream=content, filetype="pdf") as pdf:
                if not pdf.page_count:
                    raise ValueError("Empty PDF")
        except Exception as exc:
            raise HTTPException(422, "Invalid PDF") from exc
        return store.add_source(content, file.filename or "PDF", original_name=file.filename or "")

    @app.get("/internal/next/jobs")
    def jobs():
        with store.db.engine.connect() as c:
            return [
                dict(r)
                for r in rows(
                    c,
                    "SELECT id,kind,status,attempts,error_json,result_json FROM jobs ORDER BY created_at DESC LIMIT 200",
                )
            ]

    return app
