from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .context import ApiContext
from .gateway import harness_actor
from .models import (
    EditNoteBlockRequest,
    ReplaceImagesRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    service = context.service
    resolved = context.settings

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
                # Attachment.conversation_id belongs to the removed legacy
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

    return router
