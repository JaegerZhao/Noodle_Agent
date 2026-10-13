from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from threading import Lock
from typing import Any

from sqlalchemy import func, select, text, update

from ..db import (
    BackboneCorpus,
    Concept,
    ConceptAlias,
    ConceptBlock,
    ConceptContentEvidence,
    ConceptContribution,
    ConceptEmbedding,
    ConceptMergeRecord,
    ConceptModule,
    ConceptModuleAsset,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptVersion,
    Database,
    EvidenceSpan,
    SourcePage,
    Synthesis,
    SynthesisNode,
    SynthesisReferenceSource,
    SynthesisVersion,
)
from ..domain import SearchHit
from .merge_content import (
    combine_blocks,
    content_blocks,
    merge_scope,
    merge_summary,
    remap_references,
)
from .service import SynthesisService


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:120] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class KnowledgeCurationService:
    """Generic Concept registry, Synthesis composer, merge and Concept RAG."""

    def __init__(self, database: Database, embedding: Any):
        self.db = database
        self.embedding = embedding
        self._indexed_signature = ""
        self._index_lock = Lock()

    @staticmethod
    def _concept_body(session, concept: Concept, version: ConceptVersion) -> str:
        placements = session.scalars(
            select(ConceptReleaseModule)
            .where(ConceptReleaseModule.concept_version_id == version.id)
            .order_by(ConceptReleaseModule.sort_order)
        ).all()
        if placements:
            parts: list[str] = []
            for placement in placements:
                module = session.get(ConceptModule, placement.module_id)
                module_version = session.get(ConceptModuleVersion, placement.module_version_id)
                if module_version is None:
                    continue
                parts.extend(
                    item
                    for item in [
                        module_version.title,
                        module.semantic_role if module else "",
                        module_version.plain_text,
                    ]
                    if item
                )
            return "\n\n".join(parts)
        blocks = session.scalars(
            select(ConceptBlock)
            .where(ConceptBlock.concept_version_id == version.id)
            .order_by(ConceptBlock.sort_order)
        ).all()
        return "\n\n".join(block.content_text for block in blocks if block.content_text)

    @staticmethod
    def _active_synthesis_paths(session, concept_id: str) -> list[str]:
        paths: list[str] = []
        syntheses = session.scalars(
            select(Synthesis).where(
                Synthesis.visibility == "ACTIVE",
                Synthesis.active_version_id.is_not(None),
            )
        ).all()
        for synthesis in syntheses:
            rows = session.scalars(
                select(SynthesisNode)
                .where(SynthesisNode.synthesis_version_id == synthesis.active_version_id)
                .order_by(SynthesisNode.sort_order)
            ).all()
            by_id = {row.id: row for row in rows}
            for row in rows:
                if row.concept_id != concept_id:
                    continue
                chain = [row.title]
                parent_id = row.parent_id
                while parent_id and parent_id in by_id:
                    parent = by_id[parent_id]
                    chain.append(parent.title)
                    parent_id = parent.parent_id
                paths.append(f"{synthesis.title} / " + " / ".join(reversed(chain)))
        return sorted(set(paths))

    def list_concepts(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        needle = normalize_alias(query)
        with self.db.session() as session:
            concepts = session.scalars(
                select(Concept)
                .where(Concept.visibility == "ACTIVE", Concept.active_version_id.is_not(None))
                .order_by(Concept.canonical_name)
            ).all()
            rows: list[tuple[float, dict[str, Any]]] = []
            for concept in concepts:
                version = session.get(ConceptVersion, concept.active_version_id)
                if version is None or version.status != "PUBLISHED":
                    continue
                scope = _loads(version.scope, {})
                aliases = [
                    item.alias
                    for item in session.scalars(
                        select(ConceptAlias).where(ConceptAlias.concept_id == concept.id)
                    ).all()
                ]
                aliases.extend(scope.get("aliases", []) if isinstance(scope, dict) else [])
                haystacks = [
                    normalize_alias(item)
                    for item in [
                        concept.stable_key,
                        concept.canonical_name,
                        concept.original_name,
                        version.title,
                        *aliases,
                    ]
                    if item
                ]
                if needle and not any(needle in item or item in needle for item in haystacks):
                    continue
                score = 1.0 if needle and any(needle == item for item in haystacks) else 0.7
                paths = self._active_synthesis_paths(session, concept.id)
                rows.append(
                    (
                        score,
                        {
                            "concept_id": concept.id,
                            "stable_key": concept.stable_key,
                            "version_id": version.id,
                            "version_no": version.version_no,
                            "title": version.title,
                            "summary": version.summary,
                            "aliases": sorted({str(item) for item in aliases if item}),
                            "synthesis_paths": paths,
                            "synthesis_count": len({item.split(" / ", 1)[0] for item in paths}),
                            "score": score,
                        },
                    )
                )
            rows.sort(key=lambda item: (-item[0], item[1]["title"]))
            return [item for _score, item in rows[: max(1, min(limit, 200))]]

    @staticmethod
    def _normalize_outline(session, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_keys: set[str] = set()
        seen_concepts: set[str] = set()

        def visit(raw: dict[str, Any]) -> dict[str, Any]:
            node_kind = str(raw.get("node_kind") or "CONCEPT").upper()
            if node_kind not in {"GROUP", "CONCEPT"}:
                raise ValueError("Synthesis node_kind must be GROUP or CONCEPT")
            title = str(raw.get("title") or "").strip()
            if not title:
                raise ValueError("Synthesis node title must not be blank")
            stable_key = str(raw.get("stable_key") or _slug(title)).strip()
            if stable_key in seen_keys:
                raise ValueError(f"Duplicate Synthesis stable_key: {stable_key}")
            seen_keys.add(stable_key)
            concept_id = str(raw.get("concept_id") or "").strip()
            concept_version_id = ""
            if node_kind == "CONCEPT":
                concept = session.get(Concept, concept_id) if concept_id else None
                if (
                    concept is None
                    or concept.visibility != "ACTIVE"
                    or not concept.active_version_id
                ):
                    raise ValueError(f"Unknown active Concept: {concept_id}")
                if concept.id in seen_concepts:
                    raise ValueError(
                        f"Concept appears more than once in one Synthesis: {concept.id}"
                    )
                seen_concepts.add(concept.id)
                concept_id = concept.id
                concept_version_id = concept.active_version_id
            elif concept_id:
                raise ValueError("GROUP nodes cannot reference a Concept")
            children = raw.get("children") or []
            if not isinstance(children, list):
                raise TypeError("Synthesis children must be a list")
            if node_kind == "CONCEPT" and children:
                raise ValueError("CONCEPT nodes must be leaves; use a GROUP for hierarchy")
            return {
                "stable_key": stable_key,
                "title": title,
                "node_kind": node_kind,
                "concept_id": concept_id,
                "concept_version_id": concept_version_id,
                "children": [visit(item) for item in children],
            }

        return [visit(item) for item in nodes]

    @staticmethod
    def _coverage(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        flat: list[dict[str, Any]] = []

        def visit(items: list[dict[str, Any]]) -> None:
            for item in items:
                flat.append(item)
                visit(item["children"])

        visit(nodes)
        concepts = sum(item["node_kind"] == "CONCEPT" for item in flat)
        groups = sum(item["node_kind"] == "GROUP" for item in flat)
        return {
            "concept_nodes": concepts,
            "published_concepts": concepts,
            "groups": groups,
            "coverage": 1.0 if concepts else 0.0,
            "curated": True,
        }

    @staticmethod
    def _persist_nodes(session, version_id: str, nodes: list[dict[str, Any]]) -> None:
        order = 0

        def visit(items: list[dict[str, Any]], parent_id: str | None, level: int) -> None:
            nonlocal order
            for item in items:
                row = SynthesisNode(
                    synthesis_version_id=version_id,
                    stable_key=item["stable_key"],
                    parent_id=parent_id,
                    title=item["title"],
                    original_title=item["title"],
                    level=level,
                    sort_order=order,
                    node_kind=item["node_kind"],
                    concept_id=item["concept_id"] or None,
                    concept_version_id=item["concept_version_id"] or None,
                    source_locator_json="{}",
                )
                order += 1
                session.add(row)
                session.flush()
                visit(item["children"], row.id, level + 1)

        visit(nodes, None, 1)

    def create_synthesis(
        self,
        *,
        stable_key: str,
        domain_key: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        stable_key = stable_key.strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,159}", stable_key):
            raise ValueError("stable_key must contain 3-160 lowercase letters, digits or hyphens")
        with self.db.session() as session:
            if session.scalar(select(Synthesis).where(Synthesis.stable_key == stable_key)):
                raise ValueError(f"Synthesis stable_key already exists: {stable_key}")
            normalized = self._normalize_outline(session, outline)
            manifest = {"origin": "CURATED", "created_by": actor, "stable_key": stable_key}
            corpus = BackboneCorpus(
                stable_key=f"curated:{stable_key}",
                domain_key=domain_key.strip() or stable_key,
                title=title.strip(),
                authors_json="[]",
                edition_label="curated-v1",
                language="zh",
                source_url="",
                license_note="Personal curated knowledge system",
                local_path="",
                content_sha256=_sha256(_json(manifest)),
                page_count=0,
                manifest_json=_json(manifest),
                status="READY",
            )
            session.add(corpus)
            session.flush()
            synthesis = Synthesis(
                stable_key=stable_key,
                domain_key=domain_key.strip() or stable_key,
                title=title.strip(),
                description=description.strip(),
            )
            session.add(synthesis)
            session.flush()
            coverage = self._coverage(normalized)
            version = SynthesisVersion(
                synthesis_id=synthesis.id,
                backbone_corpus_id=corpus.id,
                version_no=1,
                title=f"{title.strip()} · 自定义版",
                summary=description.strip(),
                status="PUBLISHED",
                coverage_json=_json(coverage),
                content_sha256=_sha256(_json(normalized)),
            )
            session.add(version)
            session.flush()
            self._persist_nodes(session, version.id, normalized)
            synthesis.active_version_id = version.id
            return {
                "status": "PUBLISHED",
                "synthesis_id": synthesis.id,
                "version_id": version.id,
                "version_no": 1,
                "coverage": coverage,
            }

    def publish_synthesis(
        self,
        *,
        synthesis_id: str,
        expected_version_id: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        del actor
        with self.db.session() as session:
            synthesis = session.get(Synthesis, synthesis_id)
            if synthesis is None or synthesis.visibility != "ACTIVE":
                raise ValueError(f"Unknown synthesis_id: {synthesis_id}")
            if synthesis.active_version_id != expected_version_id:
                raise ValueError("Synthesis version conflict: reload before publishing")
            current = session.get(SynthesisVersion, expected_version_id)
            if current is None:
                raise ValueError("Synthesis active version is missing")
            normalized = self._normalize_outline(session, outline)
            coverage = self._coverage(normalized)
            release_hash = _sha256(
                _json(
                    {
                        "title": title.strip(),
                        "description": description.strip(),
                        "outline": normalized,
                    }
                )
            )
            existing = session.scalar(
                select(SynthesisVersion).where(
                    SynthesisVersion.synthesis_id == synthesis.id,
                    SynthesisVersion.content_sha256 == release_hash,
                )
            )
            if existing is not None:
                synthesis.active_version_id = existing.id
                synthesis.title = title.strip()
                synthesis.description = description.strip()
                return {
                    "status": "UNCHANGED",
                    "synthesis_id": synthesis.id,
                    "version_id": existing.id,
                    "version_no": existing.version_no,
                    "coverage": _loads(existing.coverage_json, {}),
                }
            version_no = (
                session.scalar(
                    select(func.max(SynthesisVersion.version_no)).where(
                        SynthesisVersion.synthesis_id == synthesis.id
                    )
                )
                or 0
            ) + 1
            version = SynthesisVersion(
                synthesis_id=synthesis.id,
                backbone_corpus_id=current.backbone_corpus_id,
                version_no=version_no,
                title=f"{title.strip()} · 自定义版",
                summary=description.strip(),
                status="PUBLISHED",
                coverage_json=_json(coverage),
                content_sha256=release_hash,
            )
            session.add(version)
            session.flush()
            self._persist_nodes(session, version.id, normalized)
            synthesis.title = title.strip()
            synthesis.description = description.strip()
            synthesis.active_version_id = version.id
            return {
                "status": "PUBLISHED",
                "synthesis_id": synthesis.id,
                "version_id": version.id,
                "version_no": version.version_no,
                "coverage": coverage,
            }

    def ensure_concept_index(self, *, force: bool = False) -> int:
        # Build only the lexical index on the read path. Never hold a SQLite
        # writer transaction while waiting for an external embedding service.
        with self._index_lock:
            with self.db.session() as session:
                rows = session.execute(
                    select(Concept, ConceptVersion)
                    .join(ConceptVersion, ConceptVersion.id == Concept.active_version_id)
                    .where(Concept.visibility == "ACTIVE", ConceptVersion.status == "PUBLISHED")
                    .order_by(Concept.id)
                ).all()
                signature = _sha256(_json([(c.id, v.id, v.content_sha256) for c, v in rows]))
                from .knowledge_sync import blocked_concepts

                blocked = blocked_concepts(session)
                rows = [(c, v) for c, v in rows if c.id not in blocked]
                signature = _sha256(_json([(c.id, v.id, v.content_sha256) for c, v in rows]))
                if not force and signature == self._indexed_signature:
                    return len(rows)
                prepared = []
                missing = []
                for concept, version in rows:
                    aliases = [
                        item.alias
                        for item in session.scalars(
                            select(ConceptAlias).where(ConceptAlias.concept_id == concept.id)
                        ).all()
                    ]
                    scope = _loads(version.scope, {})
                    if isinstance(scope, dict):
                        aliases.extend(str(item) for item in scope.get("aliases", []))
                    body = self._concept_body(session, concept, version)
                    alias_text = " ".join(sorted(set(aliases)))
                    index_text = f"{version.title}\n{version.summary}\n{body}\n{alias_text}"
                    content_hash = _sha256(index_text)
                    prepared.append(
                        {
                            "version_id": version.id,
                            "concept_id": concept.id,
                            "title": version.title,
                            "summary": version.summary,
                            "body": body,
                            "aliases": alias_text,
                        }
                    )
                    if (
                        force
                        and session.scalar(
                            select(ConceptEmbedding.id).where(
                                ConceptEmbedding.concept_version_id == version.id,
                                ConceptEmbedding.model_name == self.embedding.model_name,
                                ConceptEmbedding.content_sha256 == content_hash,
                            )
                        )
                        is None
                    ):
                        missing.append((version.id, index_text, content_hash))
            with self.db.session() as session:
                session.execute(text("DELETE FROM concept_fts"))
                for record in prepared:
                    session.execute(
                        text(
                            "INSERT INTO concept_fts(concept_version_id, concept_id, title, summary, body, aliases) VALUES (:version_id, :concept_id, :title, :summary, :body, :aliases)"
                        ),
                        record,
                    )
            self._indexed_signature = signature
        if missing:
            try:
                vectors = self.embedding.embed([item[1] for item in missing])
            except Exception:  # noqa: BLE001
                vectors = []
            if vectors and len(vectors) != len(missing):
                raise ValueError("Embedding provider returned an unexpected vector count")
            with self.db.session() as session:
                for (version_id, _index_text, content_hash), vector in zip(missing, vectors):
                    exists = session.scalar(
                        select(ConceptEmbedding.id).where(
                            ConceptEmbedding.concept_version_id == version_id,
                            ConceptEmbedding.model_name == self.embedding.model_name,
                            ConceptEmbedding.content_sha256 == content_hash,
                        )
                    )
                    if exists is None:
                        session.add(
                            ConceptEmbedding(
                                concept_version_id=version_id,
                                model_name=self.embedding.model_name,
                                dimensions=len(vector),
                                vector_json=_json(vector),
                                content_sha256=content_hash,
                            )
                        )
        return len(prepared)

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{item.replace(chr(34), "")}"' for item in tokens[:12] if item)

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        left_value = normalize_alias(left)
        right_value = normalize_alias(right)
        if not left_value or not right_value:
            return 0.0
        if left_value in right_value or right_value in left_value:
            return 1.0
        left_parts = {left_value[index : index + 2] for index in range(max(1, len(left_value) - 1))}
        right_parts = {
            right_value[index : index + 2] for index in range(max(1, len(right_value) - 1))
        }
        return 2.0 * len(left_parts & right_parts) / max(1, len(left_parts) + len(right_parts))

    def search_concepts(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        self.ensure_concept_index()
        with self.db.session() as session:
            lexical_scores: dict[str, float] = {}
            fts_query = self._fts_query(query)
            if fts_query:
                for version_id, rank in session.execute(
                    text(
                        "SELECT concept_version_id, bm25(concept_fts, 1.0, 1.0, 0.7, 0.3) FROM concept_fts WHERE concept_fts MATCH :query ORDER BY bm25(concept_fts, 1.0, 1.0, 0.7, 0.3) LIMIT :limit"
                    ),
                    {"query": fts_query, "limit": max(limit * 4, 30)},
                ).all():
                    lexical_scores[version_id] = -float(rank) / (1.0 - float(rank))
            embedded = session.execute(
                select(Concept, ConceptVersion, ConceptEmbedding)
                .join(ConceptVersion, ConceptVersion.id == Concept.active_version_id)
                .join(ConceptEmbedding, ConceptEmbedding.concept_version_id == ConceptVersion.id)
                .where(
                    Concept.visibility == "ACTIVE",
                    ConceptEmbedding.model_name == self.embedding.model_name,
                )
            ).all()
            embedding_by_version = {
                version.id: embedding for _concept, version, embedding in embedded
            }
            try:
                query_vector = self.embedding.embed([query])[0] if embedded else []
            except Exception:  # noqa: BLE001 - provider outages must preserve lexical search.
                query_vector = []
            scored: list[tuple[float, Concept, ConceptVersion]] = []
            active = session.execute(
                select(Concept, ConceptVersion)
                .join(ConceptVersion, ConceptVersion.id == Concept.active_version_id)
                .where(Concept.visibility == "ACTIVE", ConceptVersion.status == "PUBLISHED")
            ).all()
            from .knowledge_sync import blocked_concepts

            blocked = blocked_concepts(session)
            for concept, version in active:
                if concept.id in blocked:
                    continue
                embedding = embedding_by_version.get(version.id)
                vector = _loads(embedding.vector_json, []) if embedding is not None else []
                cosine = (
                    sum(left * right for left, right in zip(query_vector, vector, strict=True))
                    if query_vector and len(query_vector) == len(vector)
                    else 0.0
                )
                keyword = max(
                    lexical_scores.get(version.id, 0.0),
                    self._lexical_similarity(query, version.title),
                    0.6 * self._lexical_similarity(query, version.summary),
                )
                score = 0.58 * max(cosine, 0.0) + 0.42 * keyword
                if score > 0:
                    scored.append((score, concept, version))
            scored.sort(key=lambda item: item[0], reverse=True)
            hits: list[SearchHit] = []
            for score, concept, version in scored[: max(1, limit)]:
                body = self._concept_body(session, concept, version)
                evidence_rows = session.execute(
                    select(ConceptContentEvidence, EvidenceSpan, SourcePage)
                    .select_from(ConceptContentEvidence)
                    .join(EvidenceSpan, EvidenceSpan.id == ConceptContentEvidence.evidence_span_id)
                    .join(SourcePage, SourcePage.id == EvidenceSpan.page_id)
                    .join(
                        ConceptReleaseModule,
                        ConceptReleaseModule.module_version_id
                        == ConceptContentEvidence.module_version_id,
                    )
                    .where(ConceptReleaseModule.concept_version_id == version.id)
                    .order_by(SourcePage.page_index)
                ).all()
                evidence = [
                    {
                        "span_id": span.id,
                        "page_index": page.page_index,
                        "page_label": page.page_label,
                        "quote": span.quoted_text,
                        "evidence_level": span.evidence_level,
                        "role": "SUPPORT",
                    }
                    for _link, span, page in evidence_rows[:20]
                ]
                hits.append(
                    SearchHit(
                        note_id=f"concept:{concept.id}",
                        note_version_id=version.id,
                        title=version.title,
                        thesis=version.summary,
                        body=body,
                        score=round(score, 6),
                        entity_type="CONCEPT",
                        concept_id=concept.id,
                        synthesis_paths=self._active_synthesis_paths(session, concept.id),
                        evidence=evidence,
                    )
                )
            return hits

    def resolve_concept_id(self, concept_id: str) -> str:
        with self.db.session() as session:
            concept = session.get(Concept, concept_id)
            if concept is None or concept.visibility == "ACTIVE":
                return concept_id
            merge = session.scalar(
                select(ConceptMergeRecord)
                .where(
                    ConceptMergeRecord.source_concept_id == concept.id,
                    ConceptMergeRecord.status == "MERGED",
                )
                .order_by(ConceptMergeRecord.created_at.desc())
            )
            return merge.target_concept_id if merge else concept_id

    def preview_merge(
        self,
        *,
        source_concept_id: str,
        target_concept_id: str,
        expected_source_version_id: str,
        expected_target_version_id: str,
    ) -> dict[str, Any]:
        if source_concept_id == target_concept_id:
            raise ValueError("Source and target Concept must differ")
        with self.db.session() as session:
            source = session.get(Concept, source_concept_id)
            target = session.get(Concept, target_concept_id)
            if (
                not source
                or not target
                or source.visibility != "ACTIVE"
                or target.visibility != "ACTIVE"
            ):
                raise ValueError("Unknown active Concept")
            if (
                source.active_version_id != expected_source_version_id
                or target.active_version_id != expected_target_version_id
            ):
                raise ValueError("Concept version conflict: reload before merging")
            sv = session.get(ConceptVersion, expected_source_version_id)
            tv = session.get(ConceptVersion, expected_target_version_id)
            if (
                not sv
                or not tv
                or sv.schema_version not in {"concept-v2", "concept-v3"}
                or tv.schema_version not in {"concept-v2", "concept-v3"}
            ):
                raise ValueError("Both Concepts must be migrated to Concept V2/V3 before merge")
            source_scope = _loads(sv.scope, {})
            target_scope = _loads(tv.scope, {})
            source_scope = source_scope if isinstance(source_scope, dict) else {}
            target_scope = target_scope if isinstance(target_scope, dict) else {}
            scope = merge_scope(
                target_scope,
                source_scope,
                [source.canonical_name, source.original_name, source.stable_key],
            )
            target_names = {
                normalize_alias(name)
                for name in [target.canonical_name, *target_scope.get("aliases", [])]
            }
            source_names = {
                normalize_alias(name)
                for name in [source.canonical_name, *source_scope.get("aliases", [])]
            }
            targets = {}
            for placement in session.scalars(
                select(ConceptReleaseModule).where(ConceptReleaseModule.concept_version_id == tv.id)
            ).all():
                module = session.get(ConceptModule, placement.module_id)
                mv = session.get(ConceptModuleVersion, placement.module_version_id)
                if module and mv:
                    targets[(module.module_key, module.semantic_role, mv.render_type)] = mv
            changes = []
            for placement in session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == sv.id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all():
                module = session.get(ConceptModule, placement.module_id)
                mv = session.get(ConceptModuleVersion, placement.module_version_id)
                if not module or not mv:
                    continue
                existing = targets.get((module.module_key, module.semantic_role, mv.render_type))
                dedup = 0
                action = "APPEND"
                if existing and existing.lock_state == "OPEN":
                    blocks, _, dedup = combine_blocks(
                        content_blocks(
                            _loads(existing.payload_json, {}),
                            existing.plain_text,
                            existing.render_type,
                        ),
                        content_blocks(_loads(mv.payload_json, {}), mv.plain_text, mv.render_type),
                    )
                    if len(blocks) <= 40:
                        action = "COMBINE"
                    else:
                        dedup = 0
                changes.append(
                    {
                        "title": mv.title,
                        "module_key": module.module_key,
                        "action": action,
                        "identical_blocks": dedup,
                    }
                )
            return {
                "source_title": sv.title,
                "target_title": tv.title,
                "relation": "SHARED_NAME"
                if target_names & source_names
                else "REQUIRES_HUMAN_JUDGMENT",
                "scope_before": target_scope,
                "scope_after": scope,
                "summary_after": merge_summary(tv.summary, sv.summary),
                "modules": changes,
                "warning": "Only identical content is deduplicated. Related or parent/child topics should remain separate; contradictory claims require review.",
            }

    def merge_concepts(
        self,
        *,
        source_concept_id: str,
        target_concept_id: str,
        expected_source_version_id: str,
        expected_target_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        if source_concept_id == target_concept_id:
            raise ValueError("Source and target Concept must differ")
        with self.db.session() as session:
            source = session.get(Concept, source_concept_id)
            target = session.get(Concept, target_concept_id)
            if not source or source.visibility != "ACTIVE":
                raise ValueError(f"Unknown active source Concept: {source_concept_id}")
            if not target or target.visibility != "ACTIVE":
                raise ValueError(f"Unknown active target Concept: {target_concept_id}")
            if source.active_version_id != expected_source_version_id:
                raise ValueError("Source Concept version conflict: reload before merging")
            if target.active_version_id != expected_target_version_id:
                raise ValueError("Target Concept version conflict: reload before merging")
            source_version = session.get(ConceptVersion, expected_source_version_id)
            target_version = session.get(ConceptVersion, expected_target_version_id)
            if not source_version or not target_version:
                raise ValueError("Concept active version is missing")
            if source_version.schema_version not in {
                "concept-v2",
                "concept-v3",
            } or target_version.schema_version not in {"concept-v2", "concept-v3"}:
                raise ValueError("Both Concepts must be migrated to Concept V2/V3 before merge")
            target_placements = session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == target_version.id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all()
            source_placements = session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == source_version.id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all()
            target_scope = _loads(target_version.scope, {})
            source_scope = _loads(source_version.scope, {})
            target_scope = merge_scope(
                target_scope if isinstance(target_scope, dict) else {},
                source_scope if isinstance(source_scope, dict) else {},
                [source.canonical_name, source.original_name, source.stable_key],
            )
            release_modules = [
                (item.module_id, item.module_version_id) for item in target_placements
            ]
            modules_by_key = {}
            for placement in target_placements:
                module = session.get(ConceptModule, placement.module_id)
                module_version = session.get(ConceptModuleVersion, placement.module_version_id)
                if module and module_version:
                    modules_by_key[
                        (module.module_key, module.semantic_role, module_version.render_type)
                    ] = (module, module_version)
            used_keys = {item[0].module_key for item in modules_by_key.values()}
            deduplicated_blocks = 0
            combined_modules = 0
            for placement in source_placements:
                source_module = session.get(ConceptModule, placement.module_id)
                source_mv = session.get(ConceptModuleVersion, placement.module_version_id)
                if not source_module or not source_mv:
                    continue
                source_payload = _loads(source_mv.payload_json, {})
                source_blocks = content_blocks(
                    source_payload, source_mv.plain_text, source_mv.render_type
                )
                matched = modules_by_key.get(
                    (source_module.module_key, source_module.semantic_role, source_mv.render_type)
                )
                if matched and matched[1].lock_state != "OPEN":
                    matched = None
                if matched:
                    target_payload = _loads(matched[1].payload_json, {})
                    target_blocks = content_blocks(
                        target_payload, matched[1].plain_text, matched[1].render_type
                    )
                    if len(combine_blocks(target_blocks, source_blocks)[0]) > 40:
                        matched = None
                if matched:
                    module, old_mv = matched
                    combined_modules += 1
                else:
                    key = source_module.module_key
                    if key in used_keys:
                        key = f"merged-{_slug(source.stable_key)}-{key}"[:170]
                    base_key, suffix = key, 2
                    while key in used_keys:
                        key = f"{base_key}-{suffix}"
                        suffix += 1
                    used_keys.add(key)
                    module = ConceptModule(
                        concept_id=target.id,
                        module_key=key,
                        semantic_role=source_module.semantic_role,
                        visibility="ACTIVE",
                    )
                    session.add(module)
                    session.flush()
                    old_mv = None
                    target_payload, target_blocks = {}, []
                existing = session.scalars(
                    select(ConceptContribution).where(
                        ConceptContribution.module_id == module.id,
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                ).all()
                existing_by_note = {
                    (item.atomic_note_version_id or item.source_ref, item.contribution_role): item
                    for item in existing
                }
                source_contributions = session.scalars(
                    select(ConceptContribution).where(
                        ConceptContribution.module_id == source_module.id,
                        ConceptContribution.decision_status == "ACCEPTED",
                    )
                ).all()
                pinned = source_payload.get("_contributions")
                if isinstance(pinned, list):
                    pinned_ids = {
                        str(item.get("contribution_id"))
                        for item in pinned
                        if isinstance(item, dict)
                    }
                    source_contributions = [
                        item for item in source_contributions if item.id in pinned_ids
                    ]
                contribution_map = {}
                for old in source_contributions:
                    identity = (old.atomic_note_version_id or old.source_ref, old.contribution_role)
                    copied = existing_by_note.get(identity)
                    if copied is None:
                        copied = ConceptContribution(
                            module_id=module.id,
                            atomic_note_version_id=old.atomic_note_version_id,
                            source_role=old.source_role,
                            source_ref=old.source_ref,
                            contribution_role=old.contribution_role,
                            decision_status="ACCEPTED",
                            rationale=f"Merged from {source.stable_key}: {rationale.strip()}",
                            assigned_by=actor,
                            decided_at=old.decided_at,
                        )
                        session.add(copied)
                        session.flush()
                        existing_by_note[identity] = copied
                    contribution_map[old.id] = copied.id
                source_blocks = remap_references(source_blocks, contribution_map)
                merged_blocks, block_map, removed = combine_blocks(target_blocks, source_blocks)
                deduplicated_blocks += removed
                snapshot = target_payload.get("_contributions")
                if not isinstance(snapshot, list):
                    snapshot = [
                        {
                            "contribution_id": item.id,
                            "note_version_id": item.atomic_note_version_id,
                            "role": item.contribution_role,
                        }
                        for item in existing
                    ]
                snapshot_by_id = {
                    str(item["contribution_id"]): item
                    for item in snapshot
                    if isinstance(item, dict) and item.get("contribution_id")
                }
                for old in source_contributions:
                    copied_id = contribution_map[old.id]
                    snapshot_by_id[copied_id] = {
                        "contribution_id": copied_id,
                        "note_version_id": old.atomic_note_version_id,
                        "role": old.contribution_role,
                    }
                allowed = set(snapshot_by_id)
                if any(
                    not set(block.get("evidence_refs") or []) <= allowed for block in merged_blocks
                ):
                    raise ValueError("Merged content references a missing or inactive contribution")
                payload = {"blocks": merged_blocks, "_contributions": list(snapshot_by_id.values())}
                plain_text = "\n\n".join(
                    SynthesisService._content_block_markdown(block) for block in merged_blocks
                )
                module_no = (
                    session.scalar(
                        select(func.max(ConceptModuleVersion.version_no)).where(
                            ConceptModuleVersion.module_id == module.id
                        )
                    )
                    or 0
                ) + 1
                module_version = ConceptModuleVersion(
                    module_id=module.id,
                    version_no=module_no,
                    render_type=source_mv.render_type,
                    title=old_mv.title if old_mv else source_mv.title,
                    payload_json=_json(payload),
                    plain_text=plain_text,
                    origin="MERGED_CONCEPT",
                    lock_state="OPEN",
                    content_sha256=_sha256(_json(payload)),
                    created_by=actor,
                )
                session.add(module_version)
                session.flush()
                seen_assets, seen_evidence = set(), set()
                for previous, key_mapping, ref_mapping in [
                    (old_mv, {}, {}),
                    (source_mv, block_map, contribution_map),
                ]:
                    if previous is None:
                        continue
                    for asset in session.scalars(
                        select(ConceptModuleAsset).where(
                            ConceptModuleAsset.module_version_id == previous.id
                        )
                    ).all():
                        if asset.visual_asset_id in seen_assets:
                            continue
                        seen_assets.add(asset.visual_asset_id)
                        session.add(
                            ConceptModuleAsset(
                                module_version_id=module_version.id,
                                visual_asset_id=asset.visual_asset_id,
                                content_block_key=key_mapping.get(
                                    asset.content_block_key, asset.content_block_key
                                ),
                                order_index=len(seen_assets) - 1,
                                placement=asset.placement,
                                caption=asset.caption,
                                alt_text=asset.alt_text,
                            )
                        )
                    for evidence in session.scalars(
                        select(ConceptContentEvidence).where(
                            ConceptContentEvidence.module_version_id == previous.id
                        )
                    ).all():
                        key = key_mapping.get(
                            evidence.content_block_key, evidence.content_block_key
                        )
                        identity = (key, evidence.evidence_span_id)
                        mapped_id = ref_mapping.get(
                            evidence.contribution_id, evidence.contribution_id
                        )
                        if identity in seen_evidence or mapped_id not in allowed:
                            continue
                        seen_evidence.add(identity)
                        session.add(
                            ConceptContentEvidence(
                                module_version_id=module_version.id,
                                content_block_key=key,
                                contribution_id=mapped_id,
                                note_block_id=evidence.note_block_id,
                                evidence_span_id=evidence.evidence_span_id,
                            )
                        )
                if old_mv:
                    release_modules = [
                        (mid, module_version.id if mid == module.id else vid)
                        for mid, vid in release_modules
                    ]
                else:
                    release_modules.append((module.id, module_version.id))
                modules_by_key[
                    (module.module_key, module.semantic_role, module_version.render_type)
                ] = (module, module_version)
            version_no = (
                session.scalar(
                    select(func.max(ConceptVersion.version_no)).where(
                        ConceptVersion.concept_id == target.id
                    )
                )
                or 0
            ) + 1
            release_hash = _sha256(
                _json(
                    {
                        "target": target.id,
                        "source": source.id,
                        "modules": release_modules,
                        "scope": target_scope,
                        "summary": merge_summary(target_version.summary, source_version.summary),
                    }
                )
            )
            merged = ConceptVersion(
                concept_id=target.id,
                version_no=version_no,
                title=target_version.title,
                original_title=target_version.original_title,
                scope=_json(target_scope),
                summary=merge_summary(target_version.summary, source_version.summary),
                content_sha256=release_hash,
                schema_version="concept-v3",
                prompt_version="concept-merge-v2",
                status="PUBLISHED",
            )
            session.add(merged)
            session.flush()
            for index, (module_id, module_version_id) in enumerate(release_modules):
                session.add(
                    ConceptReleaseModule(
                        concept_version_id=merged.id,
                        module_id=module_id,
                        module_version_id=module_version_id,
                        sort_order=index,
                    )
                )
            for alias in [source.stable_key, source.canonical_name, source.original_name]:
                normalized = normalize_alias(alias)
                if not normalized:
                    continue
                existing_alias = session.scalar(
                    select(ConceptAlias).where(ConceptAlias.normalized_alias == normalized)
                )
                if existing_alias and existing_alias.concept_id not in {source.id, target.id}:
                    continue
                if existing_alias:
                    existing_alias.concept_id = target.id
                    existing_alias.alias_type = "MERGED"
                else:
                    session.add(
                        ConceptAlias(
                            concept_id=target.id,
                            alias=alias,
                            normalized_alias=normalized,
                            alias_type="MERGED",
                            created_by=actor,
                        )
                    )
            target_cas = session.execute(
                update(Concept)
                .where(
                    Concept.id == target.id,
                    Concept.active_version_id == expected_target_version_id,
                    Concept.visibility == "ACTIVE",
                )
                .values(active_version_id=merged.id, status="PUBLISHED")
            )
            source_cas = session.execute(
                update(Concept)
                .where(
                    Concept.id == source.id,
                    Concept.active_version_id == expected_source_version_id,
                    Concept.visibility == "ACTIVE",
                )
                .values(visibility="MERGED", status="MERGED")
            )
            if target_cas.rowcount != 1 or source_cas.rowcount != 1:
                raise ValueError("Concept version conflict: reload before merging")
            session.add(
                ConceptMergeRecord(
                    source_concept_id=source.id,
                    target_concept_id=target.id,
                    source_version_id=source_version.id,
                    target_version_id=target_version.id,
                    merged_target_version_id=merged.id,
                    actor=actor,
                    rationale=rationale.strip(),
                )
            )
            refreshed = self._rewire_syntheses(
                session,
                source_concept_id=source.id,
                target_concept_id=target.id,
                target_version_id=merged.id,
            )
            self._indexed_signature = ""
            return {
                "status": "MERGED",
                "source_concept_id": source.id,
                "target_concept_id": target.id,
                "target_version_id": merged.id,
                "refreshed_synthesis_versions": refreshed,
                "combined_modules": combined_modules,
                "deduplicated_blocks": deduplicated_blocks,
            }

    @staticmethod
    def _rewire_syntheses(
        session, *, source_concept_id: str, target_concept_id: str, target_version_id: str
    ) -> list[str]:
        refreshed: list[str] = []
        syntheses = session.scalars(
            select(Synthesis).where(
                Synthesis.visibility == "ACTIVE", Synthesis.active_version_id.is_not(None)
            )
        ).all()
        for synthesis in syntheses:
            current = session.get(SynthesisVersion, synthesis.active_version_id)
            if current is None:
                continue
            nodes = session.scalars(
                select(SynthesisNode)
                .where(SynthesisNode.synthesis_version_id == current.id)
                .order_by(SynthesisNode.sort_order)
            ).all()
            if not any(node.concept_id in {source_concept_id, target_concept_id} for node in nodes):
                continue
            by_id = {node.id: node for node in nodes}
            parent_ids = {node.parent_id for node in nodes if node.parent_id}
            target_already_present = any(node.concept_id == target_concept_id for node in nodes)
            skipped_ids = {
                node.id
                for node in nodes
                if target_already_present
                and node.concept_id == source_concept_id
                and node.id not in parent_ids
            }
            transformed = [node for node in nodes if node.id not in skipped_ids]
            version_no = (
                session.scalar(
                    select(func.max(SynthesisVersion.version_no)).where(
                        SynthesisVersion.synthesis_id == synthesis.id
                    )
                )
                or 0
            ) + 1
            signature = [
                (
                    node.stable_key,
                    target_concept_id if node.concept_id == source_concept_id else node.concept_id,
                    target_version_id
                    if node.concept_id in {source_concept_id, target_concept_id}
                    else node.concept_version_id,
                    by_id[node.parent_id].stable_key if node.parent_id in by_id else None,
                    node.sort_order,
                )
                for node in transformed
            ]
            coverage = _loads(current.coverage_json, {})
            coverage["concept_nodes"] = sum(node.node_kind == "CONCEPT" for node in transformed)
            coverage["published_concepts"] = coverage["concept_nodes"]
            if "groups" in coverage:
                coverage["groups"] = sum(node.node_kind == "GROUP" for node in transformed)
            new_version = SynthesisVersion(
                synthesis_id=synthesis.id,
                backbone_corpus_id=current.backbone_corpus_id,
                version_no=version_no,
                title=current.title,
                summary=current.summary,
                status="PUBLISHED",
                coverage_json=_json(coverage),
                content_sha256=_sha256(_json(signature)),
            )
            session.add(new_version)
            session.flush()
            id_map: dict[str, str] = {}
            for node in transformed:
                source_with_children = (
                    node.concept_id == source_concept_id
                    and node.id in parent_ids
                    and target_already_present
                )
                copied = SynthesisNode(
                    synthesis_version_id=new_version.id,
                    stable_key=node.stable_key,
                    parent_id=id_map.get(node.parent_id or ""),
                    title=node.title,
                    original_title=node.original_title,
                    level=node.level,
                    sort_order=node.sort_order,
                    node_kind="GROUP" if source_with_children else node.node_kind,
                    concept_id=None
                    if source_with_children
                    else target_concept_id
                    if node.concept_id == source_concept_id
                    else node.concept_id,
                    concept_version_id=None
                    if source_with_children
                    else target_version_id
                    if node.concept_id in {source_concept_id, target_concept_id}
                    else node.concept_version_id,
                    source_locator_json=node.source_locator_json,
                )
                session.add(copied)
                session.flush()
                id_map[node.id] = copied.id
            for reference in session.scalars(
                select(SynthesisReferenceSource).where(
                    SynthesisReferenceSource.synthesis_version_id == current.id
                )
            ).all():
                session.add(
                    SynthesisReferenceSource(
                        synthesis_version_id=new_version.id,
                        source_version_id=reference.source_version_id,
                        source_role=reference.source_role,
                    )
                )
            synthesis.active_version_id = new_version.id
            refreshed.append(new_version.id)
        return refreshed
