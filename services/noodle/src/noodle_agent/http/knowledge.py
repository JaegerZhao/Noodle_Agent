from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .context import ApiContext
from .gateway import harness_actor
from .models import (
    AcceptConceptContributionRequest,
    CreateSynthesisRequest,
    KnowledgeSyncRetryRequest,
    MergeConceptsRequest,
    PublishSynthesisRequest,
    TransitionConceptContributionRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service

    @router.get("/knowledge-sync")
    def list_knowledge_sync(source_id: str | None = None) -> list[dict]:
        return service.knowledge_sync.events(source_id)

    @router.get("/knowledge-sync/{event_id}/proposals")
    def knowledge_sync_proposals(event_id: str) -> list[dict]:
        return service.knowledge_sync.proposals(event_id)

    @router.post("/knowledge-sync/{event_id}/retry")
    def retry_knowledge_sync(
        event_id: str, request: KnowledgeSyncRetryRequest | None = None
    ) -> dict:
        try:
            return service.knowledge_sync.retry(
                event_id, module_ids=request.module_ids if request else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/sources/{source_id}/restore")
    def restore_knowledge_source(source_id: str) -> dict:
        try:
            return service.knowledge_sync.restore_source(source_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/notes/{note_id}")
    def delete_knowledge_note(note_id: str) -> dict:
        try:
            return service.knowledge_sync.delete_note(note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/syntheses")
    def list_syntheses() -> list[dict]:
        return service.list_syntheses()

    @router.post("/syntheses", status_code=201)
    def create_synthesis(
        payload: CreateSynthesisRequest,
        request: Request,
    ) -> dict:
        try:
            return service.create_synthesis(
                stable_key=payload.stable_key,
                domain_key=payload.domain_key,
                title=payload.title,
                description=payload.description,
                outline=[item.model_dump() for item in payload.outline],
                actor=harness_actor(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/syntheses/{synthesis_id}")
    def get_synthesis(synthesis_id: str) -> dict:
        try:
            return service.get_synthesis(synthesis_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/syntheses/{synthesis_id}")
    def publish_synthesis(
        synthesis_id: str,
        payload: PublishSynthesisRequest,
        request: Request,
    ) -> dict:
        try:
            return service.publish_synthesis(
                synthesis_id,
                expected_version_id=payload.expected_version_id,
                title=payload.title,
                description=payload.description,
                outline=[item.model_dump() for item in payload.outline],
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 409 if "conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/concepts")
    def list_concepts(
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict]:
        return service.list_concepts(q, limit=limit)

    @router.post("/concepts/merge-preview")
    def preview_concept_merge(payload: MergeConceptsRequest) -> dict:
        try:
            return service.knowledge_curation.preview_merge(
                source_concept_id=payload.source_concept_id,
                target_concept_id=payload.target_concept_id,
                expected_source_version_id=payload.expected_source_version_id,
                expected_target_version_id=payload.expected_target_version_id,
            )
        except ValueError as exc:
            status = 409 if "conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.post("/concepts/merge")
    def merge_concepts(payload: MergeConceptsRequest, request: Request) -> dict:
        try:
            return service.merge_concepts(
                source_concept_id=payload.source_concept_id,
                target_concept_id=payload.target_concept_id,
                expected_source_version_id=payload.expected_source_version_id,
                expected_target_version_id=payload.expected_target_version_id,
                rationale=payload.rationale,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 409 if "conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/concepts/{concept_id}")
    def get_concept(concept_id: str, version_id: str | None = None) -> dict:
        try:
            return service.get_concept(concept_id, version_id=version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/concepts/{concept_id}/candidates")
    def get_concept_candidates(
        concept_id: str,
        module_key: str = Query(min_length=1, max_length=180),
        limit: int = Query(default=8, ge=1, le=20),
    ) -> list[dict]:
        try:
            return service.suggest_concept_candidates(concept_id, module_key, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/concepts/{concept_id}/updates")
    def list_concept_updates(
        concept_id: str,
        limit: int = Query(default=10, ge=1, le=50),
    ) -> list[dict]:
        try:
            return service.list_concept_update_jobs(concept_id, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/concepts/{concept_id}/contributions")
    def list_concept_contributions(
        concept_id: str,
        module_key: str | None = Query(default=None, max_length=180),
    ) -> list[dict]:
        try:
            return service.list_concept_contributions(concept_id, module_key=module_key)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/concepts/{concept_id}/contributions")
    def accept_concept_contribution(
        concept_id: str,
        payload: AcceptConceptContributionRequest,
        request: Request,
    ) -> dict:
        try:
            return service.accept_atomic_note_contribution(
                concept_id=concept_id,
                module_key=payload.module_key,
                note_version_id=payload.note_version_id,
                expected_concept_version_id=payload.expected_concept_version_id,
                rationale=payload.rationale,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 409 if "version conflict" in str(exc).casefold() else 400
            job_id = str(getattr(exc, "job_id", ""))
            detail: str | dict[str, str] = str(exc)
            if job_id:
                detail = {
                    "message": str(exc),
                    "job_id": job_id,
                    "error_code": str(getattr(exc, "error_code", "BUILD_FAILED")),
                }
            raise HTTPException(status_code=status, detail=detail) from exc

    @router.get("/concept-updates/{job_id}")
    def get_concept_update_job(job_id: str) -> dict:
        try:
            return service.get_concept_update_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/concept-updates/{job_id}/diff")
    def get_concept_update_diff(job_id: str) -> dict:
        try:
            return service.get_concept_update_diff(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/concept-updates/{job_id}/retry")
    def retry_concept_update(job_id: str, request: Request) -> dict:
        try:
            return service.retry_concept_update_job(job_id, actor=harness_actor(request))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/concept-contributions/{contribution_id}/transition")
    def transition_concept_contribution(
        contribution_id: str,
        payload: TransitionConceptContributionRequest,
        request: Request,
    ) -> dict:
        try:
            return service.transition_concept_contribution(
                contribution_id=contribution_id,
                action=payload.action,
                expected_concept_version_id=payload.expected_concept_version_id,
                rationale=payload.rationale,
                actor=harness_actor(request),
                replacement_note_version_id=payload.replacement_note_version_id,
            )
        except ValueError as exc:
            status = 409 if "conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/concept-figures/{figure_id}")
    def get_concept_figure(figure_id: str) -> FileResponse:
        try:
            path = service.get_backbone_figure_path(figure_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    return router
