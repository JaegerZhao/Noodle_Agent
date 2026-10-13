from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .context import ApiContext


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service

    @router.get("/blobs/{storage_key:path}")
    def get_blob(storage_key: str) -> FileResponse:
        try:
            path = service.blobs.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Blob not found")
        return FileResponse(path)

    return router
