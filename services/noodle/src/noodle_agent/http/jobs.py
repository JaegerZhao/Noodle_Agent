from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from .context import ApiContext
from .gateway import harness_actor
from .models import (
    RestartGenerationRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service

    @router.get("/jobs")
    def list_jobs(limit: int = Query(default=10, ge=1, le=50)) -> list[dict]:
        return service.list_jobs(limit)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            return service.get_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/timeline")
    def get_job_timeline(job_id: str) -> dict:
        try:
            return service.get_job_timeline(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/result")
    def get_job_result(job_id: str) -> dict:
        try:
            result = service.get_job_result(job_id)
            if result.get("source_note_id"):
                result["source_note_url"] = f"/internal/v1/source-notes/{result['source_note_id']}"
            return result
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/jobs/{job_id}/run")
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

    @router.post("/jobs/{job_id}/resume")
    def resume_job(job_id: str) -> dict:
        try:
            return service.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/jobs/{job_id}/restart-generation")
    def restart_generation(
        job_id: str,
        payload: RestartGenerationRequest,
        request: Request,
    ) -> dict:
        try:
            return service.restart_generation(
                job_id,
                payload.from_step,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
