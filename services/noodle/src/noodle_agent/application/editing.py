from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..composition import Component
from ..db import (
    AtomicNote,
    Attachment,
    AuditEvent,
    BlobObject,
    Claim,
    ClaimEvidence,
    DocumentBlock,
    NoteAsset,
    NoteBlock,
    NoteEmbedding,
    NoteVersion,
    Source,
    SourceNote,
    SourceNoteOutlineNode,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    SourcePage,
    SourceVersion,
    VisualAsset,
    delete_fts_for_note,
    insert_fts,
    utcnow,
)
from ..domain import (
    BlockOrigin,
    QualityStatus,
    ReviewStatus,
)
from .common import (
    _json,
    _parse_json,
    _sha256_text,
)


class EditingModule(Component):
    @staticmethod
    def _numeric_page_label(value: str) -> int | None:
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None

    @staticmethod
    def _select_edit_target(
        affected: list[tuple[AtomicNote, NoteVersion]],
        *,
        target_note_title: str | None,
        vision_text: str,
    ) -> tuple[AtomicNote, NoteVersion]:
        if target_note_title:
            expected = target_note_title.casefold().strip()
            exact = [item for item in affected if item[1].title.casefold().strip() == expected]
            if exact:
                return exact[0]
            partial = [
                item
                for item in affected
                if expected in item[1].title.casefold() or item[1].title.casefold() in expected
            ]
            if len(partial) == 1:
                return partial[0]
            raise ValueError(f"Target atomic note is ambiguous: {target_note_title}")

        def score(item: tuple[AtomicNote, NoteVersion]) -> tuple[int, int, str]:
            title = item[1].title
            semantic = sum(
                weight
                for token, weight in (("示例", 8), ("计算", 5), ("案例", 4), ("图", 1))
                if token in title and (not vision_text or token in vision_text)
            )
            overlap = sum(
                1 for character in set(title) if character.strip() and character in vision_text
            )
            return semantic, overlap, title

        return max(affected, key=score)

    def _clone_note_for_image_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        removed_visual_ids: set[str],
        replacement_visual: VisualAsset,
        add_replacement: bool,
        attachment: Attachment,
        caption: str,
        purpose: str,
        actor: str,
    ) -> NoteVersion:
        old_blocks = list(
            session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == parent.id)
                .order_by(NoteBlock.order_index)
            ).all()
        )
        old_assets = list(
            session.scalars(
                select(NoteAsset)
                .where(NoteAsset.note_version_id == parent.id)
                .order_by(NoteAsset.order_index)
            ).all()
        )
        assets_by_block: dict[str, list[NoteAsset]] = {}
        for link in old_assets:
            if link.note_block_id:
                assets_by_block.setdefault(link.note_block_id, []).append(link)
        dropped_block_ids = {
            block.id
            for block in old_blocks
            if block.block_type == "IMAGE"
            and (
                (
                    assets_by_block.get(block.id)
                    and all(
                        link.visual_asset_id in removed_visual_ids
                        for link in assets_by_block[block.id]
                    )
                )
                or (add_replacement and not assets_by_block.get(block.id))
            )
        }
        removed_orders = [
            block.order_index for block in old_blocks if block.id in dropped_block_ids
        ]
        replacement_order = min(removed_orders, default=len(old_blocks))
        kept_blocks = [block for block in old_blocks if block.id not in dropped_block_ids]
        remaining_assets = [
            link for link in old_assets if link.visual_asset_id not in removed_visual_ids
        ]
        signature = {
            "parent": parent.content_sha256,
            "blocks": [
                {
                    "key": block.block_key,
                    "type": block.block_type,
                    "text": block.content_text,
                    "json": _parse_json(block.content_json, {}),
                }
                for block in kept_blocks
            ],
            "assets": sorted(link.visual_asset_id for link in remaining_assets),
            "replacement": replacement_visual.id if add_replacement else None,
            "attachment": attachment.id,
        }
        content_hash = _sha256_text(_json(signature))
        version_no = (
            session.scalar(
                select(func.max(NoteVersion.version_no)).where(NoteVersion.note_id == atomic.id)
            )
            or 0
        ) + 1
        version = NoteVersion(
            note_id=atomic.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            thesis=parent.thesis,
            tags_json=parent.tags_json,
            quality_status=QualityStatus.AUTO_VALIDATED.value,
            review_status=ReviewStatus.UNREVIEWED.value,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()

        sequence: list[tuple[int, NoteBlock | None]] = [
            (block.order_index * 2, block) for block in kept_blocks
        ]
        if add_replacement:
            sequence.append((replacement_order * 2, None))
        sequence.sort(key=lambda item: (item[0], item[1] is not None))
        block_map: dict[str, NoteBlock] = {}
        replacement_block: NoteBlock | None = None
        for order_index, (_sort_key, old_block) in enumerate(sequence):
            if old_block is None:
                replacement_block = NoteBlock(
                    note_version_id=version.id,
                    block_key=f"b-user-image-{attachment.id[:12]}",
                    previous_block_id=next(
                        (
                            block.id
                            for block in old_blocks
                            if block.order_index == replacement_order
                        ),
                        None,
                    ),
                    block_type="IMAGE",
                    order_index=order_index,
                    content_text="",
                    content_json=_json({"caption": caption, "purpose": purpose}),
                    content_hash=_sha256_text(
                        _json({"type": "IMAGE", "visual_asset_id": replacement_visual.id})
                    ),
                    origin=BlockOrigin.HUMAN_REVISED.value,
                )
                session.add(replacement_block)
                session.flush()
                continue
            cloned = NoteBlock(
                note_version_id=version.id,
                block_key=old_block.block_key,
                previous_block_id=old_block.id,
                block_type=old_block.block_type,
                order_index=order_index,
                content_text=old_block.content_text,
                content_json=old_block.content_json,
                content_hash=old_block.content_hash,
                origin=old_block.origin,
            )
            session.add(cloned)
            session.flush()
            block_map[old_block.id] = cloned
        for old_block in kept_blocks:
            if old_block.parent_block_id and old_block.parent_block_id in block_map:
                block_map[old_block.id].parent_block_id = block_map[old_block.parent_block_id].id

        old_claims = list(
            session.scalars(
                select(Claim).where(Claim.note_version_id == parent.id).order_by(Claim.order_index)
            ).all()
        )
        for old_claim in old_claims:
            cloned_block = block_map.get(old_claim.note_block_id)
            if cloned_block is None:
                continue
            new_claim = Claim(
                note_version_id=version.id,
                note_block_id=cloned_block.id,
                order_index=old_claim.order_index,
                claim_text=old_claim.claim_text,
                claim_type=old_claim.claim_type,
                support_status=old_claim.support_status,
            )
            session.add(new_claim)
            session.flush()
            span_ids = session.scalars(
                select(ClaimEvidence.span_id).where(ClaimEvidence.claim_id == old_claim.id)
            ).all()
            for span_id in span_ids:
                session.add(ClaimEvidence(claim_id=new_claim.id, span_id=span_id))

        for order_index, old_link in enumerate(remaining_assets):
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=old_link.visual_asset_id,
                    note_block_id=(
                        block_map[old_link.note_block_id].id
                        if old_link.note_block_id in block_map
                        else None
                    ),
                    order_index=order_index,
                    placement=old_link.placement,
                    purpose=old_link.purpose,
                    alt_text=old_link.alt_text,
                )
            )
        if add_replacement and replacement_block is not None:
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=replacement_visual.id,
                    note_block_id=replacement_block.id,
                    order_index=len(remaining_assets),
                    placement="INLINE",
                    purpose=purpose,
                    alt_text=caption,
                )
            )

        old_embeddings = session.scalars(
            select(NoteEmbedding).where(NoteEmbedding.note_version_id == parent.id)
        ).all()
        for embedding in old_embeddings:
            session.add(
                NoteEmbedding(
                    note_version_id=version.id,
                    model_name=embedding.model_name,
                    dimension=embedding.dimension,
                    vector_json=embedding.vector_json,
                    content_hash=content_hash,
                    ready=embedding.ready,
                )
            )
        body = "\n\n".join(block.content_text for block in kept_blocks if block.content_text)
        delete_fts_for_note(session, atomic.id)
        insert_fts(
            session,
            note_version_id=version.id,
            note_id=atomic.id,
            title=version.title,
            thesis=version.thesis,
            body=body,
            tags=" ".join(_parse_json(version.tags_json, [])),
        )
        atomic.active_version_id = version.id
        atomic.updated_at = utcnow()
        return version

    def _clone_note_for_block_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        block_key: str,
        new_text: str,
        actor: str,
    ) -> NoteVersion:
        """Create an immutable note version with one human-revised text block.

        Claims on untouched blocks retain their evidence.  The edited block is
        deliberately detached from its previous machine claim because the
        original quote can no longer prove user-authored wording.
        """
        old_blocks = list(
            session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == parent.id)
                .order_by(NoteBlock.order_index)
            ).all()
        )
        target = next((block for block in old_blocks if block.block_key == block_key), None)
        if target is None:
            raise ValueError(f"Unknown note block: {block_key}")
        if target.block_type == "IMAGE":
            raise ValueError("Image blocks must be changed with the image replacement operation")
        if target.content_text == new_text:
            return parent

        signature = {
            "parent": parent.content_sha256,
            "edit": {"block_key": block_key, "text": new_text},
        }
        content_hash = _sha256_text(_json(signature))
        version_no = (
            session.scalar(
                select(func.max(NoteVersion.version_no)).where(NoteVersion.note_id == atomic.id)
            )
            or 0
        ) + 1
        version = NoteVersion(
            note_id=atomic.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            thesis=parent.thesis,
            tags_json=parent.tags_json,
            quality_status=QualityStatus.AUTO_VALIDATED.value,
            review_status=ReviewStatus.UNREVIEWED.value,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()

        block_map: dict[str, NoteBlock] = {}
        for old_block in old_blocks:
            edited = old_block.id == target.id
            text_content = new_text if edited else old_block.content_text
            cloned = NoteBlock(
                note_version_id=version.id,
                block_key=old_block.block_key,
                previous_block_id=old_block.id,
                block_type=old_block.block_type,
                order_index=old_block.order_index,
                content_text=text_content,
                content_json=old_block.content_json,
                content_hash=_sha256_text(
                    _json(
                        {
                            "type": old_block.block_type,
                            "text": text_content,
                            "json": _parse_json(old_block.content_json, {}),
                        }
                    )
                ),
                origin=(BlockOrigin.HUMAN_REVISED.value if edited else old_block.origin),
            )
            session.add(cloned)
            session.flush()
            block_map[old_block.id] = cloned
        for old_block in old_blocks:
            if old_block.parent_block_id and old_block.parent_block_id in block_map:
                block_map[old_block.id].parent_block_id = block_map[old_block.parent_block_id].id

        old_claims = list(
            session.scalars(
                select(Claim).where(Claim.note_version_id == parent.id).order_by(Claim.order_index)
            ).all()
        )
        for old_claim in old_claims:
            if old_claim.note_block_id == target.id:
                continue
            cloned_block = block_map.get(old_claim.note_block_id)
            if cloned_block is None:
                continue
            new_claim = Claim(
                note_version_id=version.id,
                note_block_id=cloned_block.id,
                order_index=old_claim.order_index,
                claim_text=old_claim.claim_text,
                claim_type=old_claim.claim_type,
                support_status=old_claim.support_status,
            )
            session.add(new_claim)
            session.flush()
            span_ids = session.scalars(
                select(ClaimEvidence.span_id).where(ClaimEvidence.claim_id == old_claim.id)
            ).all()
            for span_id in span_ids:
                session.add(ClaimEvidence(claim_id=new_claim.id, span_id=span_id))

        old_assets = list(
            session.scalars(
                select(NoteAsset)
                .where(NoteAsset.note_version_id == parent.id)
                .order_by(NoteAsset.order_index)
            ).all()
        )
        for old_link in old_assets:
            session.add(
                NoteAsset(
                    note_version_id=version.id,
                    visual_asset_id=old_link.visual_asset_id,
                    note_block_id=(
                        block_map[old_link.note_block_id].id
                        if old_link.note_block_id in block_map
                        else None
                    ),
                    order_index=old_link.order_index,
                    placement=old_link.placement,
                    purpose=old_link.purpose,
                    alt_text=old_link.alt_text,
                )
            )

        body = "\n\n".join(
            new_text if block.id == target.id else block.content_text
            for block in old_blocks
            if (new_text if block.id == target.id else block.content_text)
        )
        vector = self.owner.embedding.embed([f"{version.title}\n{version.thesis}\n{body}"])[0]
        session.add(
            NoteEmbedding(
                note_version_id=version.id,
                model_name=self.owner.embedding.model_name,
                dimension=len(vector),
                vector_json=_json(vector),
                content_hash=content_hash,
            )
        )
        delete_fts_for_note(session, atomic.id)
        insert_fts(
            session,
            note_version_id=version.id,
            note_id=atomic.id,
            title=version.title,
            thesis=version.thesis,
            body=body,
            tags=" ".join(_parse_json(version.tags_json, [])),
        )
        atomic.active_version_id = version.id
        atomic.updated_at = utcnow()
        return version

    def edit_note_block(
        self,
        note_id: str,
        *,
        expected_version_id: str,
        block_key: str,
        new_text: str,
        actor: str = "user-via-harness",
    ) -> dict[str, Any]:
        """Patch one block using optimistic locking and publish a new release."""
        normalized_text = new_text.strip()
        if not normalized_text:
            raise ValueError("Edited block text must not be blank")
        with self.owner.db.session() as session:
            atomic = session.get(AtomicNote, note_id)
            if atomic is None or atomic.active_version_id is None:
                raise ValueError(f"Unknown active note: {note_id}")
            if atomic.active_version_id != expected_version_id:
                raise ValueError(
                    "Note version conflict: refresh the note before editing "
                    f"(expected {expected_version_id}, active {atomic.active_version_id})"
                )
            parent = session.get(NoteVersion, atomic.active_version_id)
            if parent is None:
                raise ValueError(f"Unknown note version: {atomic.active_version_id}")
            source_note_version = session.scalar(
                select(SourceNoteVersion)
                .join(SourceNote, SourceNote.active_version_id == SourceNoteVersion.id)
                .join(
                    SourceNoteUnitPlacement,
                    SourceNoteUnitPlacement.source_note_version_id == SourceNoteVersion.id,
                )
                .where(
                    SourceNoteUnitPlacement.note_id == atomic.id,
                    SourceNoteUnitPlacement.note_version_id == parent.id,
                )
            )
            if source_note_version is None:
                raise ValueError("Atomic note is not part of an active SourceNote release")
            source_note = session.get(SourceNote, source_note_version.source_note_id)
            if source_note is None:
                raise ValueError("SourceNote release owner is unavailable")

            version = self.owner._clone_note_for_block_edit(
                session,
                atomic=atomic,
                parent=parent,
                block_key=block_key,
                new_text=normalized_text,
                actor=actor,
            )
            if version.id == parent.id:
                return {
                    "status": "UNCHANGED",
                    "note_id": atomic.id,
                    "previous_version_id": parent.id,
                    "version_id": parent.id,
                    "version_no": parent.version_no,
                    "source_note_id": source_note.id,
                    "source_note_version_id": source_note_version.id,
                    "block_key": block_key,
                    "origin": "UNCHANGED",
                }
            new_source_version = self.owner._clone_source_note_release(
                session,
                source_note=source_note,
                parent=source_note_version,
                actor=actor,
                edit_signature={
                    "operation": "EDIT_NOTE_BLOCK",
                    "note_id": atomic.id,
                    "block_key": block_key,
                    "note_version_id": version.id,
                },
            )
            if self.owner.settings.knowledge_auto_sync:
                event_id = self.owner.knowledge_sync.enqueue(
                    session,
                    atomic.source_id,
                    "NOTE_UPDATED",
                    {
                        "note_id": atomic.id,
                        "previous_version_id": parent.id,
                        "version_id": version.id,
                    },
                    key=f"note-edit:{version.id}",
                )
                self.owner.knowledge_sync.invalidate(session, [parent.id], event_id)
            session.add(
                AuditEvent(
                    actor=actor,
                    action="EDIT_NOTE_BLOCK",
                    risk_level="L1",
                    target_type="NoteVersion",
                    target_id=version.id,
                    detail_json=_json(
                        {
                            "note_id": atomic.id,
                            "block_key": block_key,
                            "parent_version_id": parent.id,
                            "source_note_version_id": new_source_version.id,
                        }
                    ),
                )
            )
            return {
                "status": "UPDATED",
                "note_id": atomic.id,
                "previous_version_id": parent.id,
                "version_id": version.id,
                "version_no": version.version_no,
                "source_note_id": source_note.id,
                "source_note_version_id": new_source_version.id,
                "block_key": block_key,
                "origin": BlockOrigin.HUMAN_REVISED.value,
            }

    @staticmethod
    def _clone_source_note_release(
        session: Session,
        *,
        source_note: SourceNote,
        parent: SourceNoteVersion,
        actor: str,
        edit_signature: dict[str, Any],
    ) -> SourceNoteVersion:
        old_nodes = list(
            session.scalars(
                select(SourceNoteOutlineNode)
                .where(SourceNoteOutlineNode.source_note_version_id == parent.id)
                .order_by(SourceNoteOutlineNode.order_index)
            ).all()
        )
        old_placements = list(
            session.scalars(
                select(SourceNoteUnitPlacement)
                .where(SourceNoteUnitPlacement.source_note_version_id == parent.id)
                .order_by(
                    SourceNoteUnitPlacement.outline_node_id,
                    SourceNoteUnitPlacement.order_index,
                )
            ).all()
        )
        active_versions: dict[str, str] = {}
        for placement in old_placements:
            atomic = session.get(AtomicNote, placement.note_id)
            if atomic is None or atomic.active_version_id is None:
                raise ValueError(f"Atomic note is not active: {placement.note_id}")
            active_versions[placement.note_id] = atomic.active_version_id
        content_hash = _sha256_text(
            _json(
                {
                    "parent": parent.content_sha256,
                    "placements": active_versions,
                    "edit": edit_signature,
                }
            )
        )
        version_no = (
            session.scalar(
                select(func.max(SourceNoteVersion.version_no)).where(
                    SourceNoteVersion.source_note_id == source_note.id
                )
            )
            or 0
        ) + 1
        version = SourceNoteVersion(
            source_note_id=source_note.id,
            source_version_id=parent.source_version_id,
            parse_run_id=parent.parse_run_id,
            version_no=version_no,
            parent_version_id=parent.id,
            title=parent.title,
            summary=parent.summary,
            learning_goal=parent.learning_goal,
            content_sha256=content_hash,
            created_by=actor,
        )
        session.add(version)
        session.flush()
        node_map: dict[str, SourceNoteOutlineNode] = {}
        for old_node in sorted(old_nodes, key=lambda item: (item.level, item.order_index)):
            cloned = SourceNoteOutlineNode(
                source_note_version_id=version.id,
                stable_key=old_node.stable_key,
                parent_id=(
                    node_map[old_node.parent_id].id if old_node.parent_id in node_map else None
                ),
                title=old_node.title,
                level=old_node.level,
                order_index=old_node.order_index,
                learning_goal=old_node.learning_goal,
                source_sections_json=old_node.source_sections_json,
            )
            session.add(cloned)
            session.flush()
            node_map[old_node.id] = cloned
        for old_placement in old_placements:
            session.add(
                SourceNoteUnitPlacement(
                    source_note_version_id=version.id,
                    outline_node_id=node_map[old_placement.outline_node_id].id,
                    note_id=old_placement.note_id,
                    note_version_id=active_versions[old_placement.note_id],
                    order_index=old_placement.order_index,
                )
            )
        source_note.active_version_id = version.id
        source_note.updated_at = utcnow()
        return version

    def replace_source_note_images(
        self,
        *,
        attachment_id: str,
        page_start: int,
        page_end: int,
        source_note_id: str | None = None,
        source_ref: str | None = None,
        target_note_title: str | None = None,
        caption: str | None = None,
        purpose: str | None = None,
        expected_source_note_version_id: str | None = None,
        actor: str = "user-via-agent",
    ) -> dict[str, Any]:
        if page_start < 1 or page_end < page_start:
            raise ValueError("Invalid replacement page range")
        with self.owner.db.session() as session:
            if source_note_id:
                source_note = session.get(SourceNote, source_note_id)
                if source_note is None or source_note.active_version_id is None:
                    raise ValueError(f"Unknown active source note: {source_note_id}")
                source_note_version = session.get(SourceNoteVersion, source_note.active_version_id)
                source_version = session.get(SourceVersion, source_note_version.source_version_id)
            else:
                expected = (source_ref or "").strip().casefold()
                if not expected:
                    raise ValueError("source_note_id or source_ref is required")
                candidates = session.execute(
                    select(SourceNote, SourceNoteVersion, SourceVersion, Source)
                    .join(SourceNoteVersion, SourceNoteVersion.id == SourceNote.active_version_id)
                    .join(SourceVersion, SourceVersion.id == SourceNoteVersion.source_version_id)
                    .join(Source, Source.id == SourceNote.source_id)
                    .where(SourceNote.visibility == "ACTIVE")
                ).all()
                exact = [
                    row
                    for row in candidates
                    if row[2].original_name.casefold() == expected
                    or row[3].title.casefold() == expected
                ]
                partial = [
                    row
                    for row in candidates
                    if expected in row[2].original_name.casefold()
                    or expected in row[3].title.casefold()
                    or row[2].original_name.casefold() in expected
                ]
                matches = exact or partial
                if len(matches) != 1:
                    raise ValueError(f"Source reference is unknown or ambiguous: {source_ref}")
                source_note, source_note_version, source_version, _source = matches[0]

            if (
                expected_source_note_version_id
                and source_note_version.id != expected_source_note_version_id
            ):
                raise ValueError(
                    "SourceNote version conflict: refresh before replacing images "
                    f"(expected {expected_source_note_version_id}, "
                    f"active {source_note_version.id})"
                )

            attachment = session.get(Attachment, attachment_id)
            if attachment is None:
                raise ValueError(f"Unknown agent attachment: {attachment_id}")
            attachment_blob = session.get(BlobObject, attachment.blob_id)
            if attachment_blob is None or not attachment_blob.media_type.startswith("image/"):
                raise ValueError("Replacement attachment is not an image")
            parse_run_id = source_note_version.parse_run_id
            synthetic_external_id = f"user-attachment-{attachment.id}"
            synthetic_block = session.scalar(
                select(DocumentBlock).where(
                    DocumentBlock.parse_run_id == parse_run_id,
                    DocumentBlock.external_block_id == synthetic_external_id,
                )
            )
            replacement_visual = (
                session.scalar(
                    select(VisualAsset).where(VisualAsset.document_block_id == synthetic_block.id)
                )
                if synthetic_block is not None
                else None
            )
            if replacement_visual is not None:
                active_link = session.scalar(
                    select(NoteAsset)
                    .join(NoteVersion, NoteVersion.id == NoteAsset.note_version_id)
                    .join(AtomicNote, AtomicNote.active_version_id == NoteVersion.id)
                    .where(NoteAsset.visual_asset_id == replacement_visual.id)
                )
                if active_link is not None:
                    return {
                        "status": "ALREADY_APPLIED",
                        "source_note_id": source_note.id,
                        "source_note_version_id": source_note.active_version_id,
                        "source_title": source_note_version.title,
                        "source_file": source_version.original_name,
                        "page_start": page_start,
                        "page_end": page_end,
                        "attachment_id": attachment.id,
                        "visual_asset_id": replacement_visual.id,
                    }

            linked_rows = session.execute(
                select(AtomicNote, NoteVersion, NoteAsset, VisualAsset, SourcePage)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .join(NoteAsset, NoteAsset.note_version_id == NoteVersion.id)
                .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
                .join(SourcePage, SourcePage.id == VisualAsset.page_id)
                .where(
                    AtomicNote.source_version_id == source_version.id,
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            selected_rows = [
                row
                for row in linked_rows
                if (page := self.owner._numeric_page_label(row[4].page_label)) is not None
                and page_start <= page <= page_end
                and (
                    row[3].extraction_method != "USER_UPLOAD"
                    or (
                        target_note_title
                        and row[1].title.casefold().strip() == target_note_title.casefold().strip()
                    )
                )
            ]
            if not selected_rows:
                raise ValueError(f"No active note images found for pages {page_start}-{page_end}")
            affected_by_note: dict[str, tuple[AtomicNote, NoteVersion]] = {}
            removed_visual_ids: set[str] = set()
            for atomic, version, _link, visual, _page in selected_rows:
                affected_by_note.setdefault(atomic.id, (atomic, version))
                removed_visual_ids.add(visual.id)
            affected = list(affected_by_note.values())
            vision = _parse_json(attachment.vision_json, {})
            vision_text = " ".join(
                str(vision.get(key) or "") for key in ("caption", "teaching_role", "asset_type")
            )
            target_atomic, target_parent = self.owner._select_edit_target(
                affected,
                target_note_title=target_note_title,
                vision_text=vision_text,
            )
            resolved_caption = (
                (caption or "").strip()
                or str(vision.get("caption") or "").strip()
                or f"第 {page_start}–{page_end} 页的合并教学图示"
            )
            resolved_purpose = (
                (purpose or "").strip()
                or str(vision.get("teaching_role") or "").strip()
                or f"替代第 {page_start}–{page_end} 页的 MinerU 图片碎片"
            )
            pages = list(
                session.scalars(
                    select(SourcePage)
                    .where(SourcePage.parse_run_id == parse_run_id)
                    .order_by(SourcePage.page_index)
                ).all()
            )
            anchor_page = next(
                (
                    page
                    for page in pages
                    if self.owner._numeric_page_label(page.page_label) == page_start
                ),
                next((page for page in pages if page.page_index == page_start - 1), None),
            )
            if anchor_page is None:
                raise ValueError(f"Source page {page_start} does not exist")
            if synthetic_block is None:
                reading_order = (
                    session.scalar(
                        select(func.max(DocumentBlock.reading_order)).where(
                            DocumentBlock.parse_run_id == parse_run_id
                        )
                    )
                    or 0
                ) + 1
                synthetic_block = DocumentBlock(
                    parse_run_id=parse_run_id,
                    page_id=anchor_page.id,
                    external_block_id=synthetic_external_id,
                    block_type="IMAGE",
                    reading_order=reading_order,
                    text_content=resolved_caption,
                    metadata_json=_json(
                        {
                            "origin": "USER_UPLOAD",
                            "attachment_id": attachment.id,
                            "replaces_pages": [
                                str(page) for page in range(page_start, page_end + 1)
                            ],
                        }
                    ),
                )
                session.add(synthetic_block)
                session.flush()
            if replacement_visual is None:
                replacement_visual = VisualAsset(
                    parse_run_id=parse_run_id,
                    page_id=anchor_page.id,
                    document_block_id=synthetic_block.id,
                    blob_id=attachment_blob.id,
                    asset_type="FIGURE",
                    caption=resolved_caption,
                    extraction_method="USER_UPLOAD",
                    quality_status="USABLE",
                )
                session.add(replacement_visual)
                session.flush()

            updated_notes: list[dict[str, Any]] = []
            for atomic, parent_version in affected:
                new_version = self.owner._clone_note_for_image_edit(
                    session,
                    atomic=atomic,
                    parent=parent_version,
                    removed_visual_ids=removed_visual_ids,
                    replacement_visual=replacement_visual,
                    add_replacement=atomic.id == target_atomic.id,
                    attachment=attachment,
                    caption=resolved_caption,
                    purpose=resolved_purpose,
                    actor=actor,
                )
                updated_notes.append(
                    {
                        "note_id": atomic.id,
                        "title": new_version.title,
                        "previous_version_id": parent_version.id,
                        "version_id": new_version.id,
                        "version_no": new_version.version_no,
                        "contains_replacement": atomic.id == target_atomic.id,
                    }
                )
            new_source_version = self.owner._clone_source_note_release(
                session,
                source_note=source_note,
                parent=source_note_version,
                actor=actor,
                edit_signature={
                    "operation": "REPLACE_SOURCE_IMAGES",
                    "attachment_id": attachment.id,
                    "pages": [page_start, page_end],
                    "removed_visual_ids": sorted(removed_visual_ids),
                    "replacement_visual_id": replacement_visual.id,
                },
            )
            attachment.status = "CONSUMED"
            attachment.consumed_at = utcnow()
            session.add(
                AuditEvent(
                    actor=actor,
                    action="AGENT_REPLACE_SOURCE_IMAGES",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=new_source_version.id,
                    detail_json=_json(
                        {
                            "parent_source_note_version_id": source_note_version.id,
                            "attachment_id": attachment.id,
                            "page_start": page_start,
                            "page_end": page_end,
                            "removed_visual_count": len(removed_visual_ids),
                            "target_note_id": target_atomic.id,
                            "updated_notes": updated_notes,
                        }
                    ),
                )
            )
            return {
                "status": "UPDATED",
                "source_note_id": source_note.id,
                "source_note_version_id": new_source_version.id,
                "source_note_version_no": new_source_version.version_no,
                "source_title": new_source_version.title,
                "source_file": source_version.original_name,
                "page_start": page_start,
                "page_end": page_end,
                "removed_visual_count": len(removed_visual_ids),
                "attachment_id": attachment.id,
                "visual_asset_id": replacement_visual.id,
                "target_note_id": target_atomic.id,
                "target_note_title": target_parent.title,
                "updated_notes": updated_notes,
            }
