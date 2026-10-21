"""Hybrid recall, RRF, explicit scope and final SQL eligibility barriers."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..planning import QualityFailure
from ..repository import identifier
from ..segmentation import tokens
from .contracts import Query
from .lexical import match_query
from .vectors import normalize


def summary(hits):
    return {
        "atomic_notes": len({h["note_id"] for h in hits if h["kind"] == "ATOMIC"}),
        "concept_notes": len({h["note_id"] for h in hits if h["kind"] == "CONCEPT"}),
        "chunks": len(hits),
        "independent_source_families": len(
            {p["source_sha256"] for h in hits for p in h["provenance"]}
        ),
    }


class Searcher:
    def __init__(self, indexer, reranker=None, context_budget=6000, min_cosine=0.2):
        self.indexer = indexer
        self.store = indexer.store
        self.reranker = reranker
        self.context_budget = context_budget
        self.min_cosine = min_cosine

    def empty(self, status, message, query):
        return {
            "request_id": identifier(),
            "status": status,
            "message": message,
            "hits": [],
            "counts": summary([]),
            "snapshot": {
                "scope": query.scope.model_dump(),
                "profile_id": None,
                "index_generation": None,
                "validity_epoch": None,
                "context_sha256": None,
            },
            "timings_ms": {},
            "degraded_reason": None,
        }

    def search(self, query):
        query = Query.model_validate(query) if isinstance(query, dict) else query
        started = perf_counter()
        timings = {}
        try:
            generation, manifest, matrix = self.indexer.snapshot(query.retrieval_mode)
        except QualityFailure as exc:
            return self.empty("PROFILE_MISMATCH", str(exc), query)
        if generation is None:
            return self.empty("INDEX_PENDING", "所选模式与模型尚未建立索引。", query)
        with self.store.db.engine.connect() as c:
            c.exec_driver_sql("BEGIN")
            valid = {n["revision_id"]: n for n in eligible(c)}
            source_revisions = {}
            if query.scope.mode == "SOURCE":
                for row in c.execute(
                    text(
                        "SELECT DISTINCT se.revision_id,sr.source_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id JOIN source_revisions sr ON sr.id=e.source_revision_id"
                    )
                ):
                    source_revisions.setdefault(row[0], set()).add(row[1])
            valid = {
                r: n
                for r, n in valid.items()
                if n["kind"] in query.allowed_kinds
                and (query.scope.mode != "NOTES" or n["id"] in query.scope.note_ids)
                and (
                    query.scope.mode != "SOURCE"
                    or (
                        n["kind"] == "ATOMIC"
                        and source_revisions.get(r)
                        and source_revisions[r] <= set(query.scope.source_ids)
                    )
                )
            }
            epoch = c.exec_driver_sql(
                "SELECT validity_epoch FROM storage_contract WHERE id=1"
            ).scalar_one()

        def allowed(chunk):
            if chunk["revision_id"] not in valid or chunk["kind"] not in query.allowed_kinds:
                return False
            if query.scope.mode == "NOTES" and chunk["note_id"] not in query.scope.note_ids:
                return False
            return not (
                query.scope.mode == "SOURCE"
                and (
                    chunk["kind"] != "ATOMIC"
                    or not chunk["evidence"]
                    or any(e["source_id"] not in query.scope.source_ids for e in chunk["evidence"])
                )
            )

        admitted = {m["id"]: m for m in manifest if allowed(m)}
        if not admitted:
            return self.empty(
                "NO_EVIDENCE" if not valid else "INDEX_PENDING",
                "当前范围内没有已索引的有效知识。",
                query,
            )
        entity_titles = []
        for identity in query.entity_note_ids:
            matches = [m for m in admitted.values() if m["note_id"] == identity]
            if not matches:
                return self.empty("SCOPE_RESTRICTED", "当前实体无效或超出指定范围。", query)
            entity_titles.append(matches[0]["title"])
        routed = query.query + (
            "\n当前实体：" + "；".join(sorted(set(entity_titles))) if entity_titles else ""
        )
        if not entity_titles and query.query.strip().startswith(("它", "这个公式", "该公式")):
            return self.empty("CLARIFICATION_REQUIRED", "请指定所指的知识点或公式。", query)
        lanes = {}
        ranks = {}
        scores = {}
        if query.retrieval_mode == "HYBRID":
            # Independent routes share the exact same scope whitelist and one query vector.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="next-recall") as pool:
                lexical_work = pool.submit(self.lexical, routed, generation, admitted)
                vector_work = pool.submit(
                    self.semantic, routed, generation, manifest, matrix, admitted
                )
                lanes["lexical"], timings["lexical"] = lexical_work.result()
                lanes["vector"], vector_times = vector_work.result()
                timings.update(vector_times)
        else:
            lanes["lexical"], timings["lexical"] = self.lexical(routed, generation, admitted)
        for lane, ids in lanes.items():
            for rank, key in enumerate(dict.fromkeys(ids), 1):
                scores[key] = scores.get(key, 0) + 1 / (60 + rank)
                ranks.setdefault(key, {})[lane] = rank
        ordered = sorted(scores, key=lambda key: (-scores[key], key))
        degraded = None
        if query.rerank == "REQUIRED" and self.reranker is None:
            return self.empty("PROVIDER_ERROR", "重排服务尚未配置。", query)
        if query.rerank != "OFF" and self.reranker and ordered:
            stage = perf_counter()
            subset = ordered[:30]
            try:
                texts = self.read_passages([admitted[k] for k in subset], expand=False)
                reranked = self.reranker.rank(routed, texts)
                if len(reranked) != len(subset) or set(reranked) != set(subset):
                    raise QualityFailure("Reranker must return a complete unique permutation")
                ordered = reranked + ordered[30:]
            except Exception as exc:  # noqa: BLE001 - explicit AUTO rerank degradation retains same candidate set
                if query.rerank == "REQUIRED":
                    return self.empty("PROVIDER_ERROR", f"重排失败：{type(exc).__name__}", query)
                degraded = "RERANK_UNAVAILABLE_USING_SAME_RRF_CANDIDATES"
            timings["rerank"] = (perf_counter() - stage) * 1000
        passages = self.read_passages([admitted[k] for k in ordered], expand=True)
        selected = []
        per_note = Counter()
        used = 0
        seen = set()
        for passage in passages:
            key = (passage["revision_id"], passage["start_offset"], passage["end_offset"])
            if key in seen or per_note[passage["note_id"]] >= 3:
                continue
            cost = tokens(canonical(passage)) + 32
            if used + cost > self.context_budget:
                continue
            selected.append(passage)
            seen.add(key)
            used += cost
            per_note[passage["note_id"]] += 1
            if len(selected) >= query.top_k:
                break
        for i, hit in enumerate(selected, 1):
            hit.update(
                citation_id=f"K{i}", score=scores[hit["chunk_id"]], ranks=ranks[hit["chunk_id"]]
            )
        status = "READY" if selected else "NO_EVIDENCE"
        if selected and not self.current(selected):
            return self.empty("CONTENT_CHANGED", "检索期间来源或知识版本已变化。", query)
        timings["total"] = (perf_counter() - started) * 1000
        pending_revisions = sorted(set(valid) - {m["revision_id"] for m in manifest})
        result = {
            "request_id": identifier(),
            "status": status,
            "message": (
                "部分当前知识尚未进入本代索引，结果可能不完整。" if pending_revisions else ""
            )
            if selected
            else "没有足够的有效知识证据。",
            "hits": selected,
            "counts": summary(selected),
            "snapshot": {
                "scope": query.scope.model_dump(),
                "profile_id": generation["profile_id"],
                "index_generation": generation["id"],
                "validity_epoch": epoch,
                "context_sha256": sha(canonical(selected)),
            },
            "timings_ms": timings,
            "degraded_reason": degraded,
            "diagnostics": {
                "lanes": {k: len(v) for k, v in lanes.items()},
                "eligible_chunks": len(admitted),
                "context_tokens": used,
                "coverage_pending_revisions": pending_revisions,
            },
        }
        data = canonical({"query": query.model_dump(), "result": result}).encode()
        self.store.put(f"artifacts/queries/{result['request_id']}.json", data)
        return result

    def lexical(self, routed, generation, admitted):
        started = perf_counter()
        lexical = match_query(routed)
        result = []
        if lexical:
            with self.store.db.engine.connect() as c:
                rows = (
                    c.execute(
                        text(
                            "SELECT chunk_id,bm25(knowledge_fts,0,0,1.5,1.0) score FROM knowledge_fts WHERE knowledge_fts MATCH :q AND generation=:g AND chunk_id IN (SELECT value FROM json_each(:ids)) ORDER BY score,chunk_id LIMIT 50"
                        ),
                        {"q": lexical, "g": generation["id"], "ids": canonical(sorted(admitted))},
                    )
                    .mappings()
                    .all()
                )
                result = [r["chunk_id"] for r in rows]
        return result, (perf_counter() - started) * 1000

    def semantic(self, routed, generation, manifest, matrix, admitted):
        started = perf_counter()
        raw = self.indexer.embedding.embed([routed])
        if len(raw) != 1:
            raise QualityFailure("Query embedding response count differs")
        vector = normalize(raw, self.indexer.profile["dimensions"])[0]
        timings = {"embedding": (perf_counter() - started) * 1000}
        started = perf_counter()
        found = self.indexer.backend.search(
            generation["id"], [m["id"] for m in manifest], matrix, vector, set(admitted), 50
        )
        timings["vector"] = (perf_counter() - started) * 1000
        return [
            key for key, score in found if score >= self.min_cosine and key in admitted
        ], timings

    def read_passages(self, chunks, expand):
        notes = {}
        result = []
        for chunk in chunks:
            if chunk["note_id"] not in notes:
                try:
                    notes[chunk["note_id"]] = self.store.get_note(chunk["note_id"])
                except ValueError:
                    continue
            note = notes[chunk["note_id"]]
            if note["revision"]["id"] != chunk["revision_id"]:
                continue
            a, b = chunk["start_offset"], chunk["end_offset"]
            body = note["markdown"][a:b]
            if sha(body) != chunk["sha256"]:
                raise QualityFailure("Indexed chunk range/hash differs")
            section = next(s for s in note["sections"] if s["section_id"] == chunk["section_id"])
            if (
                expand
                and tokens(note["markdown"][section["start_offset"] : section["end_offset"]]) <= 800
            ):
                a, b = section["start_offset"], section["end_offset"]
                body = note["markdown"][a:b]
            provenance = list(
                {
                    (e["source_revision_id"], e["source_sha256"], e["page_number"]): {
                        "source_revision_id": e["source_revision_id"],
                        "source_sha256": e["source_sha256"],
                        "page": e["page_number"],
                    }
                    for e in chunk["evidence"]
                }.values()
            )
            result.append(
                {
                    "chunk_id": chunk["id"],
                    "note_id": chunk["note_id"],
                    "revision_id": chunk["revision_id"],
                    "kind": chunk["kind"],
                    "title": chunk["title"],
                    "section_id": chunk["section_id"],
                    "section_title": chunk["section_title"],
                    "start_offset": a,
                    "end_offset": b,
                    "sha256": sha(body),
                    "body": body,
                    "evidence_ids": sorted({e["evidence_id"] for e in chunk["evidence"]}),
                    "provenance": provenance,
                }
            )
        return result

    def current(self, passages):
        with self.store.db.engine.connect() as c:
            c.exec_driver_sql("BEGIN")
            valid = {n["revision_id"] for n in eligible(c, [p["revision_id"] for p in passages])}
        return all(p["revision_id"] in valid for p in passages)
