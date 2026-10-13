from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import httpx
import websockets
from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .services import NoodleService

mimetypes.add_type("text/javascript", ".mjs")


class KnowledgeSyncRetryRequest(BaseModel):
    module_ids: list[str] = Field(default_factory=list, max_length=100)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    source_version_id: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class RestartGenerationRequest(BaseModel):
    from_step: str = Field(
        pattern="^(profile|knowledge-map|outline|note-set|vision|drafts|evaluation|validation)$"
    )


class EditNoteBlockRequest(BaseModel):
    expected_version_id: str = Field(min_length=1)
    block_key: str = Field(min_length=1, max_length=200)
    new_text: str = Field(min_length=1, max_length=100_000)


class AcceptConceptContributionRequest(BaseModel):
    note_version_id: str = Field(min_length=1)
    expected_concept_version_id: str = Field(min_length=1)
    module_key: str = Field(min_length=1, max_length=180)
    rationale: str = Field(min_length=1, max_length=2_000)


class TransitionConceptContributionRequest(BaseModel):
    action: Literal["REJECT", "RETRACT", "SUPERSEDE"]
    expected_concept_version_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    replacement_note_version_id: str | None = None


class SynthesisOutlineNodeRequest(BaseModel):
    stable_key: str | None = Field(default=None, max_length=220)
    title: str = Field(min_length=1, max_length=500)
    node_kind: Literal["GROUP", "CONCEPT"] = "CONCEPT"
    concept_id: str | None = None
    children: list[SynthesisOutlineNodeRequest] = Field(default_factory=list)


class CreateSynthesisRequest(BaseModel):
    stable_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,159}$")
    domain_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4_000)
    outline: list[SynthesisOutlineNodeRequest] = Field(min_length=1, max_length=200)


class PublishSynthesisRequest(BaseModel):
    expected_version_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4_000)
    outline: list[SynthesisOutlineNodeRequest] = Field(min_length=1, max_length=200)


class MergeConceptsRequest(BaseModel):
    source_concept_id: str = Field(min_length=1)
    target_concept_id: str = Field(min_length=1)
    expected_source_version_id: str = Field(min_length=1)
    expected_target_version_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class ReplaceImagesRequest(BaseModel):
    attachment_id: str = Field(min_length=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    expected_source_note_version_id: str = Field(min_length=1)
    target_note_title: str | None = None
    caption: str | None = None
    purpose: str | None = None


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None


class FolderRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MoveSourceRequest(BaseModel):
    folder_id: str | None = None


class ReorderSourcesRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=500)


class HarnessSessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class HarnessSessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class HarnessPromptRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=100_000)
    context: str | None = Field(default=None, max_length=8_000)
    mode: str = Field(default="queue", pattern="^(queue|steer)$")


class HarnessApprovalDecisionRequest(BaseModel):
    outcome: Literal["allowed-once", "rejected"]


class IngestResponse(BaseModel):
    source_id: str
    source_version_id: str
    job_id: str
    deduplicated: str
    job_url: str
    timeline_url: str
    result_url: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the domain-only API consumed by DeepSeek Harness.

    This process owns no conversation runtime, LangGraph checkpoint, static Web
    UI, or model-facing tool planner. DeepSeek Harness is the only interactive
    Agent; this app remains the authority for knowledge-domain operations.
    """

    resolved = settings or Settings.from_env()
    service = NoodleService(resolved)
    service.init()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        service.close()

    app = FastAPI(
        title="Noodle Domain API",
        version="1.0.0",
        description="Internal knowledge-domain API for the Noodle DeepSeek Harness",
        lifespan=lifespan,
        docs_url="/internal/docs",
        openapi_url="/internal/openapi.json",
        redoc_url=None,
    )
    app.state.noodle = service

    def harness_actor(request: Request) -> str:
        session_id = request.headers.get("x-harness-session-id")
        call_id = request.headers.get("x-harness-tool-call-id")
        if session_id:
            return f"harness-session:{session_id}"
        if call_id:
            return f"harness-call:{call_id}"
        return "internal-api-user"

    async def harness_rpc(method: str, payload: dict) -> dict:
        """Call the loopback Harness RPC without exposing an unrestricted proxy."""

        rpc_id = str(uuid.uuid4())
        envelope = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": payload,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{resolved.harness_web_url}/api/{method}",
                    json=envelope,
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness is unavailable: {exc}",
            ) from exc
        if body.get("rpcId") != rpc_id:
            raise HTTPException(
                status_code=502, detail="Harness returned a mismatched RPC response"
            )
        result = body.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            detail = error.get("message") or error.get("code") or "Harness RPC failed"
            raise HTTPException(status_code=409, detail=detail)
        value = result.get("value")
        return value if isinstance(value, dict) else {"value": value}

    async def pending_harness_approval_rpc_id(
        session_id: str,
        approval_id: str,
    ) -> str:
        """Resolve the transient response RPC id for one durable approval id.

        DSH deliberately sends approvals on its WebSocket mux and accepts answers
        at /api/respond. Opening a new mux replays every still-pending approval, so
        the Noodle adapter can answer safely without owning a second Agent loop.
        """

        base_url = resolved.harness_web_url.rstrip("/")
        if base_url.startswith("https://"):
            mux_url = f"wss://{base_url.removeprefix('https://')}/api/events.mux"
        elif base_url.startswith("http://"):
            mux_url = f"ws://{base_url.removeprefix('http://')}/api/events.mux"
        else:
            raise HTTPException(status_code=500, detail="Unsupported Harness Web URL")
        try:
            async with asyncio.timeout(5.0):
                async with websockets.connect(
                    mux_url,
                    open_timeout=3.0,
                    close_timeout=1.0,
                    max_size=2 * 1024 * 1024,
                ) as socket:
                    while True:
                        raw = await socket.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        try:
                            envelope = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        payload = envelope.get("payload") or {}
                        if (
                            payload.get("type") == "approval/requested"
                            and payload.get("sessionId") == session_id
                            and payload.get("approvalId") == approval_id
                        ):
                            rpc_id = envelope.get("rpcId")
                            if isinstance(rpc_id, str) and rpc_id:
                                return rpc_id
        except TimeoutError as exc:
            raise HTTPException(
                status_code=409,
                detail="Approval is no longer pending or could not be observed",
            ) from exc
        except (OSError, websockets.WebSocketException) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness approval channel is unavailable: {exc}",
            ) from exc
        raise HTTPException(status_code=409, detail="Approval is no longer pending")

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        correlation_id = (
            request.headers.get("x-harness-tool-call-id")
            or request.headers.get("x-correlation-id")
            or uuid.uuid4().hex
        )
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        # The workbench is developed and served by the same local process.  Do not
        # let a long-lived browser tab keep an old shell or first-party bundle after
        # an upgrade; versioned HTML references provide a second line of defence.
        if request.url.path in {
            "/app",
            "/app/",
            "/app/workbench.css",
            "/app/workbench.js",
        }:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "database": service.db.healthcheck(),
            "parser": service.parser.name,
            "knowledge_agent": service.agent.name,
            "embedding": service.embedding.model_name,
            "api_surface": "internal-v1",
            "agent_runtime": "external-deepseek-harness",
            "legacy_agent_loaded": False,
            "legacy_web_loaded": False,
            "knowledge_workbench_loaded": True,
        }

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse(url="/app/")

    router = APIRouter(prefix="/internal/v1")

    @router.get("/ui-config")
    def ui_config() -> dict:
        return {
            "harness_web_url": resolved.harness_web_url,
            "harness_chat_api": "/internal/v1/harness",
            "api_prefix": "/internal/v1",
            "poll_interval_ms": 2500,
        }

    @router.get("/harness/sessions")
    async def list_harness_sessions() -> dict:
        return await harness_rpc("session.list", {})

    @router.post("/harness/sessions", status_code=201)
    async def create_harness_session(payload: HarnessSessionCreateRequest) -> dict:
        created = await harness_rpc("session.create", {})
        title = (payload.title or "").strip()
        if title:
            renamed = await harness_rpc(
                "session.rename",
                {"sessionId": created["sessionId"], "title": title},
            )
            created["title"] = renamed.get("title", title)
        return created

    @router.patch("/harness/sessions/{session_id}")
    async def rename_harness_session(
        session_id: str,
        payload: HarnessSessionRenameRequest,
    ) -> dict:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Session title cannot be blank")
        return await harness_rpc(
            "session.rename",
            {"sessionId": session_id, "title": title},
        )

    @router.get("/harness/sessions/{session_id}/history")
    async def get_harness_history(
        session_id: str,
        max_messages: int = Query(default=80, ge=1, le=200),
    ) -> dict:
        return await harness_rpc(
            "session.history",
            {"sessionId": session_id, "maxMessages": max_messages},
        )

    @router.post("/harness/prompt", status_code=202)
    async def prompt_harness(payload: HarnessPromptRequest) -> dict:
        prompt = payload.message.strip()
        if payload.context and payload.context.strip():
            prompt = (
                "可信 Noodle 界面定位（仅用于选择实体；正文与证据必须通过工具读取）：\n"
                f"{payload.context.strip()}\n\n用户请求：\n{prompt}"
            )
        return await harness_rpc(
            "session.prompt",
            {
                "sessionId": payload.session_id,
                "mode": payload.mode,
                "content": [{"type": "text", "text": prompt}],
                "clientTimeZone": "Asia/Shanghai",
            },
        )

    @router.post("/harness/sessions/{session_id}/approvals/{approval_id}")
    async def decide_harness_approval(
        session_id: str,
        approval_id: str,
        payload: HarnessApprovalDecisionRequest,
    ) -> dict:
        rpc_id = await pending_harness_approval_rpc_id(session_id, approval_id)
        response_envelope = {
            "type": "client-response",
            "rpcId": rpc_id,
            "result": {
                "ok": True,
                "value": {
                    "sessionId": session_id,
                    "approvalId": approval_id,
                    "outcome": payload.outcome,
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{resolved.harness_web_url}/api/respond",
                    json=response_envelope,
                )
                response.raise_for_status()
                receipt = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness approval response failed: {exc}",
            ) from exc
        if receipt.get("accepted") is not True:
            raise HTTPException(
                status_code=409,
                detail=receipt.get("reason") or "Approval is no longer pending",
            )
        return {
            "accepted": True,
            "session_id": session_id,
            "approval_id": approval_id,
            "outcome": payload.outcome,
        }

    @router.post("/harness/sessions/{session_id}/cancel")
    async def cancel_harness_session(session_id: str) -> dict:
        return await harness_rpc("session.cancel", {"sessionId": session_id})

    @router.get("/knowledge-sync")
    def list_knowledge_sync(source_id: str | None = None) -> list[dict]:
        return service.knowledge_sync.events(source_id)

    @router.get("/knowledge-sync/{event_id}/proposals")
    def knowledge_sync_proposals(event_id: str) -> list[dict]:
        return service.knowledge_sync.proposals(event_id)

    @router.post("/knowledge-sync/{event_id}/retry")
    def retry_knowledge_sync(event_id: str, request: KnowledgeSyncRetryRequest | None = None) -> dict:
        try:
            return service.knowledge_sync.retry(event_id, module_ids=request.module_ids if request else None)
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

    @router.get("/library")
    def get_library() -> dict:
        return service.get_library_overview()

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

    @router.post("/attachments/images", status_code=201)
    def upload_image_attachment(
        file: Annotated[UploadFile, File()],
        analyze: bool = True,
    ) -> dict:
        if file.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise HTTPException(
                status_code=415, detail="Only PNG, JPEG, or WebP images are accepted"
            )
        request_dir = resolved.data_dir / "uploads" / uuid.uuid4().hex
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
                # AgentAttachment.conversation_id belongs to the removed legacy
                # conversation table. Harness identity stays in its own session
                # log and is attached to the eventual mutation audit actor.
                conversation_id=None,
                analyze=analyze,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    @router.get("/attachments/{attachment_id}")
    def get_attachment(attachment_id: str) -> dict:
        try:
            return service.get_agent_attachments([attachment_id])[0]
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/notes")
    def list_notes() -> list[dict]:
        return service.list_notes()

    @router.get("/notes/{note_id}")
    def get_note(note_id: str) -> dict:
        try:
            return service.get_note(note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/notes/{note_id}/blocks")
    def edit_note_block(
        note_id: str,
        payload: EditNoteBlockRequest,
        request: Request,
    ) -> dict:
        try:
            return service.edit_note_block(
                note_id,
                expected_version_id=payload.expected_version_id,
                block_key=payload.block_key,
                new_text=payload.new_text,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 409 if "version conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/source-notes")
    def list_source_notes() -> list[dict]:
        return service.list_source_notes()

    @router.get("/source-notes/{source_note_id}")
    def get_source_note(source_note_id: str) -> dict:
        try:
            return service.get_source_note(source_note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/source-notes/{source_note_id}/reconcile")
    def reconcile_source_note(source_note_id: str) -> dict:
        try:
            return service.reconcile_source_note_release(source_note_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/source-notes/{source_note_id}/replace-images")
    def replace_source_note_images(
        source_note_id: str,
        payload: ReplaceImagesRequest,
        request: Request,
    ) -> dict:
        try:
            return service.replace_source_note_images(
                source_note_id=source_note_id,
                attachment_id=payload.attachment_id,
                page_start=payload.page_start,
                page_end=payload.page_end,
                target_note_title=payload.target_note_title,
                caption=payload.caption,
                purpose=payload.purpose,
                expected_source_note_version_id=payload.expected_source_note_version_id,
                actor=harness_actor(request),
            )
        except ValueError as exc:
            status = 409 if "version conflict" in str(exc).casefold() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.get("/search")
    def search(
        q: str = Query(min_length=1),
        limit: int = Query(default=5, ge=1, le=20),
        source_version_id: str | None = None,
    ) -> list[dict]:
        return [hit.model_dump() for hit in service.search_knowledge(q, limit=limit, source_version_id=source_version_id)]

    @router.post("/ask")
    def ask(request: AskRequest) -> dict:
        return service.ask(request.question, limit=request.limit, source_version_id=request.source_version_id).model_dump()

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

    @router.get("/blobs/{storage_key:path}")
    def get_blob(storage_key: str) -> FileResponse:
        try:
            path = service.blobs.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Blob not found")
        return FileResponse(path)

    app.include_router(router)
    web_dir = Path(__file__).with_name("web")

    @app.get("/app/", include_in_schema=False)
    def knowledge_workbench() -> FileResponse:
        return FileResponse(web_dir / "workbench.html")

    app.mount("/app", StaticFiles(directory=web_dir, html=True), name="knowledge-workbench")
    return app
