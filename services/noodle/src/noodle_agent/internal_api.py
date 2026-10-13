from __future__ import annotations

import mimetypes
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .http import assets, harness, jobs, knowledge, notes, retrieval, sources
from .http.context import ApiContext
from .http.gateway import HarnessGateway
from .services import NoodleService

mimetypes.add_type("text/javascript", ".mjs")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the domain-only API consumed by DeepSeek Harness.

    This process serves the knowledge workbench and owns no conversation
    runtime or model-facing tool planner. DeepSeek Harness is the only interactive
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
        if (
            request.url.path.startswith("/app/workbench/") and request.url.path.endswith(".js")
        ) or request.url.path in {
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

    context = ApiContext(service, resolved, HarnessGateway(resolved))
    app.include_router(harness.create_router(context), prefix="/internal/v1")
    app.include_router(knowledge.create_router(context), prefix="/internal/v1")
    app.include_router(sources.create_router(context), prefix="/internal/v1")
    app.include_router(jobs.create_router(context), prefix="/internal/v1")
    app.include_router(notes.create_router(context), prefix="/internal/v1")
    app.include_router(retrieval.create_router(context), prefix="/internal/v1")
    app.include_router(assets.create_router(context), prefix="/internal/v1")
    web_dir = Path(__file__).with_name("web")

    @app.get("/app/", include_in_schema=False)
    def knowledge_workbench() -> FileResponse:
        return FileResponse(web_dir / "workbench.html")

    app.mount("/app", StaticFiles(directory=web_dir, html=True), name="knowledge-workbench")
    return app
