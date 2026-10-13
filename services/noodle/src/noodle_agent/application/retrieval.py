from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..composition import Component
from ..db import (
    AtomicNote,
    BlobObject,
    Claim,
    ClaimEvidence,
    Concept,
    DocumentBlock,
    EvidenceSpan,
    Job,
    NoteAsset,
    NoteBlock,
    NoteEmbedding,
    NoteVersion,
    SourcePage,
    VisualAsset,
    insert_fts,
)
from ..domain import (
    Answer,
    JobStatus,
    SearchHit,
)
from ..synthesis.knowledge_sync import blocked_concepts, eligible_note
from .common import (
    _image_dimensions,
    _json,
    _parse_json,
    _sha256_text,
)


class RetrievalModule(Component):
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        fts_scores: dict[str, float] = {}
        match_query = self.owner._fts_query(query)
        with self.owner.db.session() as session:
            if match_query:
                rows = session.execute(
                    text(
                        """
                        SELECT f.note_version_id, bm25(note_fts, 1.0, 1.0, 0.7, 0.2) AS rank
                        FROM note_fts f
                        JOIN atomic_notes n ON n.id = f.note_id
                        WHERE note_fts MATCH :query
                          AND n.active_version_id = f.note_version_id
                          AND n.visibility = 'ACTIVE'
                          AND (:source_id IS NULL OR n.source_version_id = :source_id)
                        ORDER BY rank
                        LIMIT :limit
                        """
                    ),
                    {
                        "query": match_query,
                        "limit": max(limit * 4, 20),
                        "source_id": source_version_id,
                    },
                ).all()
                for version_id, rank in rows:
                    fts_scores[version_id] = -float(rank) / (1.0 - float(rank))

            embedded_candidates = session.execute(
                select(AtomicNote, NoteVersion, NoteEmbedding)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .join(NoteEmbedding, NoteEmbedding.note_version_id == NoteVersion.id)
                .where(
                    AtomicNote.visibility == "ACTIVE",
                    *(
                        [AtomicNote.source_version_id == source_version_id]
                        if source_version_id
                        else []
                    ),
                    NoteEmbedding.model_name == self.owner.embedding.model_name,
                )
            ).all()
            candidates: dict[str, tuple[AtomicNote, NoteVersion, NoteEmbedding | None]] = {
                version.id: (note, version, embedding)
                for note, version, embedding in embedded_candidates
            }
            if fts_scores:
                lexical_candidates = session.execute(
                    select(AtomicNote, NoteVersion)
                    .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                    .where(
                        AtomicNote.visibility == "ACTIVE",
                        *(
                            [AtomicNote.source_version_id == source_version_id]
                            if source_version_id
                            else []
                        ),
                        NoteVersion.id.in_(fts_scores),
                    )
                ).all()
                for note, version in lexical_candidates:
                    candidates.setdefault(version.id, (note, version, None))

            # A deployment may switch embedding providers before the asynchronous
            # re-index has finished.  Keep search usable during that window without
            # paying for a query embedding that cannot be compared with any stored
            # vector.  The lightweight title/thesis scan also covers CJK queries
            # that SQLite's default FTS tokenizer may not split as users expect.
            if not embedded_candidates:
                lexical_candidates = session.execute(
                    select(AtomicNote, NoteVersion)
                    .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                    .where(
                        AtomicNote.visibility == "ACTIVE",
                        *(
                            [AtomicNote.source_version_id == source_version_id]
                            if source_version_id
                            else []
                        ),
                    )
                ).all()
                for note, version in lexical_candidates:
                    candidates.setdefault(version.id, (note, version, None))

            query_vector = self.owner.embedding.embed([query])[0] if embedded_candidates else []
            scored: list[tuple[float, AtomicNote, NoteVersion]] = []
            for note, version, embedding in candidates.values():
                vector = _parse_json(embedding.vector_json, []) if embedding is not None else []
                cosine = self.owner._cosine(query_vector, vector)
                title_match = self.owner._lexical_similarity(query, version.title)
                thesis_match = self.owner._lexical_similarity(query, version.thesis)
                keyword = max(
                    fts_scores.get(version.id, 0.0),
                    title_match,
                    0.6 * thesis_match,
                )
                score = 0.55 * max(cosine, 0.0) + 0.45 * keyword
                if score > 0.0:
                    scored.append((score, note, version))
            scored.sort(key=lambda item: item[0], reverse=True)
            hits = []
            for score, note, version in (item for item in scored if item[0] >= min_score):
                if len(hits) >= limit:
                    break
                blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                hits.append(
                    SearchHit(
                        note_id=note.id,
                        note_version_id=version.id,
                        title=version.title,
                        thesis=version.thesis,
                        body="\n\n".join(block.content_text for block in blocks),
                        score=score,
                        evidence=self.owner._evidence_for_version(session, version.id),
                        assets=self.owner._assets_for_version(session, version.id),
                    )
                )
            return hits

    def search_knowledge(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        """Search both source-grounded AtomicNotes and reusable Concepts.

        Scores are produced by the same hybrid cosine/lexical recipe.  A small
        AtomicNote discount lets a canonical Concept win near-ties while still
        retaining source-level evidence in the result set.
        """
        query = query.strip()
        if not query:
            return []
        pool_size = max(limit * 3, 12)
        atomic_hits = self.owner.search(
            query, limit=pool_size, min_score=0.0, source_version_id=source_version_id
        )
        concept_hits = (
            []
            if source_version_id
            else self.owner.knowledge_curation.search_concepts(query, limit=pool_size)
        )
        atomic_ranked = [
            hit.model_copy(update={"score": round(hit.score * 0.97, 6)})
            for hit in atomic_hits
            if hit.score >= min_score
        ]
        concept_ranked = [hit for hit in concept_hits if hit.score >= min_score]
        if not atomic_ranked:
            return concept_ranked[:limit]
        if not concept_ranked:
            return atomic_ranked[:limit]

        # Concept and AtomicNote scores come from different corpora and should
        # not be treated as globally calibrated probabilities.  Interleaving
        # preserves both the canonical synthesis and page-level source layer.
        concept_slots = max(1, limit // 2)
        atomic_slots = max(1, limit - concept_slots)
        selected_concepts = concept_ranked[:concept_slots]
        selected_atomic = atomic_ranked[:atomic_slots]
        ranked: list[SearchHit] = []
        for index in range(max(len(selected_concepts), len(selected_atomic))):
            if index < len(selected_concepts):
                ranked.append(selected_concepts[index])
            if index < len(selected_atomic):
                ranked.append(selected_atomic[index])
        return ranked[:limit]

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        cleaned = [token.replace('"', "") for token in tokens if token]
        return " OR ".join(f'"{token}"' for token in cleaned[:12])

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        def normalize(value: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.casefold(), flags=re.UNICODE)

        left_value = normalize(left)
        right_value = normalize(right)
        if not left_value or not right_value:
            return 0.0
        if left_value in right_value or right_value in left_value:
            return 1.0

        def bigrams(value: str) -> set[str]:
            if len(value) == 1:
                return {value}
            return {value[index : index + 2] for index in range(len(value) - 1)}

        left_parts = bigrams(left_value)
        right_parts = bigrams(right_value)
        return 2.0 * len(left_parts & right_parts) / (len(left_parts) + len(right_parts))

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _evidence_for_version(session: Session, version_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(EvidenceSpan, SourcePage, Claim, ClaimEvidence)
            .join(ClaimEvidence, ClaimEvidence.span_id == EvidenceSpan.id)
            .join(Claim, Claim.id == ClaimEvidence.claim_id)
            .join(SourcePage, SourcePage.id == EvidenceSpan.page_id)
            .where(Claim.note_version_id == version_id)
            .order_by(SourcePage.page_index)
        ).all()
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for span, page, claim, link in rows:
            source_identity = span.document_block_id or f"{page.id}:{span.normalized_text_hash}"
            unique.setdefault(
                (claim.note_block_id, source_identity),
                {
                    "span_id": span.id,
                    "note_block_id": claim.note_block_id,
                    "page_index": page.page_index,
                    "page_label": page.page_label,
                    "quote": span.quoted_text,
                    "evidence_level": span.evidence_level,
                    "role": link.role,
                },
            )
        return list(unique.values())

    def _assets_for_version(self, session: Session, version_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(NoteAsset, VisualAsset, BlobObject, SourcePage, DocumentBlock)
            .join(VisualAsset, VisualAsset.id == NoteAsset.visual_asset_id)
            .join(BlobObject, BlobObject.id == VisualAsset.blob_id)
            .join(SourcePage, SourcePage.id == VisualAsset.page_id)
            .join(DocumentBlock, DocumentBlock.id == VisualAsset.document_block_id)
            .where(NoteAsset.note_version_id == version_id)
            .order_by(NoteAsset.order_index)
        ).all()
        return [
            {
                **_image_dimensions(str(self.owner.blobs.path_for(blob.storage_key))),
                "visual_asset_id": visual.id,
                "note_block_id": link.note_block_id,
                "placement": link.placement,
                "page_index": page.page_index,
                "page_label": page.page_label,
                "source_pages": _parse_json(block.metadata_json, {}).get(
                    "replaces_pages", [page.page_label]
                ),
                "origin": visual.extraction_method,
                "storage_key": blob.storage_key,
                "media_type": blob.media_type,
                "caption": visual.caption,
                "purpose": link.purpose,
                "alt_text": link.alt_text,
            }
            for link, visual, blob, page, block in rows
        ]

    def ask(self, question: str, *, limit: int = 5, source_version_id: str | None = None) -> Answer:
        hits = self.owner.search_knowledge(
            question,
            limit=limit,
            min_score=self.owner.settings.qa_min_relevance_score,
            source_version_id=source_version_id,
        )
        answer = self.owner.agent.answer(question, hits)
        with self.owner.db.session() as session:
            blocked = blocked_concepts(session)
            stale = False
            for hit in hits:
                if hit.entity_type == "CONCEPT":
                    concept = session.get(Concept, hit.concept_id)
                    stale = (
                        stale
                        or not concept
                        or concept.visibility != "ACTIVE"
                        or concept.id in blocked
                        or concept.active_version_id != hit.note_version_id
                    )
                else:
                    version = session.get(NoteVersion, hit.note_version_id)
                    stale = stale or not version or not eligible_note(session, version)
            if stale:
                return Answer(
                    answer="回答期间资料发生修改或删除，请在知识同步后重新提问。",
                    hits=[],
                    coverage_warning="SOURCE_CHANGED",
                )
            incomplete_count = session.scalar(
                select(func.count(Job.id)).where(
                    Job.status == JobStatus.COMPLETED_WITH_WARNINGS.value
                )
            )
        if incomplete_count and answer.coverage_warning is None:
            answer.coverage_warning = (
                f"知识库中有 {incomplete_count} 个文档作业带覆盖警告，答案可能未覆盖全部来源。"
            )
        return answer

    def rebuild_index(self) -> int:
        with self.owner.db.session() as session:
            session.execute(text("DELETE FROM note_fts"))
            rows = session.execute(
                select(AtomicNote, NoteVersion)
                .join(NoteVersion, NoteVersion.id == AtomicNote.active_version_id)
                .where(AtomicNote.visibility == "ACTIVE")
            ).all()
            count = 0
            embedding_inputs: list[str] = []
            embedding_records: list[tuple[NoteVersion, str]] = []
            for note, version in rows:
                blocks = session.scalars(
                    select(NoteBlock)
                    .where(NoteBlock.note_version_id == version.id)
                    .order_by(NoteBlock.order_index)
                ).all()
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=note.id,
                    title=version.title,
                    thesis=version.thesis,
                    body="\n\n".join(block.content_text for block in blocks),
                    tags=" ".join(_parse_json(version.tags_json, [])),
                )
                embedding_text = f"{version.title}\n{version.thesis}\n" + "\n\n".join(
                    block.content_text for block in blocks
                )
                embedding_inputs.append(embedding_text)
                embedding_records.append((version, _sha256_text(embedding_text)))
                count += 1
            vectors = self.owner.embedding.embed(embedding_inputs) if embedding_inputs else []
            for (version, content_hash), vector in zip(embedding_records, vectors, strict=True):
                current = session.scalar(
                    select(NoteEmbedding).where(
                        NoteEmbedding.note_version_id == version.id,
                        NoteEmbedding.model_name == self.owner.embedding.model_name,
                    )
                )
                if current is None:
                    current = NoteEmbedding(
                        note_version_id=version.id,
                        model_name=self.owner.embedding.model_name,
                        dimension=len(vector),
                        vector_json=_json(vector),
                        content_hash=content_hash,
                    )
                    session.add(current)
                else:
                    current.dimension = len(vector)
                    current.vector_json = _json(vector)
                    current.content_hash = content_hash
                    current.ready = True
            return count
