from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..composition import Component
from ..db import (
    Attachment,
    BlobObject,
)
from .common import (
    _json,
    _parse_json,
)


class AttachmentsModule(Component):
    @staticmethod
    def _attachment_payload(attachment: Attachment, blob: BlobObject) -> dict[str, Any]:
        return {
            "attachment_id": attachment.id,
            "conversation_id": attachment.conversation_id,
            "message_id": attachment.message_id,
            "original_name": attachment.original_name,
            "attachment_type": attachment.attachment_type,
            "status": attachment.status,
            "media_type": blob.media_type,
            "byte_size": blob.byte_size,
            "storage_key": blob.storage_key,
            "blob_url": f"/internal/v1/blobs/{blob.storage_key}",
            "vision": _parse_json(attachment.vision_json, {}),
        }

    def create_agent_attachment(
        self,
        image_path: Path,
        *,
        original_name: str | None = None,
        media_type: str | None = None,
        conversation_id: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        image_path = image_path.resolve()
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        resolved_media_type = media_type or mimetypes.guess_type(image_path.name)[0] or ""
        if resolved_media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Agent attachments currently accept PNG, JPEG, or WebP images")
        if image_path.stat().st_size > 15 * 1024 * 1024:
            raise ValueError("Agent image attachment exceeds the 15 MB limit")
        storage_key, sha256, byte_size = self.owner.blobs.put(image_path, resolved_media_type)
        with self.owner.db.session() as session:
            blob = session.scalar(
                select(BlobObject).where(
                    BlobObject.content_sha256 == sha256,
                    BlobObject.media_type == resolved_media_type,
                )
            )
            if blob is None:
                blob = BlobObject(
                    storage_key=storage_key,
                    content_sha256=sha256,
                    media_type=resolved_media_type,
                    byte_size=byte_size,
                )
                session.add(blob)
                session.flush()
            attachment = Attachment(
                conversation_id=conversation_id,
                blob_id=blob.id,
                original_name=Path(original_name or image_path.name).name,
                attachment_type="IMAGE",
                status="ANALYZING" if analyze and self.owner.vision.enabled else "READY",
            )
            session.add(attachment)
            session.flush()
            attachment_id = attachment.id

        if analyze and self.owner.vision.enabled:
            try:
                understanding = self.owner.vision.analyze(
                    image_path,
                    external_block_id=f"agent-attachment-{attachment_id}",
                    context={
                        "origin": "USER_UPLOAD",
                        "instruction": "概括图片内容、教学作用，并判断是否适合作为完整笔记插图。",
                    },
                )
                vision_payload = understanding.model_dump()
                attachment_status = "READY"
            except Exception as exc:  # noqa: BLE001
                vision_payload = {
                    "status": "FAILED",
                    "error_code": exc.__class__.__name__,
                    "error_detail": str(exc)[:1_000],
                }
                attachment_status = "READY_WITH_WARNING"
            with self.owner.db.session() as session:
                attachment = session.get(Attachment, attachment_id)
                attachment.vision_json = _json(vision_payload)
                attachment.status = attachment_status
        return self.owner.get_agent_attachments([attachment_id])[0]

    def get_agent_attachments(self, attachment_ids: list[str]) -> list[dict[str, Any]]:
        ordered_ids = list(dict.fromkeys(attachment_ids))
        if not ordered_ids:
            return []
        with self.owner.db.session() as session:
            rows = session.execute(
                select(Attachment, BlobObject)
                .join(BlobObject, BlobObject.id == Attachment.blob_id)
                .where(Attachment.id.in_(ordered_ids))
            ).all()
            by_id = {
                attachment.id: self.owner._attachment_payload(attachment, blob)
                for attachment, blob in rows
            }
        missing = [attachment_id for attachment_id in ordered_ids if attachment_id not in by_id]
        if missing:
            raise ValueError(f"Unknown agent attachment: {missing[0]}")
        return [by_id[attachment_id] for attachment_id in ordered_ids]

    def claim_agent_attachments(
        self,
        attachment_ids: list[str],
        *,
        conversation_id: str,
        message_id: str,
    ) -> None:
        if not attachment_ids:
            return
        with self.owner.db.session() as session:
            attachments = session.scalars(
                select(Attachment).where(Attachment.id.in_(attachment_ids))
            ).all()
            by_id = {attachment.id: attachment for attachment in attachments}
            for attachment_id in dict.fromkeys(attachment_ids):
                attachment = by_id.get(attachment_id)
                if attachment is None:
                    raise ValueError(f"Unknown agent attachment: {attachment_id}")
                if attachment.conversation_id not in {None, conversation_id}:
                    raise ValueError("Attachment belongs to another conversation")
                if attachment.status not in {"READY", "READY_WITH_WARNING", "CONSUMED"}:
                    raise ValueError(f"Attachment is not ready: {attachment.status}")
                attachment.conversation_id = conversation_id
                attachment.message_id = message_id
