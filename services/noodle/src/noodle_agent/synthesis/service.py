from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pymupdf as fitz
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from ..db import (
    BackboneCorpus,
    BackboneFigure,
    BackboneSection,
    BlobObject,
    Concept,
    ConceptBlock,
    ConceptContentEvidence,
    ConceptContribution,
    ConceptEvidenceLink,
    ConceptModule,
    ConceptModuleAsset,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptVersion,
    Database,
    DocumentBlock,
    EvidenceSpan,
    NoteAsset,
    NoteVersion,
    SourcePage,
    Synthesis,
    SynthesisNode,
    SynthesisVersion,
    VisualAsset,
)
from .domain import (
    BackboneManifest,
    BuildReport,
    ConceptDraft,
    FigureCaptionTranslationReport,
    FigureExtractionReport,
)
from .figures import figure_candidates, render_figure

CONCEPT_SCHEMA_VERSION = "concept-v1"
BACKBONE_PROMPT_VERSION = "backbone-writer-v1"
FIGURE_CAPTION_PROMPT_VERSION = "backbone-figure-caption-zh-v1"

MACHINE_LEARNING_MANIFEST = BackboneManifest(
    stable_key="pml-introduction-2025",
    domain_key="machine-learning",
    title="Probabilistic Machine Learning: An Introduction",
    authors=["Kevin Patrick Murphy"],
    edition_label="MIT Press 2022 · official draft 2025-04-18",
    source_url="https://probml.github.io/pml-book/book1.html",
    license_note="CC BY-NC-ND; private local knowledge use, source PDF excluded from Git",
)

_TOP_LEVEL_ZH = {
    "Preface": "前言",
    "Introduction": "机器学习导论",
    "Foundations": "基础理论",
    "Probability: Univariate Models": "概率：单变量模型",
    "Probability: Multivariate Models": "概率：多变量模型",
    "Statistics": "统计学基础",
    "Decision Theory": "决策理论",
    "Information Theory": "信息论",
    "Linear Algebra": "线性代数",
    "Optimization": "优化方法",
    "Linear Models": "线性模型",
    "Linear Discriminant Analysis": "线性判别分析",
    "Logistic Regression": "逻辑回归",
    "Linear Regression": "线性回归",
    "Generalized Linear Models": "广义线性模型",
    "Deep Neural Networks": "深度神经网络",
    "Neural Networks for Tabular Data": "表格数据神经网络",
    "Neural Networks for Images": "图像神经网络",
    "Neural Networks for Sequences": "序列神经网络",
    "Nonparametric Models": "非参数模型",
    "Exemplar-based Methods": "基于样本的方法",
    "Kernel Methods": "核方法",
    "Trees, Forests, Bagging, and Boosting": "树、森林、Bagging 与 Boosting",
    "Beyond Supervised Learning": "超越监督学习",
    "Learning with Fewer Labeled Examples": "少标注样本学习",
    "Dimensionality Reduction": "降维",
    "Clustering": "聚类",
    "Recommender Systems": "推荐系统",
    "Graph Embeddings": "图嵌入",
    "Notation": "符号表",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_title(raw: str) -> tuple[str, str]:
    title = re.sub(r"\s+", " ", raw).strip().rstrip(" *")
    match = re.match(r"^((?:[IVX]+|A|\d+(?:\.\d+)*))\s+(.+)$", title)
    return (match.group(1), match.group(2)) if match else ("", title)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:120] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _node_kind(level: int, number: str, title: str) -> str:
    if title in {"Index", "Bibliography"}:
        return "REFERENCE"
    if level == 1 and (number in {"I", "II", "III", "IV", "V"} or title in {
        "Foundations", "Linear Models", "Deep Neural Networks",
        "Nonparametric Models", "Beyond Supervised Learning",
    }):
        return "GROUP"
    return "CONCEPT"


def _canonical_title(number: str, title: str) -> str:
    if title == "Introduction" and number != "1":
        return "引言"
    return _TOP_LEVEL_ZH.get(title, title)


def _correct_reference_toc(document: fitz.Document, toc: list[list[Any]]) -> list[list[Any]]:
    """Repair reference bookmarks whose destination precedes the printed title."""

    corrected = [list(item) for item in toc]
    for item in corrected:
        _level, raw_title, raw_page = item
        _number, title = _clean_title(raw_title)
        if title not in {"Index", "Bibliography"}:
            continue
        start = max(1, int(raw_page) - 2)
        end = min(document.page_count, int(raw_page) + 80)
        for page_number in range(start, end + 1):
            lines = [
                line.strip()
                for line in document.load_page(page_number - 1).get_text("text").splitlines()
                if line.strip()
            ]
            if lines and lines[0].casefold() == title.casefold():
                item[2] = page_number
                break
    return corrected


def _compact_text(
    document: fitz.Document,
    start_page: int,
    end_page: int,
    *,
    max_pages: int = 3,
    max_chars: int = 8_000,
) -> str:
    chunks: list[str] = []
    upper = min(end_page, start_page + max(1, max_pages) - 1, document.page_count)
    for page_number in range(max(1, start_page), upper + 1):
        text = document.load_page(page_number - 1).get_text("text")
        chunks.append(text)
    text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    return text[:max_chars]


def _seed_draft(number: str, title: str, text: str) -> ConceptDraft:
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？])\s+", text)
        if 35 <= len(item.strip()) <= 520
    ]
    selected = sentences[:5]
    summary = (selected[0] if selected else text[:360]).strip()
    bullets = selected[1:5]
    body = summary
    if bullets:
        body += "\n\n" + "\n".join(f"- {item}" for item in bullets)
    if not body:
        body = "该目录节点已建立，等待模型根据教材证据补全内容。"
    return ConceptDraft(
        title_zh=_canonical_title(number, title),
        scope=f"教材目录中的 {number or title} 节；当前版本以该节原文为基准。",
        summary=summary or "该概念等待内容补全。",
        blocks=[{"block_key": "explanation", "block_type": "MARKDOWN", "content": body}],
    )


class SynthesisService:
    """Builds versioned Synthesis trees without coupling them to SourceNote jobs."""

    def __init__(self, database: Database, knowledge_agent: Any):
        self.db = database
        self.agent = knowledge_agent

    def register_backbone(
        self,
        pdf_path: Path,
        *,
        manifest: BackboneManifest = MACHINE_LEARNING_MANIFEST,
    ) -> BuildReport:
        pdf_path = pdf_path.resolve()
        if not pdf_path.exists():
            raise FileNotFoundError(pdf_path)
        content_sha = _sha256_bytes(pdf_path)
        document = fitz.open(pdf_path)
        try:
            toc = _correct_reference_toc(document, document.get_toc(simple=True))
            if not toc:
                raise ValueError("Backbone PDF has no bookmarks; a deterministic outline is required")
            with self.db.session() as session:
                corpus = session.scalar(
                    select(BackboneCorpus).where(BackboneCorpus.stable_key == manifest.stable_key)
                )
                if corpus is None:
                    corpus = BackboneCorpus(
                        stable_key=manifest.stable_key,
                        domain_key=manifest.domain_key,
                        title=manifest.title,
                        authors_json=_json(manifest.authors),
                        edition_label=manifest.edition_label,
                        language=manifest.language,
                        source_url=manifest.source_url,
                        license_note=manifest.license_note,
                        local_path=str(pdf_path),
                        content_sha256=content_sha,
                        page_count=document.page_count,
                        manifest_json=_json(manifest.model_dump()),
                        status="BUILDING",
                    )
                    session.add(corpus)
                    session.flush()
                elif corpus.content_sha256 != content_sha:
                    raise ValueError(
                        "The same backbone stable_key points to a different PDF hash; register a new edition key"
                    )
                existing = session.scalar(
                    select(func.count()).select_from(BackboneSection).where(
                        BackboneSection.corpus_id == corpus.id
                    )
                )
                if not existing:
                    self._seed_sections(session, document, corpus, toc)
                else:
                    self._normalize_structural_parts(session, corpus)
                    self._refresh_section_ranges(session, corpus, document, toc)
                    self._refresh_seeded_titles(session, corpus)
                    self._refresh_evidence_locators(session, corpus)
                report = self._ensure_synthesis(session, corpus)
                corpus.status = "READY"
                return report
        finally:
            document.close()

    def extract_figures(self, corpus_id: str) -> FigureExtractionReport:
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            if corpus is None:
                raise ValueError(f"Unknown corpus_id: {corpus_id}")
            sections = session.scalars(
                select(BackboneSection).where(
                    BackboneSection.corpus_id == corpus.id,
                    BackboneSection.node_kind == "CONCEPT",
                    BackboneSection.concept_id.is_not(None),
                )
            ).all()
            existing = {
                item.figure_label: item
                for item in session.scalars(
                    select(BackboneFigure).where(BackboneFigure.corpus_id == corpus.id)
                ).all()
            }
            corpus_root = Path(corpus.local_path).resolve().parent.parent
            output_dir = corpus_root / "figures"
            output_dir.mkdir(parents=True, exist_ok=True)
            discovered = 0
            extracted = 0
            skipped_unmapped = 0
            bytes_written = 0
            concept_ids: set[str] = set()
            page_numbers: set[int] = set()

            document = fitz.open(corpus.local_path)
            try:
                for page in document:
                    for candidate in figure_candidates(page):
                        discovered += 1
                        owners = [
                            section
                            for section in sections
                            if section.start_page <= candidate.page_number <= section.end_page
                        ]
                        if not owners:
                            skipped_unmapped += 1
                            continue
                        owner = min(
                            owners,
                            key=lambda item: (
                                -item.level,
                                item.end_page - item.start_page,
                                item.sort_order,
                            ),
                        )
                        image = render_figure(page, candidate)
                        digest = hashlib.sha256(image).hexdigest()
                        safe_label = re.sub(
                            r"[^a-zA-Z0-9]+", "-", candidate.figure_label
                        ).strip("-")
                        filename = f"p{candidate.page_number:04d}-figure-{safe_label}.png"
                        storage_key = f"figures/{filename}"
                        path = output_dir / filename
                        if not path.exists() or _sha256_bytes(path) != digest:
                            path.write_bytes(image)
                            bytes_written += len(image)
                        pixmap = fitz.Pixmap(image)
                        row = existing.get(candidate.figure_label)
                        if row is None:
                            row = BackboneFigure(
                                corpus_id=corpus.id,
                                section_id=owner.id,
                                concept_id=owner.concept_id,
                                page_number=candidate.page_number,
                                figure_label=candidate.figure_label,
                                caption=candidate.caption,
                                storage_key=storage_key,
                                image_sha256=digest,
                                bbox_json=_json(list(candidate.clip)),
                                width_px=pixmap.width,
                                height_px=pixmap.height,
                            )
                            session.add(row)
                            existing[candidate.figure_label] = row
                        else:
                            row.section_id = owner.id
                            row.concept_id = owner.concept_id
                            row.page_number = candidate.page_number
                            row.caption = candidate.caption
                            row.storage_key = storage_key
                            row.image_sha256 = digest
                            row.bbox_json = _json(list(candidate.clip))
                            row.width_px = pixmap.width
                            row.height_px = pixmap.height
                        extracted += 1
                        concept_ids.add(owner.concept_id)
                        page_numbers.add(candidate.page_number)
            finally:
                document.close()
            return FigureExtractionReport(
                corpus_id=corpus.id,
                discovered=discovered,
                extracted=extracted,
                skipped_unmapped=skipped_unmapped,
                concepts_with_figures=len(concept_ids),
                pages_with_figures=len(page_numbers),
                bytes_written=bytes_written,
            )

    def _translate_figure_caption_batch(
        self, figures: list[dict[str, str]]
    ) -> dict[str, str]:
        payload = self.agent.structured_json(
            system=(
                "你是机器学习教材的专业中文编辑。将输入的英文 Figure caption 翻译成准确、简洁、"
                "适合课程笔记阅读的中文。保留模型名、算法名、变量、公式、数据集名、人名和引用编号；"
                "专业术语首次出现时可保留英文括注。不要解释图片，不得增加原文没有的结论。"
                "caption_zh 只写图注正文，不重复 Figure 编号。输出严格 JSON："
                "{translations:[{figure_id,caption_zh}]}，不得遗漏、增加或修改 figure_id。"
            ),
            user={"figures": figures},
            operation="backbone.translate_figure_captions",
            prompt_version=FIGURE_CAPTION_PROMPT_VERSION,
            use_pro_model=False,
        )
        raw_items = payload.get("translations", []) if isinstance(payload, dict) else []
        translations = {
            str(item.get("figure_id") or ""): str(item.get("caption_zh") or "").strip()
            for item in raw_items
            if isinstance(item, dict)
        }
        expected = {item["figure_id"] for item in figures}
        if set(translations) != expected or any(not value for value in translations.values()):
            raise ValueError(
                f"Figure caption IDs expected={sorted(expected)}, actual={sorted(translations)}"
            )
        return translations

    def translate_figure_captions(
        self,
        corpus_id: str,
        *,
        batch_size: int = 16,
        concurrency: int = 4,
        force: bool = False,
    ) -> FigureCaptionTranslationReport:
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            if corpus is None:
                raise ValueError(f"Unknown corpus_id: {corpus_id}")
            all_figures = session.scalars(
                select(BackboneFigure)
                .where(BackboneFigure.corpus_id == corpus_id)
                .order_by(BackboneFigure.page_number, BackboneFigure.figure_label)
            ).all()
            pending = [item for item in all_figures if force or not item.caption_zh.strip()]
            skipped_existing = len(all_figures) - len(pending)
            payloads = [
                {
                    "figure_id": item.id,
                    "figure_label": item.figure_label,
                    "caption": item.caption,
                }
                for item in pending
            ]
        size = max(1, batch_size)
        batches = [
            payloads[index : index + size]
            for index in range(0, len(payloads), size)
        ]
        translated: dict[str, str] = {}
        failed: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            futures = {
                executor.submit(self._translate_figure_caption_batch, batch): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    translated.update(future.result())
                except Exception as exc:  # noqa: BLE001 - isolate one failed batch
                    failed.append(
                        {
                            "figure_ids": ",".join(item["figure_id"] for item in batch),
                            "error": str(exc),
                        }
                    )
        with self.db.session() as session:
            figures = session.scalars(
                select(BackboneFigure).where(BackboneFigure.corpus_id == corpus_id)
            ).all()
            for figure in figures:
                if figure.id in translated:
                    figure.caption_zh = translated[figure.id]
                caption_zh = figure.caption_zh.strip()
                if caption_zh:
                    document_block = session.scalar(
                        select(DocumentBlock).where(
                            DocumentBlock.external_block_id == f"pml-figure:{figure.id}"
                        )
                    )
                    if document_block is None:
                        continue
                    visual = session.scalar(
                        select(VisualAsset).where(
                            VisualAsset.document_block_id == document_block.id
                        )
                    )
                    if visual is None:
                        continue
                    visual.caption = caption_zh
                    for placement in session.scalars(
                        select(ConceptModuleAsset).where(
                            ConceptModuleAsset.visual_asset_id == visual.id
                        )
                    ):
                        # The placement is part of an immutable Concept release
                        # and keeps the original source caption. Localization is
                        # mutable VisualAsset metadata selected by the reader.
                        placement.caption = figure.caption
                        placement.alt_text = figure.caption
                    for note_asset in session.scalars(
                        select(NoteAsset).where(NoteAsset.visual_asset_id == visual.id)
                    ):
                        note_asset.alt_text = caption_zh
        return FigureCaptionTranslationReport(
            corpus_id=corpus_id,
            requested=len(pending),
            translated=len(translated),
            skipped_existing=skipped_existing,
            batches=len(batches),
            failed=failed,
        )

    @staticmethod
    def _refresh_section_ranges(session, corpus, document, toc: list[list[Any]]) -> None:
        sections = session.scalars(
            select(BackboneSection)
            .where(BackboneSection.corpus_id == corpus.id)
            .order_by(BackboneSection.sort_order)
        ).all()
        if len(sections) != len(toc):
            raise ValueError("Stored backbone outline no longer matches the PDF bookmark count")
        for index, (section, toc_item) in enumerate(zip(sections, toc, strict=True)):
            level, _raw_title, page = toc_item
            next_page = document.page_count
            for next_level, _next_title, candidate_page in toc[index + 1 :]:
                if int(next_level) <= int(level):
                    next_page = max(int(page), int(candidate_page) - 1)
                    break
            section.start_page = max(1, int(page))
            section.end_page = min(document.page_count, next_page)
            section.source_locator_json = _json(
                {"pages": [section.start_page, section.end_page]}
            )

    def get_figure_path(self, figure_id: str) -> Path:
        with self.db.session() as session:
            figure = session.get(BackboneFigure, figure_id)
            if figure is None:
                raise ValueError(f"Unknown figure_id: {figure_id}")
            corpus = session.get(BackboneCorpus, figure.corpus_id)
            if corpus is None:
                raise ValueError("Figure corpus is missing")
            root = Path(corpus.local_path).resolve().parent.parent
            path = (root / figure.storage_key).resolve()
            if root not in path.parents or not path.is_file():
                raise FileNotFoundError(path)
            return path

    def _refresh_seeded_titles(self, session, corpus) -> None:
        sections = session.scalars(
            select(BackboneSection).where(BackboneSection.corpus_id == corpus.id)
        ).all()
        for section in sections:
            title = _canonical_title(section.number, section.title)
            if section.concept_id:
                concept = session.get(Concept, section.concept_id)
                if concept and concept.status == "SEEDED":
                    concept.canonical_name = title
                    version = session.get(ConceptVersion, concept.active_version_id)
                    if version and version.status == "SEEDED":
                        version.title = title
                for node in session.scalars(
                    select(SynthesisNode).where(SynthesisNode.concept_id == section.concept_id)
                ).all():
                    if concept and concept.status == "SEEDED":
                        node.title = title
            elif section.node_kind == "GROUP":
                for node in session.scalars(
                    select(SynthesisNode).where(SynthesisNode.stable_key == section.stable_key)
                ).all():
                    node.title = title

    def _normalize_structural_parts(self, session, corpus) -> None:
        """Repair corpora registered before multi-character Roman numerals were parsed."""
        sections = session.scalars(
            select(BackboneSection).where(
                BackboneSection.corpus_id == corpus.id,
                BackboneSection.level == 1,
            )
        ).all()
        for section in sections:
            match = re.match(r"^(II|III|IV)\s+(.+)$", section.title)
            if not match:
                continue
            number, title = match.groups()
            concept = session.get(Concept, section.concept_id) if section.concept_id else None
            nodes = session.scalars(
                select(SynthesisNode).where(SynthesisNode.stable_key == section.stable_key)
            ).all()
            for node in nodes:
                node.node_kind = "GROUP"
                node.concept_id = None
                node.concept_version_id = None
                node.title = _canonical_title(number, title)
                node.original_title = f"{number} {title}"
            if concept is not None:
                version_ids = session.scalars(
                    select(ConceptVersion.id).where(ConceptVersion.concept_id == concept.id)
                ).all()
                block_ids = session.scalars(
                    select(ConceptBlock.id).where(ConceptBlock.concept_version_id.in_(version_ids))
                ).all() if version_ids else []
                if block_ids:
                    session.execute(
                        delete(ConceptEvidenceLink).where(
                            ConceptEvidenceLink.concept_block_id.in_(block_ids)
                        )
                    )
                    session.execute(delete(ConceptBlock).where(ConceptBlock.id.in_(block_ids)))
                if version_ids:
                    session.execute(
                        delete(ConceptVersion).where(ConceptVersion.id.in_(version_ids))
                    )
                session.delete(concept)
            section.number = number
            section.title = title
            section.node_kind = "GROUP"
            section.concept_id = None
            section.status = "STRUCTURAL"

    def _refresh_evidence_locators(self, session, corpus) -> None:
        sections = session.scalars(
            select(BackboneSection).where(
                BackboneSection.corpus_id == corpus.id,
                BackboneSection.concept_id.is_not(None),
            )
        ).all()
        for section in sections:
            evidence_end = self._evidence_end(session, section)
            version_ids = session.scalars(
                select(ConceptVersion.id).where(
                    ConceptVersion.concept_id == section.concept_id
                )
            ).all()
            block_ids = session.scalars(
                select(ConceptBlock.id).where(
                    ConceptBlock.concept_version_id.in_(version_ids)
                )
            ).all() if version_ids else []
            links = session.scalars(
                select(ConceptEvidenceLink).where(
                    ConceptEvidenceLink.concept_block_id.in_(block_ids),
                    ConceptEvidenceLink.source_role == "BACKBONE",
                )
            ).all() if block_ids else []
            for link in links:
                link.source_ref = (
                    f"{section.stable_key}:p{section.start_page}-p{evidence_end}"
                )
                link.source_locator_json = _json(
                    {
                        "backbone_section_id": section.id,
                        "pages": [section.start_page, evidence_end],
                    }
                )

    @staticmethod
    def _evidence_end(session, section: BackboneSection) -> int:
        child_count = session.scalar(
            select(func.count())
            .select_from(BackboneSection)
            .where(BackboneSection.parent_id == section.id)
        ) or 0
        if child_count == 0:
            return section.end_page
        return min(section.end_page, section.start_page + 2)

    def _seed_sections(self, session, document, corpus, toc: list[list[Any]]) -> None:
        stack: dict[int, BackboneSection] = {}
        section_rows: list[tuple[BackboneSection, str, str]] = []
        for index, (level, raw_title, page) in enumerate(toc):
            number, title = _clean_title(raw_title)
            stable_key = f"{corpus.domain_key}:{number or _slug(title)}"
            if any(item[0].stable_key == stable_key for item in section_rows):
                stable_key = f"{stable_key}:{index + 1}"
            next_page = document.page_count
            for next_level, _next_title, candidate_page in toc[index + 1 :]:
                if int(next_level) <= int(level):
                    next_page = max(int(page), int(candidate_page) - 1)
                    break
            parent = stack.get(int(level) - 1)
            kind = _node_kind(int(level), number, title)
            row = BackboneSection(
                corpus_id=corpus.id,
                stable_key=stable_key,
                parent_id=parent.id if parent else None,
                number=number,
                title=title,
                level=int(level),
                sort_order=index,
                node_kind=kind,
                start_page=max(1, int(page)),
                end_page=min(document.page_count, next_page),
                status="PLANNED" if kind == "CONCEPT" else "STRUCTURAL",
                source_locator_json=_json({"pages": [int(page), min(document.page_count, next_page)]}),
            )
            session.add(row)
            session.flush()
            stack[int(level)] = row
            for stale_level in [item for item in stack if item > int(level)]:
                del stack[stale_level]
            section_rows.append((row, number, title))

        for row, number, title in section_rows:
            if row.node_kind != "CONCEPT":
                continue
            text = _compact_text(document, row.start_page, row.end_page)
            draft = _seed_draft(number, title, text)
            concept = Concept(
                stable_key=row.stable_key,
                domain_key=corpus.domain_key,
                canonical_name=draft.title_zh,
                original_name=f"{number} {title}".strip(),
                status="SEEDED",
            )
            session.add(concept)
            session.flush()
            version = self._persist_concept_version(
                session, concept, draft, row, source_role="BACKBONE", status="SEEDED"
            )
            concept.active_version_id = version.id
            row.concept_id = concept.id
            row.status = "SEEDED"

    def _persist_concept_version(
        self, session, concept, draft: ConceptDraft, section, *, source_role: str, status: str
    ) -> ConceptVersion:
        next_version = (session.scalar(
            select(func.max(ConceptVersion.version_no)).where(ConceptVersion.concept_id == concept.id)
        ) or 0) + 1
        content_hash = _sha256_text(_json(draft.model_dump()))
        version = ConceptVersion(
            concept_id=concept.id,
            version_no=next_version,
            title=draft.title_zh,
            original_title=concept.original_name,
            scope=draft.scope,
            summary=draft.summary,
            content_sha256=content_hash,
            schema_version=CONCEPT_SCHEMA_VERSION,
            prompt_version=BACKBONE_PROMPT_VERSION,
            status=status,
        )
        session.add(version)
        session.flush()
        evidence_end = self._evidence_end(session, section)
        for order, item in enumerate(draft.blocks):
            block = ConceptBlock(
                concept_version_id=version.id,
                block_key=item.block_key,
                block_type=item.block_type,
                sort_order=order,
                content_text=item.content,
                content_json="{}",
                source_role=source_role,
                content_sha256=_sha256_text(item.content),
            )
            session.add(block)
            session.flush()
            session.add(ConceptEvidenceLink(
                concept_block_id=block.id,
                source_role=source_role,
                source_ref=(
                    f"{section.stable_key}:p{section.start_page}-"
                    f"p{evidence_end}"
                ),
                source_locator_json=_json({
                    "backbone_section_id": section.id,
                    "pages": [
                        section.start_page,
                        evidence_end,
                    ],
                }),
            ))
        return version

    def _ensure_synthesis(self, session, corpus) -> BuildReport:
        synthesis_key = f"{corpus.domain_key}-foundation"
        synthesis = session.scalar(select(Synthesis).where(Synthesis.stable_key == synthesis_key))
        if synthesis is None:
            synthesis = Synthesis(
                stable_key=synthesis_key,
                domain_key=corpus.domain_key,
                title="机器学习知识体系",
                description="以权威教材目录为骨架、可持续吸收个人来源笔记的长期知识综合。",
            )
            session.add(synthesis)
            session.flush()
        version = session.scalar(
            select(SynthesisVersion).where(
                SynthesisVersion.synthesis_id == synthesis.id,
                SynthesisVersion.backbone_corpus_id == corpus.id,
            )
        )
        sections = session.scalars(
            select(BackboneSection).where(BackboneSection.corpus_id == corpus.id).order_by(
                BackboneSection.sort_order
            )
        ).all()
        concept_total = sum(item.node_kind == "CONCEPT" for item in sections)
        published = sum(item.status == "PUBLISHED" for item in sections)
        seeded = sum(item.status in {"SEEDED", "PUBLISHED"} for item in sections)
        coverage = {
            "toc_nodes": len(sections), "concept_nodes": concept_total,
            "seeded_concepts": seeded, "published_concepts": published,
            "coverage": round(seeded / concept_total, 4) if concept_total else 0,
        }
        release_status = (
            "PUBLISHED"
            if concept_total and published == concept_total
            else "SEEDED"
            if concept_total and seeded == concept_total
            else "BUILDING"
        )
        release_hash = _sha256_text(
            _json(
                [
                    (
                        item.stable_key,
                        session.get(Concept, item.concept_id).active_version_id
                        if item.concept_id
                        else None,
                    )
                    for item in sections
                ]
            )
        )
        if version is None:
            version = SynthesisVersion(
                synthesis_id=synthesis.id,
                backbone_corpus_id=corpus.id,
                version_no=1,
                title="机器学习知识体系 · 教材基准版",
                summary="覆盖概率、统计、优化、线性模型、深度网络、非参数方法及无监督学习。",
                status=release_status,
                coverage_json=_json(coverage),
                content_sha256=release_hash,
            )
            session.add(version)
            session.flush()
            node_ids: dict[str, str] = {}
            for item in sections:
                concept = session.get(Concept, item.concept_id) if item.concept_id else None
                node = SynthesisNode(
                    synthesis_version_id=version.id,
                    stable_key=item.stable_key,
                    parent_id=node_ids.get(item.parent_id or ""),
                    title=concept.canonical_name if concept else _canonical_title(item.number, item.title),
                    original_title=f"{item.number} {item.title}".strip(),
                    level=item.level,
                    sort_order=item.sort_order,
                    node_kind=item.node_kind,
                    concept_id=item.concept_id,
                    concept_version_id=concept.active_version_id if concept else None,
                    source_locator_json=item.source_locator_json,
                )
                session.add(node)
                session.flush()
                node_ids[item.id] = node.id
            synthesis.active_version_id = version.id
        else:
            version.coverage_json = _json(coverage)
            version.status = release_status
            version.content_sha256 = release_hash
        return BuildReport(
            corpus_id=corpus.id,
            synthesis_id=synthesis.id,
            synthesis_version_id=version.id,
            status=version.status,
            **coverage,
        )

    def enrich_concept(self, concept_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None:
                raise ValueError(f"Unknown concept_id: {concept_id}")
            section = session.scalar(
                select(BackboneSection).where(BackboneSection.concept_id == concept.id)
            )
            corpus = session.get(BackboneCorpus, section.corpus_id) if section else None
            if section is None or corpus is None:
                raise ValueError("Concept is not bound to a backbone section")
            evidence_end = self._evidence_end(session, section)
            evidence_page_count = evidence_end - section.start_page + 1
            is_leaf = evidence_end == section.end_page
            document = fitz.open(corpus.local_path)
            try:
                evidence_text = _compact_text(
                    document,
                    section.start_page,
                    evidence_end,
                    max_pages=evidence_page_count,
                    max_chars=60_000 if is_leaf else 8_000,
                )
            finally:
                document.close()
            payload = self.agent.structured_json(
                system=(
                    "你是严谨的机器学习课程助教。只依据给定教材证据，把一个目录节点写成简洁、易读、可长期维护的中文 Concept。"
                    "专业词首次出现保留英文括注；公式使用标准 Markdown LaTeX。不要输出学习目标、本节目标、学习指南或固定的核心结论栏目。"
                    "内容可用短段落、列表、表格；只有证据确实支持时才写公式、算法、例子、限制。"
                    "输出 JSON：{title_zh,scope,summary,blocks:[{block_key,block_type,content}]}。"
                    "block_type 只能是 MARKDOWN/FORMULA/ALGORITHM/EXAMPLE/TABLE/FIGURE/LIMITATION。"
                ),
                user={
                    "section": {
                        "stable_key": section.stable_key, "number": section.number,
                        "title": section.title, "pages": [section.start_page, section.end_page],
                    },
                    "evidence": {"text": evidence_text},
                },
                operation="backbone.write_concept",
                prompt_version=BACKBONE_PROMPT_VERSION,
                use_pro_model=False,
            )
            try:
                draft = ConceptDraft.model_validate(payload)
            except ValidationError as exc:
                raise ValueError(f"Invalid Concept payload: {exc}") from exc
            version = self._persist_concept_version(
                session, concept, draft, section, source_role="BACKBONE", status="PUBLISHED"
            )
            concept.canonical_name = draft.title_zh
            concept.active_version_id = version.id
            concept.status = "PUBLISHED"
            section.status = "PUBLISHED"
            nodes = session.scalars(select(SynthesisNode).where(SynthesisNode.concept_id == concept.id)).all()
            for node in nodes:
                node.title = draft.title_zh
                node.concept_version_id = version.id
            return {"concept_id": concept.id, "version_id": version.id, "status": "PUBLISHED"}

    def enrich_pending(
        self,
        corpus_id: str,
        *,
        limit: int | None = None,
        concurrency: int = 4,
        include_published: bool = False,
        long_leaves_only: bool = False,
    ) -> dict[str, Any]:
        with self.db.session() as session:
            sections = session.scalars(
                select(BackboneSection)
                .where(BackboneSection.corpus_id == corpus_id)
                .order_by(BackboneSection.level.desc(), BackboneSection.sort_order)
            ).all()
            parent_ids = {item.parent_id for item in sections if item.parent_id}
            targets = [
                item
                for item in sections
                if item.node_kind == "CONCEPT"
                and item.concept_id
                and (include_published or item.status != "PUBLISHED")
                and (
                    not long_leaves_only
                    or (
                        item.id not in parent_ids
                        and item.end_page - item.start_page + 1 > 3
                    )
                )
            ]
            concept_ids = [item.concept_id for item in targets]
        if limit is not None:
            concept_ids = concept_ids[: max(0, limit)]
        completed: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            futures = {executor.submit(self.enrich_concept, concept_id): concept_id for concept_id in concept_ids}
            for future in as_completed(futures):
                concept_id = futures[future]
                try:
                    completed.append(future.result())
                except Exception as exc:  # noqa: BLE001 - one failed batch must not cancel peers
                    failed.append({"concept_id": concept_id, "error": str(exc)})
        with self.db.session() as session:
            corpus = session.get(BackboneCorpus, corpus_id)
            report = self._ensure_synthesis(session, corpus) if corpus else None
        return {
            "requested": len(concept_ids), "completed": len(completed), "failed": failed,
            "coverage": report.model_dump() if report else None,
        }

    def list_syntheses(self) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(Synthesis).where(Synthesis.visibility == "ACTIVE").order_by(Synthesis.title)
            ).all()
            result = []
            for item in rows:
                version = session.get(SynthesisVersion, item.active_version_id) if item.active_version_id else None
                result.append({
                    "synthesis_id": item.id, "stable_key": item.stable_key,
                    "domain_key": item.domain_key,
                    "title": item.title, "description": item.description,
                    "version_id": version.id if version else None,
                    "version_no": version.version_no if version else None,
                    "status": version.status if version else "EMPTY",
                    "coverage": _loads(version.coverage_json, {}) if version else {},
                })
            return result

    def get_synthesis(self, synthesis_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            synthesis = session.get(Synthesis, synthesis_id)
            if synthesis is None or synthesis.visibility != "ACTIVE":
                raise ValueError(f"Unknown synthesis_id: {synthesis_id}")
            version = session.get(SynthesisVersion, synthesis.active_version_id)
            if version is None:
                raise ValueError("Synthesis has no active version")
            rows = session.scalars(
                select(SynthesisNode).where(SynthesisNode.synthesis_version_id == version.id).order_by(
                    SynthesisNode.sort_order
                )
            ).all()
            nodes: dict[str, dict[str, Any]] = {}
            roots: list[dict[str, Any]] = []
            for row in rows:
                node = {
                    "node_id": row.id, "stable_key": row.stable_key, "title": row.title,
                    "original_title": row.original_title, "level": row.level,
                    "node_kind": row.node_kind, "concept_id": row.concept_id,
                    "concept_version_id": row.concept_version_id,
                    "source_locator": _loads(row.source_locator_json, {}), "children": [],
                }
                nodes[row.id] = node
                if row.parent_id and row.parent_id in nodes:
                    nodes[row.parent_id]["children"].append(node)
                else:
                    roots.append(node)
            return {
                "synthesis_id": synthesis.id, "version_id": version.id,
                "stable_key": synthesis.stable_key, "domain_key": synthesis.domain_key,
                "title": synthesis.title, "description": synthesis.description,
                "version_title": version.title, "status": version.status,
                "coverage": _loads(version.coverage_json, {}), "outline": roots,
            }

    def get_concept(
        self, concept_id: str, *, version_id: str | None = None
    ) -> dict[str, Any]:
        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility != "ACTIVE":
                raise ValueError(f"Unknown concept_id: {concept_id}")
            resolved_version_id = version_id or concept.active_version_id
            version = session.get(ConceptVersion, resolved_version_id) if resolved_version_id else None
            if version is None:
                raise ValueError("Concept has no active version")
            if version.concept_id != concept.id:
                raise ValueError(f"Concept version does not belong to Concept: {version.id}")
            if version.schema_version in {"concept-v2", "concept-v3"}:
                return self._get_semantic_concept(session, concept, version)
            blocks = session.scalars(
                select(ConceptBlock).where(ConceptBlock.concept_version_id == version.id).order_by(
                    ConceptBlock.sort_order
                )
            ).all()
            evidence = session.scalars(
                select(ConceptEvidenceLink).where(
                    ConceptEvidenceLink.concept_block_id.in_([item.id for item in blocks])
                )
            ).all() if blocks else []
            evidence_by_block: dict[str, list[dict[str, Any]]] = {}
            for link in evidence:
                evidence_by_block.setdefault(link.concept_block_id, []).append({
                    "source_role": link.source_role, "source_ref": link.source_ref,
                    "source_locator": _loads(link.source_locator_json, {}),
                })
            figures = session.scalars(
                select(BackboneFigure)
                .where(BackboneFigure.concept_id == concept.id)
                .order_by(BackboneFigure.page_number, BackboneFigure.figure_label)
            ).all()
            return {
                "concept_id": concept.id, "stable_key": concept.stable_key,
                "domain_key": concept.domain_key, "title": version.title,
                "original_title": version.original_title, "scope": version.scope,
                "summary": version.summary, "status": version.status,
                "version_id": version.id, "version_no": version.version_no,
                "figures": [
                    {
                        "figure_id": item.id,
                        "figure_label": item.figure_label,
                        "caption": item.caption_zh or item.caption,
                        "caption_original": item.caption,
                        "page_number": item.page_number,
                        "width": item.width_px,
                        "height": item.height_px,
                        "asset_url": (
                            f"/internal/v1/concept-figures/{item.id}"
                            f"?v={item.image_sha256[:16]}"
                        ),
                    }
                    for item in figures
                ],
                "blocks": [{
                    "block_id": item.id, "block_key": item.block_key,
                    "block_type": item.block_type, "content": item.content_text,
                    "source_role": item.source_role,
                    "evidence": evidence_by_block.get(item.id, []),
                } for item in blocks],
            }

    @staticmethod
    def _content_block_markdown(block: dict[str, Any]) -> str:
        block_type = str(block.get("type") or "PROSE")
        if block_type == "LIST":
            return "\n".join(f"- {item}" for item in block.get("items", []) if item)
        if block_type == "TABLE":
            headers = block.get("table_headers", [])
            rows = block.get("table_rows", [])
            if headers:
                return "\n".join(
                    [
                        "| " + " | ".join(str(item) for item in headers) + " |",
                        "| " + " | ".join("---" for _ in headers) + " |",
                        *[
                            "| " + " | ".join(str(cell) for cell in row) + " |"
                            for row in rows
                        ],
                    ]
                )
        if block_type == "PROCEDURE":
            return "\n".join(
                f"{index}. {item}"
                for index, item in enumerate(block.get("steps", []), 1)
            )
        if block_type == "CODE":
            return f"```{block.get('language', '')}\n{block.get('code', '')}\n```"
        if block_type == "FORMULA":
            formula = str(block.get("latex") or "").strip().strip("$")
            explanation = str(block.get("markdown") or "")
            symbols = block.get("symbols", [])
            symbol_text = "\n".join(
                f"- `${item.get('symbol', '')}`：{item.get('meaning') or item.get('description') or ''}"
                for item in symbols
                if isinstance(item, dict) and item.get("symbol")
            )
            return "\n\n".join(
                item
                for item in [f"$$\n{formula}\n$$" if formula else "", explanation, symbol_text]
                if item
            )
        if block_type == "CALLOUT":
            return "\n".join(
                f"> {line}" if line else ">"
                for line in str(block.get("markdown") or "").splitlines()
            )
        if block_type == "CONCEPT_REF":
            return str(block.get("concept_key") or "")
        return str(block.get("markdown") or "")

    @staticmethod
    def _module_markdown(render_type: str, payload: dict[str, Any], plain_text: str) -> str:
        content_blocks = payload.get("blocks")
        if isinstance(content_blocks, list) and content_blocks:
            return "\n\n".join(
                item
                for item in (
                    SynthesisService._content_block_markdown(block)
                    for block in content_blocks
                    if isinstance(block, dict)
                )
                if item
            )
        if render_type == "LIST":
            return "\n".join(f"- {item}" for item in payload.get("items", []) if item)
        if render_type == "TABLE":
            headers = payload.get("table_headers", [])
            rows = payload.get("table_rows", [])
            if headers:
                return "\n".join(
                    [
                        "| " + " | ".join(headers) + " |",
                        "| " + " | ".join("---" for _ in headers) + " |",
                        *["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows],
                    ]
                )
        if render_type == "PROCEDURE":
            return "\n".join(
                f"{index}. {item}"
                for index, item in enumerate(payload.get("steps", []), 1)
            )
        if render_type == "CODE":
            language = payload.get("language", "")
            return f"```{language}\n{payload.get('code', '')}\n```"
        if render_type == "FORMULA":
            formula = payload.get("latex", "")
            explanation = payload.get("markdown", "")
            return "\n\n".join(
                item for item in [f"$$\n{formula}\n$$" if formula else "", explanation] if item
            )
        return str(payload.get("markdown") or plain_text)

    def _get_semantic_concept(self, session, concept, version) -> dict[str, Any]:
        placements = session.scalars(
            select(ConceptReleaseModule)
            .where(ConceptReleaseModule.concept_version_id == version.id)
            .order_by(ConceptReleaseModule.sort_order)
        ).all()
        blocks: list[dict[str, Any]] = []
        figures: list[dict[str, Any]] = []
        for placement in placements:
            module = session.get(ConceptModule, placement.module_id)
            module_version = session.get(ConceptModuleVersion, placement.module_version_id)
            if module is None or module_version is None:
                continue
            payload = _loads(module_version.payload_json, {})
            contribution_snapshot = payload.get("_contributions")
            if isinstance(contribution_snapshot, list):
                contribution_ids = [
                    str(item.get("contribution_id") or "")
                    for item in contribution_snapshot
                    if isinstance(item, dict) and item.get("contribution_id")
                ]
                contributions = session.scalars(
                    select(ConceptContribution)
                    .where(ConceptContribution.id.in_(contribution_ids))
                    .order_by(ConceptContribution.created_at)
                ).all() if contribution_ids else []
            else:
                contributions = session.scalars(
                    select(ConceptContribution)
                    .where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                    .order_by(ConceptContribution.created_at)
                ).all()
            # Legacy versions lack snapshots. Do not leak contributions added by a later merge.
            if not isinstance(contribution_snapshot, list):
                contributions = [
                    item for item in contributions
                    if not (item.rationale.startswith("Merged from ") and item.created_at > module_version.created_at)
                ]
            evidence: list[dict[str, Any]] = []
            contribution_payload: list[dict[str, Any]] = []
            for contribution in contributions:
                note_version = (
                    session.get(NoteVersion, contribution.atomic_note_version_id)
                    if contribution.atomic_note_version_id
                    else None
                )
                contribution_payload.append(
                    {
                        "contribution_id": contribution.id,
                        "role": contribution.contribution_role,
                        "source_role": contribution.source_role,
                        "source_ref": contribution.source_ref,
                        "note_version_id": contribution.atomic_note_version_id,
                        "note_title": note_version.title if note_version else "",
                    }
                )
                evidence.append(
                    {
                        "source_role": contribution.source_role,
                        "source_ref": contribution.source_ref,
                        "source_locator": {
                            "note_version_id": contribution.atomic_note_version_id,
                        },
                    }
                )
            assets = session.execute(
                select(ConceptModuleAsset, VisualAsset, BlobObject, SourcePage)
                .join(VisualAsset, VisualAsset.id == ConceptModuleAsset.visual_asset_id)
                .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
                .join(SourcePage, SourcePage.id == VisualAsset.page_id)
                .where(ConceptModuleAsset.module_version_id == module_version.id)
                .order_by(ConceptModuleAsset.order_index)
            ).all()
            asset_payload: list[dict[str, Any]] = []
            for asset_placement, visual, blob, page in assets:
                item = {
                    "asset_id": visual.id,
                    "caption": visual.caption or asset_placement.caption,
                    "alt_text": visual.caption or asset_placement.alt_text,
                    "page_number": page.page_index + 1,
                    "placement": asset_placement.placement,
                    "content_block_key": asset_placement.content_block_key,
                    "asset_url": f"/internal/v1/blobs/{blob.storage_key}",
                }
                asset_payload.append(item)
                figures.append(
                    {
                        "figure_id": visual.id,
                        "figure_label": module_version.title or module.module_key,
                        "caption": item["caption"],
                        "page_number": item["page_number"],
                        "width": None,
                        "height": None,
                        "asset_url": item["asset_url"],
                    }
                )
            precise_rows = session.execute(
                select(ConceptContentEvidence, EvidenceSpan, SourcePage)
                .join(EvidenceSpan, EvidenceSpan.id == ConceptContentEvidence.evidence_span_id)
                .join(SourcePage, SourcePage.id == EvidenceSpan.page_id)
                .where(ConceptContentEvidence.module_version_id == module_version.id)
                .order_by(SourcePage.page_index)
            ).all()
            precise_by_block: dict[str, list[dict[str, Any]]] = {}
            for precise, span, page in precise_rows:
                precise_by_block.setdefault(precise.content_block_key, []).append(
                    {
                        "contribution_id": precise.contribution_id,
                        "note_block_id": precise.note_block_id,
                        "evidence_span_id": span.id,
                        "page_number": page.page_index + 1,
                        "page_label": page.page_label,
                        "quote": span.quoted_text,
                        "evidence_level": span.evidence_level,
                    }
                )
            content_blocks = []
            for raw_block in payload.get("blocks", []):
                if not isinstance(raw_block, dict):
                    continue
                content_block = dict(raw_block)
                if content_block.get("symbols"):
                    content_block["symbols"] = [
                        {**item, "meaning": item.get("meaning") or item.get("description") or ""}
                        for item in content_block["symbols"] if isinstance(item, dict)
                    ]
                content_block["evidence_details"] = precise_by_block.get(
                    str(raw_block.get("block_key") or ""), []
                )
                content_blocks.append(content_block)
            blocks.append(
                {
                    "block_id": module.id,
                    "block_version_id": module_version.id,
                    "block_version_no": module_version.version_no,
                    "block_key": module.module_key,
                    "block_type": module_version.render_type,
                    "semantic_role": module.semantic_role,
                    "title": module_version.title,
                    "content": self._module_markdown(
                        module_version.render_type, payload, module_version.plain_text
                    ),
                    "content_json": payload,
                    "content_blocks": content_blocks,
                    "origin": module_version.origin,
                    "lock_state": module_version.lock_state,
                    "evidence": evidence,
                    "contributions": contribution_payload,
                    "assets": asset_payload,
                }
            )
        return {
            "concept_id": concept.id,
            "stable_key": concept.stable_key,
            "domain_key": concept.domain_key,
            "title": version.title,
            "original_title": version.original_title,
            "scope": _loads(
                version.scope,
                {"includes": [], "excludes": [], "prerequisites": [], "aliases": []},
            ),
            "summary": version.summary,
            "status": version.status,
            "schema_version": version.schema_version,
            "content_level": (
                "OVERVIEW" if len(blocks) == 1 and blocks[0]["semantic_role"] == "OVERVIEW"
                else "DETAIL"
            ),
            "version_id": version.id,
            "version_no": version.version_no,
            "figures": figures,
            "blocks": blocks,
        }
