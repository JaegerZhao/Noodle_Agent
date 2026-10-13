from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..composition import Component
from ..db import (
    AtomicNote,
    AuditEvent,
    Claim,
    ClaimEvidence,
    DocumentBlock,
    EvidenceSpan,
    Job,
    NoteAsset,
    NoteBlock,
    NoteCandidate,
    NoteEmbedding,
    NoteVersion,
    QualityEvaluationRecord,
    Source,
    SourceNote,
    SourceNoteOutlineNode,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    VisualAsset,
    delete_fts_for_note,
    insert_fts,
    utcnow,
)
from ..domain import (
    BlockOrigin,
    KnowledgePlan,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    QualityStatus,
    ReviewStatus,
    SourceNoteOutlinePlan,
)
from .common import (
    _json,
    _sha256_text,
)


class PersistenceModule(Component):
    def _record_candidate_results(
        self,
        knowledge_map_id: str,
        *,
        passed: list,
        failures: list[dict[str, Any]],
        repair_attempted_keys: set[str],
    ) -> None:
        failure_by_key = {str(item.get("candidate")): item for item in failures}
        passed_keys = {note.stable_key for note in passed}
        with self.owner.db.session() as session:
            candidates = session.scalars(
                select(NoteCandidate).where(NoteCandidate.knowledge_map_id == knowledge_map_id)
            ).all()
            for candidate in candidates:
                if candidate.stable_key in passed_keys:
                    candidate.validation_status = "PASSED"
                    candidate.failure_json = "[]"
                    candidate.payload_json = next(
                        note.model_dump_json()
                        for note in passed
                        if note.stable_key == candidate.stable_key
                    )
                elif candidate.stable_key in failure_by_key:
                    candidate.validation_status = "FAILED"
                    candidate.failure_json = _json(
                        failure_by_key[candidate.stable_key].get("detail", [])
                    )
                if candidate.stable_key in repair_attempted_keys:
                    candidate.repair_attempts = 1

    def _record_quality_evaluations(
        self,
        knowledge_map_id: str,
        evaluations: list[NoteEvaluation],
    ) -> None:
        with self.owner.db.session() as session:
            for evaluation in evaluations:
                if not evaluation.content_sha256:
                    continue
                existing = session.scalar(
                    select(QualityEvaluationRecord).where(
                        QualityEvaluationRecord.knowledge_map_id == knowledge_map_id,
                        QualityEvaluationRecord.stable_key == evaluation.stable_key,
                        QualityEvaluationRecord.content_sha256 == evaluation.content_sha256,
                        QualityEvaluationRecord.evaluator_model
                        == (evaluation.evaluator_model or self.owner.agent.name),
                        QualityEvaluationRecord.prompt_version
                        == (evaluation.prompt_version or "unknown"),
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    QualityEvaluationRecord(
                        knowledge_map_id=knowledge_map_id,
                        stable_key=evaluation.stable_key,
                        content_sha256=evaluation.content_sha256,
                        evaluator_model=evaluation.evaluator_model or self.owner.agent.name,
                        prompt_version=evaluation.prompt_version or "unknown",
                        passed=evaluation.passed,
                        issues_json=_json(evaluation.issues),
                    )
                )

    def _persist_source_note(
        self,
        *,
        job_id: str,
        source_id: str,
        source_version_id: str,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        passed_notes: list[NoteDraft],
    ) -> str:
        passed_keys = {note.stable_key for note in passed_notes}
        spec_by_key = {
            spec.stable_key: spec for spec in note_set.notes if spec.stable_key in passed_keys
        }
        original_nodes = {node.stable_key: node for node in outline.nodes}
        used_keys = {
            spec.outline_node_key
            for spec in spec_by_key.values()
            if spec.outline_node_key in original_nodes
        }
        retained_keys = set(used_keys)
        for key in list(used_keys):
            node = original_nodes[key]
            while node.parent_key and node.parent_key in original_nodes:
                retained_keys.add(node.parent_key)
                node = original_nodes[node.parent_key]
        retained_nodes = [node for node in outline.nodes if node.stable_key in retained_keys]
        if passed_notes and not retained_nodes:
            fallback_outline, fallback_set = self.owner._fallback_source_note_plan(
                outline.title,
                KnowledgePlan(
                    document_summary=outline.summary,
                    notes=passed_notes,
                ),
            )
            outline = fallback_outline
            original_nodes = {node.stable_key: node for node in outline.nodes}
            retained_nodes = outline.nodes
            spec_by_key = {spec.stable_key: spec for spec in fallback_set.notes}

        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            source_note = session.scalar(
                select(SourceNote).where(SourceNote.source_id == source_id)
            )
            if source_note is None:
                source_note = SourceNote(source_id=source_id)
                session.add(source_note)
                session.flush()

            note_rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(
                    AtomicNote.source_version_id == source_version_id,
                    AtomicNote.stable_key.in_(passed_keys),
                    AtomicNote.visibility == "ACTIVE",
                )
            ).all()
            note_by_key = {note.stable_key: (note, version) for note, version in note_rows}
            composition = [
                {
                    "outline_key": node.stable_key,
                    "parent_key": node.parent_key,
                    "level": node.level,
                    "title": node.title,
                    "note_versions": [
                        note_by_key[key][1].id
                        for key, spec in spec_by_key.items()
                        if spec.outline_node_key == node.stable_key and key in note_by_key
                    ],
                }
                for node in retained_nodes
            ]
            content_hash = _sha256_text(
                _json(
                    {
                        "title": outline.title,
                        "summary": outline.summary,
                        "composition": composition,
                    }
                )
            )
            active_version = (
                session.get(SourceNoteVersion, source_note.active_version_id)
                if source_note.active_version_id
                else None
            )
            if active_version and active_version.content_sha256 == content_hash:
                return source_note.id

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
                source_version_id=source_version_id,
                parse_run_id=job.parse_run_id,
                version_no=version_no,
                parent_version_id=source_note.active_version_id,
                title=outline.title,
                summary=outline.summary,
                learning_goal="",
                content_sha256=content_hash,
                created_by=self.owner.agent.name,
            )
            session.add(version)
            session.flush()

            persisted_nodes: dict[str, SourceNoteOutlineNode] = {}
            for order_index, node in sorted(
                enumerate(retained_nodes), key=lambda item: (item[1].level, item[0])
            ):
                parent = persisted_nodes.get(node.parent_key or "")
                record = SourceNoteOutlineNode(
                    source_note_version_id=version.id,
                    stable_key=node.stable_key,
                    parent_id=parent.id if parent else None,
                    title=node.title,
                    level=node.level,
                    order_index=order_index,
                    learning_goal="",
                    source_sections_json=_json(node.source_sections),
                )
                session.add(record)
                session.flush()
                persisted_nodes[node.stable_key] = record

            order_by_outline: dict[str, int] = {}
            for spec in note_set.notes:
                if spec.stable_key not in note_by_key or spec.stable_key not in spec_by_key:
                    continue
                outline_record = persisted_nodes.get(spec_by_key[spec.stable_key].outline_node_key)
                if outline_record is None:
                    continue
                note, note_version = note_by_key[spec.stable_key]
                placement_order = order_by_outline.get(outline_record.stable_key, 0)
                session.add(
                    SourceNoteUnitPlacement(
                        source_note_version_id=version.id,
                        outline_node_id=outline_record.id,
                        note_id=note.id,
                        note_version_id=note_version.id,
                        order_index=placement_order,
                    )
                )
                order_by_outline[outline_record.stable_key] = placement_order + 1

            source = session.get(Source, source_id)
            if source is None or source.visibility != "ACTIVE" or job.status == "CANCELLED":
                raise ValueError("Source was deleted while generation was running")
            old_version_ids = session.scalars(
                select(NoteVersion.id)
                .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                .where(
                    AtomicNote.source_id == source_id,
                    NoteVersion.source_version_id != source_version_id,
                )
            ).all()
            source.current_version_id = source_version_id
            for old_note in session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_id == source_id,
                    AtomicNote.source_version_id != source_version_id,
                    AtomicNote.visibility == "ACTIVE",
                )
            ):
                old_note.visibility = "SUPERSEDED_SOURCE"
                delete_fts_for_note(session, old_note.id)
            source_note.active_version_id = version.id
            source_note.visibility = "ACTIVE"
            source_note.updated_at = utcnow()
            if self.owner.settings.knowledge_auto_sync:
                event_id = self.owner.knowledge_sync.enqueue(
                    session,
                    source_id,
                    "SOURCE_PUBLISHED",
                    {"source_version_id": source_version_id, "source_note_version_id": version.id},
                    key=f"source-publish:{version.id}",
                )
                self.owner.knowledge_sync.invalidate(session, old_version_ids, event_id)
            session.add(
                AuditEvent(
                    actor=self.owner.agent.name,
                    action="ACTIVATE_SOURCE_NOTE_VERSION",
                    risk_level="L1",
                    target_type="SourceNoteVersion",
                    target_id=version.id,
                    detail_json=_json(
                        {
                            "outline_node_count": len(retained_nodes),
                            "unit_count": len(note_by_key),
                        }
                    ),
                )
            )
            return source_note.id

    @staticmethod
    def _reconcile_note_version_assets(
        session: Session,
        *,
        note_version: NoteVersion,
        parse_run_id: str,
        note: NoteDraft,
    ) -> int:
        blocks = session.scalars(
            select(NoteBlock)
            .where(NoteBlock.note_version_id == note_version.id)
            .order_by(NoteBlock.order_index)
        ).all()
        existing_assets = session.scalars(
            select(NoteAsset).where(NoteAsset.note_version_id == note_version.id)
        ).all()
        existing_visual_ids = {item.visual_asset_id for item in existing_assets}
        linked_blob_ids = {
            asset.blob_id
            for item in existing_assets
            if (asset := session.get(VisualAsset, item.visual_asset_id)) is not None
        }
        next_order = max((item.order_index for item in existing_assets), default=-1) + 1
        added = 0
        for block_order, draft_block in enumerate(note.blocks):
            if block_order >= len(blocks):
                break
            for external_block_id in dict.fromkeys(draft_block.asset_block_ids):
                visual_asset = session.scalar(
                    select(VisualAsset)
                    .join(DocumentBlock, DocumentBlock.id == VisualAsset.document_block_id)
                    .where(
                        VisualAsset.parse_run_id == parse_run_id,
                        DocumentBlock.external_block_id == external_block_id,
                    )
                )
                if (
                    visual_asset is None
                    or visual_asset.id in existing_visual_ids
                    or visual_asset.blob_id in linked_blob_ids
                ):
                    continue
                session.add(
                    NoteAsset(
                        note_version_id=note_version.id,
                        visual_asset_id=visual_asset.id,
                        note_block_id=blocks[block_order].id,
                        order_index=next_order,
                        placement="INLINE",
                        purpose=draft_block.purpose
                        or f"支持笔记“{note.title}”正文块 {block_order + 1} 的视觉解释或证据",
                        alt_text=draft_block.caption
                        or visual_asset.caption
                        or f"{note.title}的来源图片",
                    )
                )
                existing_visual_ids.add(visual_asset.id)
                linked_blob_ids.add(visual_asset.blob_id)
                next_order += 1
                added += 1
        return added

    def _persist_notes(
        self,
        job_id: str,
        source_id: str,
        source_version_id: str,
        notes: list,
        *,
        publish_release: bool = True,
    ) -> list[str]:
        activated: list[str] = []
        for note in notes:
            body = "\n\n".join(text for block in note.blocks if (text := block.plain_text()))
            content_hash = _sha256_text(
                _json(
                    {
                        "title": note.title,
                        "thesis": note.thesis,
                        "blocks": [
                            {
                                "type": block.block_type,
                                "content": block.content_payload(),
                                "evidence": block.evidence_block_ids,
                                "assets": block.asset_block_ids,
                            }
                            for block in note.blocks
                        ],
                        "tags": note.tags,
                    }
                )
            )
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                existing_version = session.scalar(
                    select(NoteVersion)
                    .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                    .where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.stable_key == note.stable_key,
                        NoteVersion.source_version_id == source_version_id,
                        NoteVersion.parse_run_id == job.parse_run_id,
                        NoteVersion.content_sha256 == content_hash,
                    )
                )
                if existing_version is not None:
                    atomic = session.get(AtomicNote, existing_version.note_id)
                    self.owner._reconcile_note_version_assets(
                        session,
                        note_version=existing_version,
                        parse_run_id=job.parse_run_id,
                        note=note,
                    )
                    delete_fts_for_note(session, atomic.id)
                    insert_fts(
                        session,
                        note_version_id=existing_version.id,
                        note_id=atomic.id,
                        title=note.title,
                        thesis=note.thesis,
                        body=body,
                        tags=" ".join(note.tags),
                    )
                    atomic.active_version_id = existing_version.id
                    atomic.visibility = "ACTIVE"
                    atomic.updated_at = utcnow()
                    activated.append(atomic.id)
                    continue

                atomic = session.scalar(
                    select(AtomicNote).where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.stable_key == note.stable_key,
                    )
                )
                if atomic is None:
                    atomic = AtomicNote(
                        source_id=source_id,
                        source_version_id=source_version_id,
                        stable_key=note.stable_key,
                    )
                    session.add(atomic)
                    session.flush()
                version_no = (
                    session.scalar(
                        select(func.max(NoteVersion.version_no)).where(
                            NoteVersion.note_id == atomic.id
                        )
                    )
                    or 0
                ) + 1
                version = NoteVersion(
                    note_id=atomic.id,
                    source_version_id=source_version_id,
                    parse_run_id=job.parse_run_id,
                    version_no=version_no,
                    parent_version_id=atomic.active_version_id,
                    title=note.title,
                    thesis=note.thesis,
                    tags_json=_json(note.tags),
                    quality_status=QualityStatus.AUTO_VALIDATED.value,
                    review_status=ReviewStatus.UNREVIEWED.value,
                    content_sha256=content_hash,
                    created_by=self.owner.agent.name,
                )
                session.add(version)
                session.flush()
                linked_asset_blob_ids: set[str] = set()
                asset_index = 0
                for order, draft_block in enumerate(note.blocks):
                    block_text = draft_block.plain_text()
                    block_payload = draft_block.content_payload()
                    block_hash_input = _json(
                        {"type": draft_block.block_type, "content": block_payload}
                    )
                    block = NoteBlock(
                        note_version_id=version.id,
                        block_key=f"b-{order}-{_sha256_text(block_hash_input)[:12]}",
                        block_type=draft_block.block_type,
                        order_index=order,
                        content_text=block_text,
                        content_json=_json(block_payload),
                        content_hash=_sha256_text(block_hash_input),
                        origin=BlockOrigin.AI_GENERATED.value,
                    )
                    session.add(block)
                    session.flush()
                    claim = None
                    if block_text:
                        claim = Claim(
                            note_version_id=version.id,
                            note_block_id=block.id,
                            order_index=order,
                            claim_text=block_text,
                            support_status="SUPPORTED",
                        )
                        session.add(claim)
                        session.flush()
                    for external_block_id in dict.fromkeys(draft_block.evidence_block_ids):
                        document_block = session.scalar(
                            select(DocumentBlock).where(
                                DocumentBlock.parse_run_id == job.parse_run_id,
                                DocumentBlock.external_block_id == external_block_id,
                            )
                        )
                        if document_block is None:
                            continue
                        span = EvidenceSpan(
                            parse_run_id=job.parse_run_id,
                            page_id=document_block.page_id,
                            document_block_id=document_block.id,
                            quoted_text=document_block.text_content,
                            normalized_text_hash=_sha256_text(
                                " ".join(document_block.text_content.split()).lower()
                            ),
                            evidence_level="MINERU_TEXT_MATCHED",
                            confidence=1.0,
                        )
                        session.add(span)
                        session.flush()
                        if claim is not None:
                            session.add(ClaimEvidence(claim_id=claim.id, span_id=span.id))
                    for external_block_id in dict.fromkeys(draft_block.asset_block_ids):
                        visual_asset = session.scalar(
                            select(VisualAsset)
                            .join(
                                DocumentBlock,
                                DocumentBlock.id == VisualAsset.document_block_id,
                            )
                            .where(
                                VisualAsset.parse_run_id == job.parse_run_id,
                                DocumentBlock.external_block_id == external_block_id,
                            )
                        )
                        if visual_asset is not None:
                            if visual_asset.blob_id in linked_asset_blob_ids:
                                continue
                            exists = session.scalar(
                                select(NoteAsset).where(
                                    NoteAsset.note_version_id == version.id,
                                    NoteAsset.visual_asset_id == visual_asset.id,
                                )
                            )
                            if exists is None:
                                session.add(
                                    NoteAsset(
                                        note_version_id=version.id,
                                        visual_asset_id=visual_asset.id,
                                        note_block_id=block.id,
                                        order_index=asset_index,
                                        placement="INLINE",
                                        purpose=draft_block.purpose
                                        or f"支持笔记“{note.title}”正文块 {order + 1} 的视觉解释或证据",
                                        alt_text=draft_block.caption
                                        or visual_asset.caption
                                        or f"{note.title}的来源图片",
                                    )
                                )
                                linked_asset_blob_ids.add(visual_asset.blob_id)
                                asset_index += 1

                vector = self.owner.embedding.embed([f"{note.title}\n{note.thesis}\n{body}"])[0]
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
                    title=note.title,
                    thesis=note.thesis,
                    body=body,
                    tags=" ".join(note.tags),
                )
                atomic.active_version_id = version.id
                atomic.visibility = "ACTIVE"
                atomic.updated_at = utcnow()
                activated.append(atomic.id)
        if activated and publish_release:
            with self.owner.db.session() as session:
                source = session.get(Source, source_id)
                if source.visibility != "ACTIVE":
                    raise ValueError("Cannot publish notes for a deleted source")
                source.updated_at = utcnow()
                previous_notes = session.scalars(
                    select(AtomicNote).where(
                        AtomicNote.source_id == source_id,
                        AtomicNote.source_version_id != source_version_id,
                        AtomicNote.visibility == "ACTIVE",
                    )
                ).all()
                obsolete_current_notes = session.scalars(
                    select(AtomicNote).where(
                        AtomicNote.source_version_id == source_version_id,
                        AtomicNote.id.not_in(activated),
                        AtomicNote.visibility == "ACTIVE",
                    )
                ).all()
                hidden_notes = [*previous_notes, *obsolete_current_notes]
                for previous in hidden_notes:
                    previous.visibility = "HIDDEN"
                    previous.updated_at = utcnow()
                    delete_fts_for_note(session, previous.id)
                session.add(
                    AuditEvent(
                        actor=self.owner.agent.name,
                        action="ACTIVATE_SOURCE_VERSION",
                        risk_level="L1",
                        target_type="SourceVersion",
                        target_id=source_version_id,
                        detail_json=_json(
                            {
                                "activated_note_count": len(activated),
                                "hidden_previous_note_count": len(previous_notes),
                                "hidden_obsolete_note_count": len(obsolete_current_notes),
                            }
                        ),
                    )
                )
        return activated
