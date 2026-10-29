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
from .context import answer_knowledge, expanded_range, ranking_document
from .contracts import Query
from .lexical import match_query
from .options import RetrievalOptions
from .vectors import normalize


def summary(hits):
    return {
        "atomic_notes": len({h["note_id"] for h in hits if h["kind"] == "ATOMIC"}),
        "concept_notes": len({h["note_id"] for h in hits if h["kind"] == "CONCEPT"}),
        "chunks": len(hits),
        "independent_source_families": len(
            {
                family
                for h in hits
                for p in h["provenance"]
                for family in (p.get("source_families") or [p["source_sha256"]])
            }
        ),
    }


class Searcher:
    def __init__(
        self, indexer, reranker=None, context_budget=None, min_cosine=0.2, *, options=None
    ):
        self.indexer = indexer
        self.store = indexer.store
        self.reranker = reranker
        self.options = options or RetrievalOptions()
        self.context_budget = (
            self.options.context_tokens if context_budget is None else context_budget
        )
        self.min_cosine = min_cosine

    def empty(self, status, message, query):
        return {
            "request_id": getattr(self,'request_id',None) or identifier(),
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
        timings['snapshot'] = (perf_counter() - started) * 1000
        validity_started = perf_counter()
        with self.store.db.engine.connect() as c:
            c.exec_driver_sql("BEGIN")
            valid = {n["revision_id"]: n for n in eligible(c)}
            source_revisions = {}
            if query.scope.mode == "SOURCE":
                for row in c.execute(
                    text(
                        "SELECT DISTINCT se.revision_id,sr.source_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id JOIN source_revisions sr ON sr.id=e.source_revision_id WHERE NOT EXISTS(SELECT 1 FROM author_origin_sources ao WHERE ao.revision_id=se.revision_id) UNION SELECT ao.revision_id,sr.source_id FROM author_origin_sources ao JOIN source_revisions sr ON sr.id=ao.source_revision_id"
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
                    or not source_revisions.get(chunk["revision_id"], set())
                    <= set(query.scope.source_ids)
                )
            )

        admitted = {m["id"]: m for m in manifest if allowed(m)}
        timings['eligibility'] = (perf_counter() - validity_started) * 1000
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
        notes = {}
        rerank_stats = {"documents": 0, "skipped_budget": 0}
        rerank_scores = {}
        if query.rerank == "REQUIRED" and self.reranker is None:
            return self.empty("PROVIDER_ERROR", "重排服务尚未配置。", query)
        if query.rerank != "OFF" and self.reranker and ordered:
            stage = perf_counter()
            texts, subset = [], []
            estimated = tokens(routed)
            for passage in self.iter_passages([admitted[k] for k in ordered], False, notes):
                cost = tokens(ranking_document(passage))
                trial_tokens = (
                    self.reranker.estimate_request_tokens(routed, texts + [passage])
                    if hasattr(self.reranker, "estimate_request_tokens")
                    else estimated + cost + tokens(routed)
                )
                if (
                    cost > self.options.rerank_document_tokens
                    or trial_tokens > self.options.rerank_tokens
                ):
                    rerank_stats["skipped_budget"] += 1
                    continue
                texts.append(passage)
                subset.append(passage["chunk_id"])
                estimated = trial_tokens
                if len(subset) >= self.options.rerank_candidates:
                    break
            rerank_stats.update(documents=len(subset), input_tokens_estimate=estimated)
            try:
                if not subset:
                    raise QualityFailure("No candidate fits the rerank input budget")
                if hasattr(self.reranker, "rank_with_details"):
                    detail = self.reranker.rank_with_details(routed, texts)
                    reranked = detail.ordered_ids
                    rerank_scores = detail.scores
                    rerank_stats.update(detail.diagnostics)
                else:
                    reranked = self.reranker.rank(routed, texts)
                if len(reranked) != len(subset) or set(reranked) != set(subset):
                    raise QualityFailure("Reranker must return a complete unique permutation")
                rerank_stats["status"] = "READY"
                ordered = reranked + [k for k in ordered if k not in set(subset)]
            except Exception as exc:  # noqa: BLE001 - explicit AUTO degradation
                if query.rerank == "REQUIRED":
                    return self.empty("PROVIDER_ERROR", f"重排失败：{type(exc).__name__}", query)
                rerank_stats["status"] = "FAILED"
                degraded = "RERANK_UNAVAILABLE_USING_SAME_RRF_CANDIDATES"
            timings["rerank"] = (perf_counter() - stage) * 1000
        stage = perf_counter()
        selected = []
        per_note = Counter()
        used = 0
        for passage in self.iter_passages([admitted[k] for k in ordered], True, notes):
            overlap = next(
                (
                    i
                    for i, previous in enumerate(selected)
                    if previous["revision_id"] == passage["revision_id"]
                    and previous["section_id"] == passage["section_id"]
                    and max(previous["start_offset"], passage["start_offset"])
                    <= min(previous["end_offset"], passage["end_offset"])
                ),
                None,
            )
            trial = list(selected)
            if overlap is not None:
                previous = selected[overlap]
                left = min(previous["start_offset"], passage["start_offset"])
                right = max(previous["end_offset"], passage["end_offset"])
                body = notes[passage["note_id"]]["markdown"][left:right]
                matches = {
                    r["chunk_id"]: r for r in previous["matched_ranges"] + passage["matched_ranges"]
                }
                passage = {
                    **previous,
                    "start_offset": left,
                    "end_offset": right,
                    "body": body,
                    "sha256": sha(body),
                    "matched_ranges": list(matches.values()),
                    "expansion_reason": "MERGED_RANGES",
                }
                trial[overlap] = passage
            else:
                if per_note[passage["note_id"]] >= self.options.per_note:
                    continue
                passage["citation_id"] = f"K{len(selected) + 1}"
                trial.append(passage)
            cost = tokens(canonical(answer_knowledge(trial)))
            if cost > self.context_budget:
                continue
            selected, used = trial, cost
            if overlap is None:
                per_note[passage["note_id"]] += 1
            if len(selected) >= query.top_k:
                break
        timings["context"] = (perf_counter() - stage) * 1000
        for hit in selected:
            hit.update(
                score=scores[hit["chunk_id"]],
                ranks=ranks[hit["chunk_id"]],
                rerank_score=rerank_scores.get(hit["chunk_id"]),
            )
        status = "READY" if selected else "NO_EVIDENCE"
        if selected and not self.current(selected):
            return self.empty("CONTENT_CHANGED", "检索期间来源或知识版本已变化。", query)
        timings["total"] = (perf_counter() - started) * 1000
        pending_revisions = sorted(set(valid) - {m["revision_id"] for m in manifest})
        result = {
            "request_id": getattr(self,'request_id',None) or identifier(),
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
                "candidate_count": len(ordered),
                "rerank": rerank_stats,
                "context_artifact_tokens": tokens(canonical(selected)),
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
            bounds = generation.get('fts_rowid_bounds')
            bounded = ' AND rowid>=:low AND rowid<=:high ' if bounds else ''
            with self.store.db.engine.connect() as c:
                rows = (
                    c.execute(
                        text(
                            "SELECT chunk_id,bm25(knowledge_fts,0,0,1.5,1.0) score FROM knowledge_fts WHERE knowledge_fts MATCH :q AND generation=:g AND chunk_id IN (SELECT value FROM json_each(:ids)) " + bounded + " ORDER BY score,chunk_id LIMIT :limit"
                        ),
                        {
                            "q": lexical,
                            "g": generation["id"],
                            "ids": canonical(sorted(admitted)),
                            "limit": self.options.recall_k,
                            **({'low': bounds[0], 'high': bounds[1]} if bounds else {}),
                        },
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
            generation["id"],
            [m["id"] for m in manifest],
            matrix,
            vector,
            set(admitted),
            self.options.recall_k,
        )
        timings["vector"] = (perf_counter() - started) * 1000
        return [
            key for key, score in found if score >= self.min_cosine and key in admitted
        ], timings

    def read_passages(self, chunks, expand):
        return list(self.iter_passages(chunks, expand))

    def iter_passages(self, chunks, expand, notes=None):
        notes = {} if notes is None else notes
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
            matches = [
                {
                    "chunk_id": chunk["id"],
                    "start_offset": a,
                    "end_offset": b,
                    "sha256": chunk["sha256"],
                }
            ]
            reason = "MATCH_ONLY"
            if expand:
                a, b, reason = expanded_range(
                    note["markdown"], section, chunk, self.options.expansion_tokens
                )
                body = note["markdown"][a:b]
            provenance = list(
                {
                    (e["source_revision_id"], e["source_sha256"], e["page_number"]): {
                        "source_revision_id": e["source_revision_id"],
                        "source_sha256": e["source_sha256"],
                        "source_families": e.get("source_families", []),
                        "page": e["page_number"],
                    }
                    for e in chunk["evidence"]
                }.values()
            )
            yield {
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
                "matched_ranges": matches,
                "expansion_reason": reason,
            }

    def current(self, passages):
        with self.store.db.engine.connect() as c:
            c.exec_driver_sql("BEGIN")
            valid = {n["revision_id"] for n in eligible(c, [p["revision_id"] for p in passages])}
        return all(p["revision_id"] in valid for p in passages)
