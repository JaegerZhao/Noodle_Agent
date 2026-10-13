from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .context import ApiContext
from .models import (
    AskRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service

    @router.get("/search")
    def search(
        q: str = Query(min_length=1),
        limit: int = Query(default=5, ge=1, le=20),
        source_version_id: str | None = None,
    ) -> list[dict]:
        return [
            hit.model_dump()
            for hit in service.search_knowledge(q, limit=limit, source_version_id=source_version_id)
        ]

    @router.post("/ask")
    def ask(request: AskRequest) -> dict:
        return service.ask(
            request.question, limit=request.limit, source_version_id=request.source_version_id
        ).model_dump()

    @router.post("/admin/rebuild-index")
    def rebuild_index() -> dict:
        return {
            "indexed_notes": service.rebuild_index(),
            "indexed_concepts": service.knowledge_curation.ensure_concept_index(force=True),
        }

    @router.post("/admin/backfill-source-notes")
    def backfill_source_notes() -> dict:
        return service.backfill_source_notes()

    @router.post("/admin/export-markdown")
    def export_markdown(source_version_id: str | None = None) -> list[dict]:
        try:
            return service.export_markdown(source_version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
