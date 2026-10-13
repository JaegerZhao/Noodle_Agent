from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..adapters.blob import LocalBlobStore
from ..db import (
    AtomicNote,
    AuditEvent,
    BackboneCorpus,
    BackboneFigure,
    BackboneSection,
    BlobObject,
    Claim,
    ClaimEvidence,
    Concept,
    ConceptBlock,
    ConceptVersion,
    Database,
    DocumentBlock,
    EvidenceSpan,
    LibraryEntry,
    LibraryFolder,
    NoteAsset,
    NoteBlock,
    NoteVersion,
    OutlineNode,
    ParseRun,
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
from .domain import BackboneMigrationReport

MIGRATION_PROVIDER = "BACKBONE_SOURCE_MIGRATION_V2"
MIGRATION_SOURCE_TITLE = "Probabilistic Machine Learning: An Introduction（教材完整笔记）"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blob_row(session, blobs: LocalBlobStore, path: Path, media_type: str) -> BlobObject:
    storage_key, digest, size = blobs.put(path, media_type)
    row = session.scalar(select(BlobObject).where(BlobObject.storage_key == storage_key))
    if row is None:
        row = BlobObject(
            storage_key=storage_key,
            content_sha256=digest,
            media_type=media_type,
            byte_size=size,
        )
        session.add(row)
        session.flush()
    return row


def _note_block_type(block_type: str) -> str:
    return {
        "MARKDOWN": "PARAGRAPH",
        "ALGORITHM": "NUMBERED_LIST",
        "LIMITATION": "CALLOUT",
        "FIGURE": "PARAGRAPH",
    }.get(block_type, block_type)


class BackboneSourceMigrator:
    """Moves the book-aligned knowledge projection into the ordinary note system."""

    def __init__(self, database: Database, blobs: LocalBlobStore):
        self.db = database
        self.blobs = blobs

    def migrate(self, corpus_id: str) -> BackboneMigrationReport:
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            if corpus is None:
                raise ValueError(f"Unknown corpus_id: {corpus_id}")
            pdf_path = Path(corpus.local_path).resolve()
            if not pdf_path.is_file():
                raise FileNotFoundError(pdf_path)

            original_blob = _blob_row(session, self.blobs, pdf_path, "application/pdf")
            source = session.scalar(
                select(Source).where(Source.title == MIGRATION_SOURCE_TITLE)
            )
            if source is None:
                source = Source(source_type="PDF", title=MIGRATION_SOURCE_TITLE)
                session.add(source)
                session.flush()
            source.visibility = "ACTIVE"

            source_version = session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.content_sha256 == corpus.content_sha256,
                )
            )
            if source_version is None:
                version_no = (
                    session.scalar(
                        select(func.max(SourceVersion.version_no)).where(
                            SourceVersion.source_id == source.id
                        )
                    )
                    or 0
                ) + 1
                source_version = SourceVersion(
                    source_id=source.id,
                    version_no=version_no,
                    original_blob_id=original_blob.id,
                    content_sha256=corpus.content_sha256,
                    media_type="application/pdf",
                    byte_size=original_blob.byte_size,
                    original_name=pdf_path.name,
                )
                session.add(source_version)
                session.flush()
            source.current_version_id = source_version.id

            parse_run = session.scalar(
                select(ParseRun).where(
                    ParseRun.source_version_id == source_version.id,
                    ParseRun.provider == MIGRATION_PROVIDER,
                )
            )
            if parse_run is None:
                parse_run = ParseRun(
                    source_version_id=source_version.id,
                    provider=MIGRATION_PROVIDER,
                    parser_version="backbone-source-v2",
                    request_hash=corpus.content_sha256,
                    status="SUCCEEDED",
                    completed_at=utcnow(),
                )
                session.add(parse_run)
                session.flush()
            source_version.canonical_parse_run_id = parse_run.id

            pages = session.scalars(
                select(SourcePage)
                .where(SourcePage.parse_run_id == parse_run.id)
                .order_by(SourcePage.page_index)
            ).all()
            if not pages:
                for page_index in range(corpus.page_count):
                    session.add(
                        SourcePage(
                            parse_run_id=parse_run.id,
                            page_index=page_index,
                            page_label=str(page_index + 1),
                            mapping_status="VERIFIED",
                        )
                    )
                session.flush()
                pages = session.scalars(
                    select(SourcePage)
                    .where(SourcePage.parse_run_id == parse_run.id)
                    .order_by(SourcePage.page_index)
                ).all()
            page_by_number = {item.page_index + 1: item for item in pages}

            sections = session.scalars(
                select(BackboneSection)
                .where(BackboneSection.corpus_id == corpus.id)
                .order_by(BackboneSection.sort_order)
            ).all()
            outline_count = session.scalar(
                select(func.count()).select_from(OutlineNode).where(
                    OutlineNode.parse_run_id == parse_run.id
                )
            ) or 0
            if not outline_count:
                outline_by_section: dict[str, OutlineNode] = {}
                for section in sections:
                    row = OutlineNode(
                        parse_run_id=parse_run.id,
                        parent_id=(
                            outline_by_section[section.parent_id].id
                            if section.parent_id in outline_by_section
                            else None
                        ),
                        title=(f"{section.number} {section.title}".strip()),
                        level=section.level,
                        start_page_index=max(0, section.start_page - 1),
                        end_page_index=max(0, section.end_page - 1),
                        source="BACKBONE_BOOKMARK",
                        order_index=section.sort_order,
                    )
                    session.add(row)
                    session.flush()
                    outline_by_section[section.id] = row

            document_blocks = {
                item.external_block_id: item
                for item in session.scalars(
                    select(DocumentBlock).where(DocumentBlock.parse_run_id == parse_run.id)
                ).all()
            }
            reading_order = max(
                (item.reading_order for item in document_blocks.values()), default=-1
            ) + 1
            section_document_blocks: dict[tuple[str, str], DocumentBlock] = {}
            note_by_section: dict[str, tuple[AtomicNote, NoteVersion]] = {}
            migrated_note_blocks = 0

            for section in sections:
                if section.node_kind != "CONCEPT" or not section.concept_id:
                    continue
                legacy = session.get(Concept, section.concept_id)
                legacy_version = (
                    session.get(ConceptVersion, legacy.active_version_id)
                    if legacy and legacy.active_version_id
                    else None
                )
                if legacy is None or legacy_version is None:
                    continue
                legacy_blocks = session.scalars(
                    select(ConceptBlock)
                    .where(ConceptBlock.concept_version_id == legacy_version.id)
                    .order_by(ConceptBlock.sort_order)
                ).all()
                for legacy_block in legacy_blocks:
                    external_id = f"pml:{section.stable_key}:{legacy_block.block_key}"
                    document_block = document_blocks.get(external_id)
                    if document_block is None:
                        document_block = DocumentBlock(
                            parse_run_id=parse_run.id,
                            page_id=page_by_number[section.start_page].id,
                            external_block_id=external_id,
                            block_type=legacy_block.block_type,
                            reading_order=reading_order,
                            text_content=legacy_block.content_text,
                            metadata_json=_json(
                                {
                                    "migration": MIGRATION_PROVIDER,
                                    "backbone_section_id": section.id,
                                    "legacy_concept_id": legacy.id,
                                    "legacy_concept_version_id": legacy_version.id,
                                    "pages": [section.start_page, section.end_page],
                                }
                            ),
                        )
                        session.add(document_block)
                        session.flush()
                        document_blocks[external_id] = document_block
                        reading_order += 1
                    section_document_blocks[(section.id, legacy_block.id)] = document_block

                stable_key = f"pml:{section.stable_key}"
                note = session.scalar(
                    select(AtomicNote).where(
                        AtomicNote.source_version_id == source_version.id,
                        AtomicNote.stable_key == stable_key,
                    )
                )
                if note is None:
                    note = AtomicNote(
                        source_id=source.id,
                        source_version_id=source_version.id,
                        stable_key=stable_key,
                    )
                    session.add(note)
                    session.flush()
                content_hash = _sha256_text(
                    _json(
                        {
                            "legacy_version": legacy_version.id,
                            "title": legacy_version.title,
                            "blocks": [item.content_sha256 for item in legacy_blocks],
                        }
                    )
                )
                note_version = session.scalar(
                    select(NoteVersion).where(
                        NoteVersion.note_id == note.id,
                        NoteVersion.content_sha256 == content_hash,
                    )
                )
                if note_version is None:
                    version_no = (
                        session.scalar(
                            select(func.max(NoteVersion.version_no)).where(
                                NoteVersion.note_id == note.id
                            )
                        )
                        or 0
                    ) + 1
                    note_version = NoteVersion(
                        note_id=note.id,
                        source_version_id=source_version.id,
                        parse_run_id=parse_run.id,
                        version_no=version_no,
                        parent_version_id=note.active_version_id,
                        title=legacy_version.title,
                        thesis=legacy_version.summary,
                        tags_json=_json(
                            ["机器学习", "PML", "基准教材", section.number]
                        ),
                        quality_status="AUTO_VALIDATED",
                        review_status="UNREVIEWED",
                        content_sha256=content_hash,
                        created_by="backbone-migration-v2",
                    )
                    session.add(note_version)
                    session.flush()
                    for order, legacy_block in enumerate(legacy_blocks):
                        content = legacy_block.content_text
                        block = NoteBlock(
                            note_version_id=note_version.id,
                            block_key=legacy_block.block_key,
                            block_type=_note_block_type(legacy_block.block_type),
                            order_index=order,
                            content_text=content,
                            content_json=_json({"text": content}),
                            content_hash=legacy_block.content_sha256,
                            origin="BACKBONE_MIGRATED",
                        )
                        session.add(block)
                        session.flush()
                        claim = Claim(
                            note_version_id=note_version.id,
                            note_block_id=block.id,
                            order_index=order,
                            claim_text=content,
                            claim_type=legacy_block.block_type,
                            support_status="SUPPORTED",
                        )
                        session.add(claim)
                        session.flush()
                        document_block = section_document_blocks[(section.id, legacy_block.id)]
                        span = EvidenceSpan(
                            parse_run_id=parse_run.id,
                            page_id=document_block.page_id,
                            document_block_id=document_block.id,
                            quoted_text=document_block.text_content,
                            normalized_text_hash=_sha256_text(
                                " ".join(document_block.text_content.split()).lower()
                            ),
                            evidence_level="BACKBONE_BLOCK_MIGRATED",
                            confidence=1.0,
                        )
                        session.add(span)
                        session.flush()
                        session.add(ClaimEvidence(claim_id=claim.id, span_id=span.id))
                        migrated_note_blocks += 1
                note.active_version_id = note_version.id
                note.visibility = "ACTIVE"
                note.updated_at = utcnow()
                body = "\n\n".join(item.content_text for item in legacy_blocks)
                delete_fts_for_note(session, note.id)
                insert_fts(
                    session,
                    note_version_id=note_version.id,
                    note_id=note.id,
                    title=note_version.title,
                    thesis=note_version.thesis,
                    body=body,
                    tags=f"机器学习 PML 基准教材 {section.number}",
                )
                note_by_section[section.id] = (note, note_version)

            visual_by_legacy_concept: dict[str, list[VisualAsset]] = {}
            figures = session.scalars(
                select(BackboneFigure)
                .where(BackboneFigure.corpus_id == corpus.id)
                .order_by(BackboneFigure.page_number, BackboneFigure.figure_label)
            ).all()
            corpus_root = pdf_path.parent.parent
            for figure in figures:
                external_id = f"pml-figure:{figure.id}"
                document_block = document_blocks.get(external_id)
                if document_block is None:
                    document_block = DocumentBlock(
                        parse_run_id=parse_run.id,
                        page_id=page_by_number[figure.page_number].id,
                        external_block_id=external_id,
                        block_type="IMAGE",
                        reading_order=reading_order,
                        text_content=figure.caption,
                        bbox_json=figure.bbox_json,
                        metadata_json=_json(
                            {
                                "figure_label": figure.figure_label,
                                "backbone_figure_id": figure.id,
                            }
                        ),
                    )
                    session.add(document_block)
                    session.flush()
                    document_blocks[external_id] = document_block
                    reading_order += 1
                visual = session.scalar(
                    select(VisualAsset).where(
                        VisualAsset.document_block_id == document_block.id
                    )
                )
                if visual is None:
                    image_path = (corpus_root / figure.storage_key).resolve()
                    blob = _blob_row(session, self.blobs, image_path, "image/png")
                    visual = VisualAsset(
                        parse_run_id=parse_run.id,
                        page_id=document_block.page_id,
                        document_block_id=document_block.id,
                        blob_id=blob.id,
                        asset_type="FIGURE",
                        caption=figure.caption_zh or figure.caption,
                        bbox_json=figure.bbox_json,
                        extraction_method="BACKBONE_FIGURE_RENDER",
                        quality_status="USABLE",
                    )
                    session.add(visual)
                    session.flush()
                else:
                    visual.caption = figure.caption_zh or figure.caption
                visual_by_legacy_concept.setdefault(figure.concept_id, []).append(visual)

            for section in sections:
                if section.id not in note_by_section or not section.concept_id:
                    continue
                _note, note_version = note_by_section[section.id]
                note_blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == note_version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                anchor = next(
                    (item for item in note_blocks if item.block_type == "IMAGE"),
                    note_blocks[0] if note_blocks else None,
                )
                for order, visual in enumerate(
                    visual_by_legacy_concept.get(section.concept_id, [])
                ):
                    existing = session.scalar(
                        select(NoteAsset).where(
                            NoteAsset.note_version_id == note_version.id,
                            NoteAsset.visual_asset_id == visual.id,
                        )
                    )
                    if existing is None:
                        session.add(
                            NoteAsset(
                                note_version_id=note_version.id,
                                visual_asset_id=visual.id,
                                note_block_id=anchor.id if anchor else None,
                                order_index=order,
                                placement="INLINE",
                                purpose="保留教材中的必要图示",
                                alt_text=visual.caption or note_version.title,
                            )
                        )
                    else:
                        existing.alt_text = visual.caption or note_version.title

            source_note = session.scalar(
                select(SourceNote).where(SourceNote.source_id == source.id)
            )
            if source_note is None:
                source_note = SourceNote(source_id=source.id)
                session.add(source_note)
                session.flush()
            composition = [
                {
                    "section": section.stable_key,
                    "note_version": note_by_section[section.id][1].id
                    if section.id in note_by_section
                    else None,
                }
                for section in sections
            ]
            release_hash = _sha256_text(_json(composition))
            active_release = (
                session.get(SourceNoteVersion, source_note.active_version_id)
                if source_note.active_version_id
                else None
            )
            if active_release and active_release.content_sha256 == release_hash:
                release = active_release
            else:
                release_no = (
                    session.scalar(
                        select(func.max(SourceNoteVersion.version_no)).where(
                            SourceNoteVersion.source_note_id == source_note.id
                        )
                    )
                    or 0
                ) + 1
                release = SourceNoteVersion(
                    source_note_id=source_note.id,
                    source_version_id=source_version.id,
                    parse_run_id=parse_run.id,
                    version_no=release_no,
                    parent_version_id=source_note.active_version_id,
                    title="《Probabilistic Machine Learning: An Introduction》完整教材笔记",
                    summary="按原书目录保留完整章节结构、中文知识笔记、教材图示与页码证据。",
                    learning_goal="",
                    content_sha256=release_hash,
                    created_by="backbone-migration-v2",
                )
                session.add(release)
                session.flush()
                release_nodes: dict[str, SourceNoteOutlineNode] = {}
                for section in sections:
                    node = SourceNoteOutlineNode(
                        source_note_version_id=release.id,
                        stable_key=section.stable_key,
                        parent_id=(
                            release_nodes[section.parent_id].id
                            if section.parent_id in release_nodes
                            else None
                        ),
                        title=f"{section.number} {section.title}".strip(),
                        level=section.level,
                        order_index=section.sort_order,
                        learning_goal="",
                        source_sections_json=_json(
                            [f"P.{section.start_page}–{section.end_page}"]
                        ),
                    )
                    session.add(node)
                    session.flush()
                    release_nodes[section.id] = node
                for section in sections:
                    if section.id not in note_by_section:
                        continue
                    note, note_version = note_by_section[section.id]
                    session.add(
                        SourceNoteUnitPlacement(
                            source_note_version_id=release.id,
                            outline_node_id=release_nodes[section.id].id,
                            note_id=note.id,
                            note_version_id=note_version.id,
                            order_index=0,
                        )
                    )
                source_note.active_version_id = release.id
                source_note.visibility = "ACTIVE"
                source_note.updated_at = utcnow()
                session.add(
                    AuditEvent(
                        actor="backbone-migration-v2",
                        action="MIGRATE_BACKBONE_TO_SOURCE_NOTE",
                        risk_level="L1",
                        target_type="SourceNoteVersion",
                        target_id=release.id,
                        detail_json=_json(
                            {
                                "outline_nodes": len(sections),
                                "atomic_notes": len(note_by_section),
                                "figures": len(figures),
                            }
                        ),
                    )
                )

            folder = session.scalar(
                select(LibraryFolder).where(
                    LibraryFolder.parent_id.is_(None),
                    LibraryFolder.name == "基准教材",
                )
            )
            if folder is None:
                folder = LibraryFolder(name="基准教材", sort_order=0)
                session.add(folder)
                session.flush()
            entry = session.scalar(
                select(LibraryEntry).where(LibraryEntry.source_id == source.id)
            )
            if entry is None:
                session.add(
                    LibraryEntry(folder_id=folder.id, source_id=source.id, sort_order=0)
                )

            return BackboneMigrationReport(
                source_id=source.id,
                source_version_id=source_version.id,
                parse_run_id=parse_run.id,
                source_note_id=source_note.id,
                source_note_version_id=release.id,
                outline_nodes=len(sections),
                atomic_notes=len(note_by_section),
                note_blocks=(
                    session.scalar(
                        select(func.count()).select_from(NoteBlock).join(
                            NoteVersion, NoteVersion.id == NoteBlock.note_version_id
                        ).join(AtomicNote, AtomicNote.id == NoteVersion.note_id).where(
                            AtomicNote.source_version_id == source_version.id
                        )
                    )
                    or migrated_note_blocks
                ),
                figures=len(figures),
            )
