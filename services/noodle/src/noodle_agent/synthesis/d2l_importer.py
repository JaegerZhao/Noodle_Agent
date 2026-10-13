from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..adapters.blob import LocalBlobStore
from ..db import (
    AtomicNote,
    AuditEvent,
    BackboneCorpus,
    BackboneSection,
    BlobObject,
    Claim,
    ClaimEvidence,
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

D2L_STABLE_KEY = "d2l-zh-v2-source"
D2L_SOURCE_TITLE = "《动手学深度学习》第二版（官方完整教材笔记）"
D2L_PROVIDER = "D2L_MARKDOWN_SOURCE_V1"


@dataclass(frozen=True)
class D2LNode:
    stable_key: str
    relative_path: str
    parent_key: str | None
    number: str
    title: str
    level: int
    order_index: int
    markdown: str


@dataclass(frozen=True)
class MarkdownPart:
    kind: str
    text: str
    image_path: Path | None = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blob_row(
    session, blobs: LocalBlobStore, path: Path, media_type: str
) -> BlobObject:
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


def _toc_targets(markdown: str) -> list[str]:
    targets: list[str] = []
    for match in re.finditer(r"```toc\s*\n(.*?)```", markdown, flags=re.DOTALL):
        for raw in match.group(1).splitlines():
            value = raw.strip()
            if not value or value.startswith((":", "#")):
                continue
            targets.append(value)
    return targets


def _title(markdown: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    if match:
        return match.group(1).strip().replace("`", "")
    lines = markdown.splitlines()
    for index in range(len(lines) - 1):
        if lines[index].strip() and re.fullmatch(r"=+", lines[index + 1].strip()):
            return lines[index].strip()
    return fallback.replace("-", " ").strip().title()


def _resolve_markdown(root: Path, target: str, parent: Path) -> Path:
    candidate = (parent / target).with_suffix(".md").resolve()
    if root.resolve() not in candidate.parents:
        raise ValueError(f"D2L TOC target escapes repository: {target}")
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate


def _build_nodes(root: Path) -> list[D2LNode]:
    index_path = root / "index.md"
    if not index_path.is_file():
        raise FileNotFoundError(index_path)
    top_targets = _toc_targets(index_path.read_text(encoding="utf-8"))
    metadata_numbers = {
        "chapter_preface/index": "P",
        "chapter_installation/index": "I",
        "chapter_notation/index": "N",
        "chapter_references/zreferences": "R",
    }
    nodes: list[D2LNode] = []
    visited: set[str] = set()
    numbered_chapter = 0

    def append(target: str, parent_key: str | None, number: str, level: int) -> None:
        path = _resolve_markdown(root, target, root)
        relative = path.relative_to(root).as_posix()
        stable_key = path.relative_to(root).with_suffix("").as_posix()
        if stable_key in visited:
            return
        visited.add(stable_key)
        markdown = path.read_text(encoding="utf-8")
        node = D2LNode(
            stable_key=stable_key,
            relative_path=relative,
            parent_key=parent_key,
            number=number,
            title=_title(markdown, path.stem),
            level=level,
            order_index=len(nodes),
            markdown=markdown,
        )
        nodes.append(node)
        for child_index, child in enumerate(_toc_targets(markdown), 1):
            child_target = (path.parent.relative_to(root) / child).as_posix()
            child_number = f"{number}.{child_index}" if number else str(child_index)
            append(child_target, stable_key, child_number, level + 1)

    for target in top_targets:
        number = metadata_numbers.get(target)
        if number is None:
            numbered_chapter += 1
            number = str(numbered_chapter)
        append(target, None, number, 1)
    return nodes


def _clean_markdown(markdown: str) -> str:
    value = re.sub(r"```toc\s*\n.*?```", "", markdown, flags=re.DOTALL)
    value = re.sub(r"```eval_rst\s*\n.*?```", "", value, flags=re.DOTALL)
    value = re.sub(r"^#\s+.*?$", "", value, count=1, flags=re.MULTILINE)
    lines = value.splitlines()
    if len(lines) >= 2 and lines[0].strip() and re.fullmatch(r"=+", lines[1].strip()):
        lines = lines[2:]
    value = "\n".join(lines)
    value = re.sub(
        r"^:(?:label|begin_tab|end_tab|width|height|class):.*?$",
        "",
        value,
        flags=re.MULTILINE,
    )
    value = re.sub(r":numref:`([^`]+)`", r"相关图表或章节（\1）", value)
    value = re.sub(r":ref:`([^`]+)`", r"相关章节（\1）", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _markdown_parts(root: Path, node_path: Path, markdown: str) -> list[MarkdownPart]:
    cleaned = _clean_markdown(markdown)
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    parts: list[MarkdownPart] = []
    cursor = 0
    for match in pattern.finditer(cleaned):
        before = cleaned[cursor : match.start()].strip()
        if before:
            parts.append(MarkdownPart(kind="MARKDOWN", text=before))
        alt = match.group(1).strip() or "教材图示"
        raw_target = match.group(2).strip().split()[0].strip("<>")
        if re.match(r"https?://", raw_target):
            parts.append(MarkdownPart(kind="MARKDOWN", text=match.group(0)))
        else:
            image_path = (node_path.parent / raw_target).resolve()
            if root.resolve() in image_path.parents and image_path.is_file():
                parts.append(
                    MarkdownPart(kind="IMAGE", text=alt, image_path=image_path)
                )
            else:
                parts.append(MarkdownPart(kind="MARKDOWN", text=match.group(0)))
        cursor = match.end()
    tail = cleaned[cursor:].strip()
    if tail:
        parts.append(MarkdownPart(kind="MARKDOWN", text=tail))
    return parts or [MarkdownPart(kind="MARKDOWN", text="本节仅包含结构信息。")]


def _thesis(parts: list[MarkdownPart], title: str) -> str:
    for part in parts:
        if part.kind != "MARKDOWN":
            continue
        plain = re.sub(r"```.*?```", "", part.text, flags=re.DOTALL)
        plain = re.sub(r"[#>*`$|]", "", plain)
        plain = " ".join(plain.split())
        if plain:
            return plain[:500]
    return f"本节介绍{title}。"


class D2LSourceImporter:
    """Import the official D2L Markdown tree into ordinary Noodle Notes first."""

    def __init__(self, database: Database, blobs: LocalBlobStore):
        self.db = database
        self.blobs = blobs

    def import_repository(self, repository: Path) -> BackboneMigrationReport:
        repository = repository.resolve()
        nodes = _build_nodes(repository)
        manifest_entries = [
            {
                "path": node.relative_path,
                "sha256": _sha256_text(node.markdown),
                "number": node.number,
                "title": node.title,
            }
            for node in nodes
        ]
        content_sha = _sha256_text(_json(manifest_entries))
        manifest = {
            "stable_key": D2L_STABLE_KEY,
            "domain_key": "deep-learning",
            "title": "动手学深度学习",
            "authors": ["Aston Zhang", "Zachary C. Lipton", "Mu Li", "Alexander J. Smola"],
            "edition_label": "2.0.0",
            "language": "zh",
            "source_url": "https://github.com/d2l-ai/d2l-zh",
            "license_note": "CC BY-SA 4.0（正文）与 MIT-0（示例代码）",
            "source_note_prefix": "d2l",
            "content_sha256": content_sha,
            "files": manifest_entries,
        }
        manifest_path = repository.parent / "d2l-import-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with self.db.session() as session:
            manifest_blob = _blob_row(
                session, self.blobs, manifest_path, "application/json"
            )
            corpus = session.scalar(
                select(BackboneCorpus).where(BackboneCorpus.stable_key == D2L_STABLE_KEY)
            )
            if corpus is None:
                corpus = BackboneCorpus(
                    stable_key=D2L_STABLE_KEY,
                    domain_key="deep-learning",
                    title="动手学深度学习",
                    authors_json=_json(manifest["authors"]),
                    edition_label="2.0.0",
                    language="zh",
                    source_url=manifest["source_url"],
                    license_note=manifest["license_note"],
                    local_path=str(repository),
                    content_sha256=content_sha,
                    page_count=len(nodes),
                    manifest_json=_json(manifest),
                    status="READY",
                )
                session.add(corpus)
                session.flush()
            elif corpus.content_sha256 != content_sha:
                raise ValueError(
                    "D2L source changed; use a new stable corpus key for the new edition"
                )

            section_by_key: dict[str, BackboneSection] = {
                item.stable_key: item
                for item in session.scalars(
                    select(BackboneSection).where(BackboneSection.corpus_id == corpus.id)
                ).all()
            }
            for node in nodes:
                section = section_by_key.get(node.stable_key)
                if section is None:
                    section = BackboneSection(
                        corpus_id=corpus.id,
                        stable_key=node.stable_key,
                        parent_id=(
                            section_by_key[node.parent_key].id
                            if node.parent_key in section_by_key
                            else None
                        ),
                        number=node.number,
                        title=node.title,
                        level=node.level,
                        sort_order=node.order_index,
                        node_kind="CONCEPT",
                        start_page=node.order_index + 1,
                        end_page=node.order_index + 1,
                        status="SOURCE_IMPORTED",
                        source_locator_json=_json(
                            {"path": node.relative_path, "number": node.number}
                        ),
                    )
                    session.add(section)
                    session.flush()
                    section_by_key[node.stable_key] = section

            source = session.scalar(select(Source).where(Source.title == D2L_SOURCE_TITLE))
            if source is None:
                source = Source(source_type="MARKDOWN", title=D2L_SOURCE_TITLE)
                session.add(source)
                session.flush()
            source.visibility = "ACTIVE"
            source_version = session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.content_sha256 == content_sha,
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
                    original_blob_id=manifest_blob.id,
                    content_sha256=content_sha,
                    media_type="application/vnd.noodle.textbook+json",
                    byte_size=manifest_blob.byte_size,
                    original_name="d2l-zh-2.0.0-source-manifest.json",
                )
                session.add(source_version)
                session.flush()
            source.current_version_id = source_version.id

            parse_run = session.scalar(
                select(ParseRun).where(
                    ParseRun.source_version_id == source_version.id,
                    ParseRun.provider == D2L_PROVIDER,
                )
            )
            if parse_run is None:
                parse_run = ParseRun(
                    source_version_id=source_version.id,
                    provider=D2L_PROVIDER,
                    parser_version="d2l-markdown-v1",
                    request_hash=content_sha,
                    status="SUCCEEDED",
                    completed_at=utcnow(),
                )
                session.add(parse_run)
                session.flush()
            source_version.canonical_parse_run_id = parse_run.id

            pages = {
                item.page_index: item
                for item in session.scalars(
                    select(SourcePage).where(SourcePage.parse_run_id == parse_run.id)
                ).all()
            }
            for node in nodes:
                if node.order_index not in pages:
                    page = SourcePage(
                        parse_run_id=parse_run.id,
                        page_index=node.order_index,
                        page_label=node.relative_path,
                        mapping_status="VERIFIED",
                    )
                    session.add(page)
                    session.flush()
                    pages[node.order_index] = page

            outline_existing = session.scalar(
                select(func.count()).select_from(OutlineNode).where(
                    OutlineNode.parse_run_id == parse_run.id
                )
            ) or 0
            if not outline_existing:
                outline_by_key: dict[str, OutlineNode] = {}
                for node in nodes:
                    outline = OutlineNode(
                        parse_run_id=parse_run.id,
                        parent_id=(
                            outline_by_key[node.parent_key].id
                            if node.parent_key in outline_by_key
                            else None
                        ),
                        title=f"{node.number} {node.title}".strip(),
                        level=node.level,
                        start_page_index=node.order_index,
                        end_page_index=node.order_index,
                        source="D2L_TOC",
                        order_index=node.order_index,
                    )
                    session.add(outline)
                    session.flush()
                    outline_by_key[node.stable_key] = outline

            document_blocks = {
                item.external_block_id: item
                for item in session.scalars(
                    select(DocumentBlock).where(DocumentBlock.parse_run_id == parse_run.id)
                ).all()
            }
            reading_order = max(
                (item.reading_order for item in document_blocks.values()), default=-1
            ) + 1
            note_by_key: dict[str, tuple[AtomicNote, NoteVersion]] = {}
            note_blocks_count = 0
            figures_count = 0

            for node in nodes:
                node_path = repository / node.relative_path
                parts = _markdown_parts(repository, node_path, node.markdown)
                part_hashes = [
                    _sha256_text(
                        _json(
                            {
                                "kind": part.kind,
                                "text": part.text,
                                "image": (
                                    _sha256_path(part.image_path)
                                    if part.image_path is not None
                                    else ""
                                ),
                            }
                        )
                    )
                    for part in parts
                ]
                note_hash = _sha256_text(
                    _json({"path": node.relative_path, "parts": part_hashes})
                )
                stable_key = f"d2l:{node.stable_key}"
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
                note_version = session.scalar(
                    select(NoteVersion).where(
                        NoteVersion.note_id == note.id,
                        NoteVersion.content_sha256 == note_hash,
                    )
                )
                if note_version is None:
                    note_version = NoteVersion(
                        note_id=note.id,
                        source_version_id=source_version.id,
                        parse_run_id=parse_run.id,
                        version_no=(
                            session.scalar(
                                select(func.max(NoteVersion.version_no)).where(
                                    NoteVersion.note_id == note.id
                                )
                            )
                            or 0
                        )
                        + 1,
                        parent_version_id=note.active_version_id,
                        title=node.title,
                        thesis=_thesis(parts, node.title),
                        tags_json=_json(["深度学习", "D2L", "基准教材", node.number]),
                        quality_status="SOURCE_VERIFIED",
                        review_status="UNREVIEWED",
                        content_sha256=note_hash,
                        created_by="d2l-source-import-v1",
                    )
                    session.add(note_version)
                    session.flush()
                    for part_index, part in enumerate(parts):
                        external_id = f"d2l:{node.stable_key}:part:{part_index}"
                        document_block = document_blocks.get(external_id)
                        if document_block is None:
                            document_block = DocumentBlock(
                                parse_run_id=parse_run.id,
                                page_id=pages[node.order_index].id,
                                external_block_id=external_id,
                                block_type=("IMAGE" if part.kind == "IMAGE" else "MARKDOWN"),
                                reading_order=reading_order,
                                text_content=part.text,
                                metadata_json=_json(
                                    {
                                        "path": node.relative_path,
                                        "part_index": part_index,
                                        "source": "D2L_OFFICIAL_MARKDOWN",
                                    }
                                ),
                            )
                            session.add(document_block)
                            session.flush()
                            document_blocks[external_id] = document_block
                            reading_order += 1
                        block_text = (
                            f"*教材图示：{part.text}*"
                            if part.kind == "IMAGE"
                            else part.text
                        )
                        note_block = NoteBlock(
                            note_version_id=note_version.id,
                            block_key=f"part-{part_index + 1:03d}",
                            block_type=("IMAGE" if part.kind == "IMAGE" else "PARAGRAPH"),
                            order_index=part_index,
                            content_text=block_text,
                            content_json=_json({"text": block_text}),
                            content_hash=part_hashes[part_index],
                            origin="SOURCE_IMPORTED",
                        )
                        session.add(note_block)
                        session.flush()
                        note_blocks_count += 1
                        claim = Claim(
                            note_version_id=note_version.id,
                            note_block_id=note_block.id,
                            order_index=part_index,
                            claim_text=block_text,
                            claim_type=("FIGURE" if part.kind == "IMAGE" else "SOURCE_TEXT"),
                            support_status="SUPPORTED",
                        )
                        session.add(claim)
                        session.flush()
                        span = EvidenceSpan(
                            parse_run_id=parse_run.id,
                            page_id=pages[node.order_index].id,
                            document_block_id=document_block.id,
                            quoted_text=part.text,
                            normalized_text_hash=_sha256_text(
                                " ".join(part.text.split()).lower()
                            ),
                            evidence_level="SOURCE_MARKDOWN_EXACT",
                            confidence=None,
                        )
                        session.add(span)
                        session.flush()
                        session.add(ClaimEvidence(claim_id=claim.id, span_id=span.id))
                        if part.kind == "IMAGE" and part.image_path is not None:
                            media_type = (
                                mimetypes.guess_type(part.image_path.name)[0]
                                or "application/octet-stream"
                            )
                            blob = _blob_row(
                                session, self.blobs, part.image_path, media_type
                            )
                            visual = session.scalar(
                                select(VisualAsset).where(
                                    VisualAsset.document_block_id == document_block.id
                                )
                            )
                            if visual is None:
                                visual = VisualAsset(
                                    parse_run_id=parse_run.id,
                                    page_id=pages[node.order_index].id,
                                    document_block_id=document_block.id,
                                    blob_id=blob.id,
                                    asset_type="FIGURE",
                                    caption=part.text,
                                    extraction_method="D2L_SOURCE_IMAGE",
                                    quality_status="USABLE",
                                )
                                session.add(visual)
                                session.flush()
                            session.add(
                                NoteAsset(
                                    note_version_id=note_version.id,
                                    visual_asset_id=visual.id,
                                    note_block_id=note_block.id,
                                    order_index=part_index,
                                    placement="INLINE",
                                    purpose="保留官方教材中的原始图示",
                                    alt_text=part.text,
                                )
                            )
                            figures_count += 1
                note.active_version_id = note_version.id
                note.visibility = "ACTIVE"
                note.updated_at = utcnow()
                active_blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == note_version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                body = "\n\n".join(item.content_text for item in active_blocks)
                delete_fts_for_note(session, note.id)
                insert_fts(
                    session,
                    note_version_id=note_version.id,
                    note_id=note.id,
                    title=note_version.title,
                    thesis=note_version.thesis,
                    body=body,
                    tags=f"深度学习 D2L 基准教材 {node.number}",
                )
                note_by_key[node.stable_key] = (note, note_version)

            source_note = session.scalar(
                select(SourceNote).where(SourceNote.source_id == source.id)
            )
            if source_note is None:
                source_note = SourceNote(source_id=source.id)
                session.add(source_note)
                session.flush()
            composition = [
                (node.stable_key, note_by_key[node.stable_key][1].id) for node in nodes
            ]
            release_hash = _sha256_text(_json(composition))
            release = session.scalar(
                select(SourceNoteVersion).where(
                    SourceNoteVersion.source_note_id == source_note.id,
                    SourceNoteVersion.content_sha256 == release_hash,
                )
            )
            if release is None:
                release = SourceNoteVersion(
                    source_note_id=source_note.id,
                    source_version_id=source_version.id,
                    parse_run_id=parse_run.id,
                    version_no=(
                        session.scalar(
                            select(func.max(SourceNoteVersion.version_no)).where(
                                SourceNoteVersion.source_note_id == source_note.id
                            )
                        )
                        or 0
                    )
                    + 1,
                    parent_version_id=source_note.active_version_id,
                    title="《动手学深度学习》第二版 · 完整框架笔记",
                    summary=(
                        "按官方目录完整保留中文正文、公式、代码、练习与教材图片；"
                        "该版本作为深度学习 Concept/Synthesis 的来源事实层。"
                    ),
                    learning_goal="",
                    content_sha256=release_hash,
                    created_by="d2l-source-import-v1",
                )
                session.add(release)
                session.flush()
                outline_by_key: dict[str, SourceNoteOutlineNode] = {}
                for node in nodes:
                    outline = SourceNoteOutlineNode(
                        source_note_version_id=release.id,
                        stable_key=node.stable_key,
                        parent_id=(
                            outline_by_key[node.parent_key].id
                            if node.parent_key in outline_by_key
                            else None
                        ),
                        title=f"{node.number} {node.title}".strip(),
                        level=node.level,
                        order_index=node.order_index,
                        learning_goal="",
                        source_sections_json=_json([node.relative_path]),
                    )
                    session.add(outline)
                    session.flush()
                    outline_by_key[node.stable_key] = outline
                for node in nodes:
                    note, note_version = note_by_key[node.stable_key]
                    session.add(
                        SourceNoteUnitPlacement(
                            source_note_version_id=release.id,
                            outline_node_id=outline_by_key[node.stable_key].id,
                            note_id=note.id,
                            note_version_id=note_version.id,
                            order_index=0,
                        )
                    )
                session.add(
                    AuditEvent(
                        actor="d2l-source-import-v1",
                        action="IMPORT_D2L_TO_NOTES",
                        risk_level="L1",
                        target_type="SourceNoteVersion",
                        target_id=release.id,
                        detail_json=_json(
                            {
                                "outline_nodes": len(nodes),
                                "atomic_notes": len(note_by_key),
                                "figures": figures_count,
                                "license": manifest["license_note"],
                            }
                        ),
                    )
                )
            source_note.active_version_id = release.id
            source_note.visibility = "ACTIVE"
            source_note.updated_at = utcnow()

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
                    LibraryEntry(folder_id=folder.id, source_id=source.id, sort_order=1)
                )

            note_blocks_total = session.scalar(
                select(func.count())
                .select_from(NoteBlock)
                .join(NoteVersion, NoteVersion.id == NoteBlock.note_version_id)
                .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                .where(AtomicNote.source_version_id == source_version.id)
            ) or 0
            figure_total = session.scalar(
                select(func.count())
                .select_from(NoteAsset)
                .join(NoteVersion, NoteVersion.id == NoteAsset.note_version_id)
                .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                .where(AtomicNote.source_version_id == source_version.id)
            ) or 0
            return BackboneMigrationReport(
                source_id=source.id,
                source_version_id=source_version.id,
                parse_run_id=parse_run.id,
                source_note_id=source_note.id,
                source_note_version_id=release.id,
                outline_nodes=len(nodes),
                atomic_notes=len(note_by_key),
                note_blocks=note_blocks_total,
                figures=figure_total,
            )
