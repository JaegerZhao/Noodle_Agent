from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..composition import Component
from ..db import (
    AtomicNote,
    AuditEvent,
    Job,
    NoteBlock,
    NoteVersion,
    Source,
    SourceNote,
    SourceNoteOutlineNode,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    SourceVersion,
    delete_fts_for_note,
    insert_fts,
    utcnow,
)
from ..domain import (
    JobStatus,
    KnowledgePlan,
    NoteBlockDraft,
    NoteDraft,
)
from .common import (
    _json,
    _parse_json,
)


class ReadingModule(Component):
    def backfill_source_notes(self) -> dict[str, int]:
        with self.owner.db.session() as session:
            source_rows = session.execute(
                select(Source, SourceVersion, Job)
                .join(SourceVersion, SourceVersion.id == Source.current_version_id)
                .join(Job, Job.source_version_id == SourceVersion.id)
                .where(
                    Job.status.in_(
                        [
                            JobStatus.COMPLETED.value,
                            JobStatus.COMPLETED_WITH_WARNINGS.value,
                        ]
                    ),
                    Job.parse_run_id.is_not(None),
                )
                .order_by(Source.id, Job.updated_at.desc())
            ).all()
            selected: dict[str, tuple[str, str, str]] = {}
            for source, source_version, job in source_rows:
                selected.setdefault(
                    source.id,
                    (source.title, source_version.id, job.id),
                )

        created = 0
        skipped = 0
        for source_id, (source_title, source_version_id, job_id) in selected.items():
            with self.owner.db.session() as session:
                existing = session.scalar(
                    select(SourceNote.id).where(SourceNote.source_id == source_id)
                )
                note_ids = list(
                    session.scalars(
                        select(AtomicNote.id).where(
                            AtomicNote.source_version_id == source_version_id,
                            AtomicNote.visibility == "ACTIVE",
                            AtomicNote.active_version_id.is_not(None),
                        )
                    ).all()
                )
            if existing or not note_ids:
                skipped += 1
                continue
            drafts: list[NoteDraft] = []
            for note_id in note_ids:
                note = self.owner.get_note(note_id)
                drafts.append(
                    NoteDraft(
                        stable_key=next(
                            item["stable_key"]
                            for item in self.owner._active_note_identities(source_version_id)
                            if item["note_id"] == note_id
                        ),
                        title=note["title"],
                        thesis=note["thesis"],
                        blocks=[
                            NoteBlockDraft(
                                block_type=block["type"],
                                text=block["text"],
                                evidence_block_ids=[],
                            )
                            for block in note["blocks"]
                        ],
                        tags=note["tags"],
                        source_sections=["知识要点"],
                    )
                )
            plan = KnowledgePlan(
                document_summary=f"{source_title} 的完整来源笔记。",
                notes=drafts,
            )
            outline, note_set = self.owner._fallback_source_note_plan(source_title, plan)
            self.owner._persist_source_note(
                job_id=job_id,
                source_id=source_id,
                source_version_id=source_version_id,
                outline=outline,
                note_set=note_set,
                passed_notes=drafts,
            )
            created += 1
        return {"created": created, "skipped": skipped}

    def _active_note_identities(self, source_version_id: str) -> list[dict[str, str]]:
        with self.owner.db.session() as session:
            rows = session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                    AtomicNote.active_version_id.is_not(None),
                )
            ).all()
            return [{"note_id": note.id, "stable_key": note.stable_key} for note in rows]

    def list_source_notes(self) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            rows = session.execute(
                select(SourceNote, SourceNoteVersion, Source)
                .join(SourceNoteVersion, SourceNoteVersion.id == SourceNote.active_version_id)
                .join(Source, Source.id == SourceNote.source_id)
                .where(SourceNote.visibility == "ACTIVE")
                .order_by(SourceNoteVersion.created_at.desc())
            ).all()
            return [
                {
                    "source_note_id": source_note.id,
                    "version_id": version.id,
                    "source_id": source.id,
                    "source_version_id": version.source_version_id,
                    "title": version.title,
                    "summary": version.summary,
                    "learning_goal": version.learning_goal,
                    "version_no": version.version_no,
                }
                for source_note, version, source in rows
            ]

    def get_source_note(self, source_note_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            source_note = session.get(SourceNote, source_note_id)
            if (
                source_note is None
                or source_note.visibility != "ACTIVE"
                or source_note.active_version_id is None
            ):
                raise ValueError(f"Unknown active source note: {source_note_id}")
            version = session.get(SourceNoteVersion, source_note.active_version_id)
            source = session.get(Source, source_note.source_id)
            source_version = session.get(SourceVersion, version.source_version_id)
            nodes = session.scalars(
                select(SourceNoteOutlineNode)
                .where(SourceNoteOutlineNode.source_note_version_id == version.id)
                .order_by(SourceNoteOutlineNode.order_index)
            ).all()
            placements = session.scalars(
                select(SourceNoteUnitPlacement)
                .where(SourceNoteUnitPlacement.source_note_version_id == version.id)
                .order_by(
                    SourceNoteUnitPlacement.outline_node_id,
                    SourceNoteUnitPlacement.order_index,
                )
            ).all()
            placement_data = [
                (
                    item.outline_node_id,
                    item.note_id,
                    item.note_version_id,
                    item.order_index,
                )
                for item in placements
            ]
            payload = {
                "source_note_id": source_note.id,
                "version_id": version.id,
                "source_id": source.id,
                "source_version_id": source_version.id,
                "source_file": source_version.original_name,
                "source_media_type": source_version.media_type,
                "source_kind": "PDF"
                if source_version.media_type == "application/pdf"
                else "TEXTBOOK",
                "original_view_available": source_version.media_type == "application/pdf",
                "title": version.title,
                "summary": version.summary,
                "learning_goal": version.learning_goal,
                "version_no": version.version_no,
            }

        by_id: dict[str, dict[str, Any]] = {
            node.id: {
                "outline_node_id": node.id,
                "stable_key": node.stable_key,
                "title": node.title,
                "level": node.level,
                "learning_goal": node.learning_goal,
                "source_sections": _parse_json(node.source_sections_json, []),
                "children": [],
                "notes": [],
                "_parent_id": node.parent_id,
                "_order": node.order_index,
            }
            for node in nodes
        }
        navigation_anchors: list[dict[str, Any]] = []
        navigation_keys: set[tuple[str, str, str, int, str]] = set()

        def add_navigation_anchor(
            *,
            outline_node_id: str,
            note_id: str,
            block_key: str | None,
            page_index: int | None,
            page_label: str | None,
            anchor_kind: str,
            evidence_role: str = "PRIMARY",
        ) -> None:
            if page_index is None or page_index < 0:
                return
            key = (
                outline_node_id,
                note_id,
                block_key or "",
                page_index,
                anchor_kind,
            )
            if key in navigation_keys:
                return
            navigation_keys.add(key)
            navigation_anchors.append(
                {
                    "outline_node_id": outline_node_id,
                    "note_id": note_id,
                    "block_key": block_key,
                    "page_index": page_index,
                    "page_label": str(page_label or page_index + 1),
                    "anchor_kind": anchor_kind,
                    "evidence_role": evidence_role,
                    "confidence": "EXACT" if block_key else "NOTE",
                }
            )

        for outline_node_id, note_id, note_version_id, order_index in placement_data:
            if outline_node_id in by_id:
                note = self.owner.get_note(note_id, version_id=note_version_id)
                note["placement_order"] = order_index
                by_id[outline_node_id]["notes"].append(note)
                for block in note.get("blocks", []):
                    block_key = str(block.get("block_key") or "") or None
                    for evidence in block.get("evidence", []):
                        add_navigation_anchor(
                            outline_node_id=outline_node_id,
                            note_id=note_id,
                            block_key=block_key,
                            page_index=evidence.get("page_index"),
                            page_label=evidence.get("page_label"),
                            anchor_kind="EVIDENCE",
                            evidence_role=str(evidence.get("role") or "PRIMARY"),
                        )
                    for asset in block.get("assets", []):
                        add_navigation_anchor(
                            outline_node_id=outline_node_id,
                            note_id=note_id,
                            block_key=block_key,
                            page_index=asset.get("page_index"),
                            page_label=asset.get("page_label"),
                            anchor_kind="ASSET",
                        )
                for evidence in note.get("evidence", []):
                    if evidence.get("block_key"):
                        continue
                    add_navigation_anchor(
                        outline_node_id=outline_node_id,
                        note_id=note_id,
                        block_key=None,
                        page_index=evidence.get("page_index"),
                        page_label=evidence.get("page_label"),
                        anchor_kind="EVIDENCE",
                        evidence_role=str(evidence.get("role") or "PRIMARY"),
                    )
                for asset in note.get("assets", []):
                    add_navigation_anchor(
                        outline_node_id=outline_node_id,
                        note_id=note_id,
                        block_key=None,
                        page_index=asset.get("page_index"),
                        page_label=asset.get("page_label"),
                        anchor_kind="ASSET",
                    )
        roots: list[dict[str, Any]] = []
        for node in sorted(by_id.values(), key=lambda item: item["_order"]):
            parent_id = node.pop("_parent_id")
            node.pop("_order")
            node["notes"].sort(key=lambda item: item["placement_order"])
            if parent_id and parent_id in by_id:
                by_id[parent_id]["children"].append(node)
            else:
                roots.append(node)
        payload["outline"] = roots
        payload["unit_count"] = len(placement_data)
        payload["navigation_anchors"] = sorted(
            navigation_anchors,
            key=lambda item: (
                item["page_index"],
                item["outline_node_id"],
                item["note_id"],
                item.get("block_key") or "",
                0 if item["anchor_kind"] == "EVIDENCE" else 1,
            ),
        )
        return payload

    def list_notes(self) -> list[dict[str, Any]]:
        with self.owner.db.session() as session:
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(AtomicNote.visibility == "ACTIVE")
                .order_by(NoteVersion.created_at.desc())
            ).all()
            return [
                {
                    "note_id": note.id,
                    "version_id": version.id,
                    "title": version.title,
                    "thesis": version.thesis,
                    "quality": version.quality_status,
                    "review": version.review_status,
                    "version_no": version.version_no,
                }
                for note, version in rows
            ]

    def get_note(self, note_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        with self.owner.db.session() as session:
            note = session.get(AtomicNote, note_id)
            if (
                note is None
                or note.active_version_id is None
                or (not version_id and note.visibility != "ACTIVE")
            ):
                raise ValueError(f"Unknown active note: {note_id}")
            resolved_version_id = version_id or note.active_version_id
            version = session.get(NoteVersion, resolved_version_id)
            if version is None or version.note_id != note.id:
                raise ValueError(f"Unknown note version: {resolved_version_id}")
            blocks = session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == version.id)
                .order_by(NoteBlock.order_index)
            ).all()
            evidence = self.owner._evidence_for_version(session, version.id)
            assets = self.owner._assets_for_version(session, version.id)
            block_keys = {block.id: block.block_key for block in blocks}
            evidence = [
                {
                    **item,
                    "block_key": block_keys.get(str(item.get("note_block_id") or "")),
                }
                for item in evidence
            ]
            evidence_by_block: dict[str, list[dict[str, Any]]] = {}
            for item in evidence:
                block_id = str(item.get("note_block_id") or "")
                if block_id:
                    evidence_by_block.setdefault(block_id, []).append(item)
            assets_by_block: dict[str, list[dict[str, Any]]] = {}
            for asset in assets:
                anchor = str(asset.get("note_block_id") or "")
                if anchor:
                    assets_by_block.setdefault(anchor, []).append(asset)
            return {
                "note_id": note.id,
                "version_id": version.id,
                "title": version.title,
                "thesis": version.thesis,
                "tags": _parse_json(version.tags_json, []),
                "quality": version.quality_status,
                "review": version.review_status,
                "blocks": [
                    {
                        **_parse_json(block.content_json, {}),
                        "block_key": block.block_key,
                        "type": block.block_type,
                        "text": block.content_text,
                        "origin": block.origin,
                        "evidence": evidence_by_block.get(block.id, []),
                        "assets": assets_by_block.get(block.id, []),
                    }
                    for block in blocks
                ],
                "evidence": evidence,
                "assets": [item for item in assets if not item.get("note_block_id")],
            }

    def reconcile_source_note_release(self, source_note_id: str) -> dict[str, int]:
        """Restore the active atomic-note set from deterministic placements.

        SourceNoteVersion placements are the release manifest.  This operation
        is intentionally separate from note persistence so a targeted block or
        asset repair cannot accidentally publish a two-note partial release.
        """
        with self.owner.db.session() as session:
            source_note = session.get(SourceNote, source_note_id)
            if source_note is None or source_note.active_version_id is None:
                raise ValueError(f"Unknown or inactive source_note_id: {source_note_id}")
            source_version = session.get(SourceNoteVersion, source_note.active_version_id)
            placements = session.scalars(
                select(SourceNoteUnitPlacement).where(
                    SourceNoteUnitPlacement.source_note_version_id == source_version.id
                )
            ).all()
            release_note_ids = {item.note_id for item in placements}
            restored = 0
            for placement in placements:
                note = session.get(AtomicNote, placement.note_id)
                version = session.get(NoteVersion, placement.note_version_id)
                if note is None or version is None:
                    continue
                if note.visibility != "ACTIVE" or note.active_version_id != version.id:
                    restored += 1
                note.visibility = "ACTIVE"
                note.active_version_id = version.id
                note.updated_at = utcnow()
                body = "\n\n".join(
                    session.scalars(
                        select(NoteBlock.content_text)
                        .where(NoteBlock.note_version_id == version.id)
                        .order_by(NoteBlock.order_index)
                    ).all()
                )
                delete_fts_for_note(session, note.id)
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=note.id,
                    title=version.title,
                    thesis=version.thesis,
                    body=body,
                    tags=" ".join(_parse_json(version.tags_json, [])),
                )
            hidden = 0
            other_notes = session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_version_id == source_version.source_version_id,
                    AtomicNote.id.not_in(release_note_ids),
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            for note in other_notes:
                note.visibility = "HIDDEN"
                note.updated_at = utcnow()
                delete_fts_for_note(session, note.id)
                hidden += 1
            session.add(
                AuditEvent(
                    actor="reconcile",
                    action="RECONCILE_SOURCE_NOTE_RELEASE",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=source_version.id,
                    detail_json=_json(
                        {
                            "release_note_count": len(release_note_ids),
                            "restored_note_count": restored,
                            "hidden_non_release_count": hidden,
                        }
                    ),
                )
            )
            return {
                "release_note_count": len(release_note_ids),
                "restored_note_count": restored,
                "hidden_non_release_count": hidden,
            }
