"""Evaluate next-v1 retrieval against explicit note-level relevance labels.

Uses an existing corpus only. It never generates notes or dispatches maintenance.
Reports contain note text and model output: keep them in an ignored local directory.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from time import perf_counter

from noodle_agent.adapters.llm import OpenAIEmbedding
from noodle_agent.config import Settings
from noodle_agent.next import model_calls
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.retrieval import answering, search, vectors
from noodle_agent.next.retrieval.answering import Answerer, ModelReranker
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.retrieval.vectors import ChromaVectors, normalize
from noodle_agent.next.runtime import real_agent
from noodle_agent.next.store import Store


def ranked_notes(hits):
    return list(dict.fromkeys(h["note_id"] for h in hits))


def metrics(ranked, relevance):
    relevant = {k for k, v in relevance.items() if v > 0}
    result = {}
    primary = {k for k, v in relevance.items() if v == max(relevance.values())}
    for k in (1, 3, 5):
        result[f"primary_hit@{k}"] = float(bool(set(ranked[:k]) & primary))
        result[f"hit@{k}"] = float(bool(set(ranked[:k]) & relevant))
        result[f"recall@{k}"] = len(set(ranked[:k]) & relevant) / len(relevant)
    result["mrr@5"] = next(
        (1 / i for i, n in enumerate(ranked[:5], 1) if n in relevant), 0
    )
    gain = sum(
        (2 ** relevance.get(n, 0) - 1) / math.log2(i + 1)
        for i, n in enumerate(ranked[:5], 1)
    )
    ideal = sum(
        (2**v - 1) / math.log2(i + 1)
        for i, v in enumerate(sorted(relevance.values(), reverse=True)[:5], 1)
    )
    result["ndcg@5"] = gain / ideal
    return result


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    left = math.floor(position)
    right = math.ceil(position)
    return values[left] + (values[right] - values[left]) * (position - left)


class QueryVectors:
    """Only evaluation uses this shared cache to compare identical query vectors."""

    def __init__(self, provider, root, profile_id):
        self.provider, self.root, self.profile_id = provider, root, profile_id
        self.calls = 0
        root.mkdir(parents=True, exist_ok=True)

    def embed(self, texts):
        result = []
        for text in texts:
            path = self.root / (sha(canonical([self.profile_id, text])) + ".json")
            if path.exists():
                result.append(json.loads(path.read_text(encoding="utf-8"))["vector"])
            else:
                started = perf_counter()
                vector = self.provider.embed([text])[0]
                self.calls += 1
                path.write_text(
                    canonical(
                        {
                            "vector": vector,
                            "provider_ms": (perf_counter() - started) * 1000,
                        }
                    ),
                    encoding="utf-8",
                )
                result.append(vector)
        return result


def evaluate(args):
    store = Store(args.data_dir)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw_cases = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = raw_cases["retrieval"]
    if not cases or any(
        not c["relevance"] or max(c["relevance"].values()) <= 0 for c in cases
    ):
        raise ValueError(
            "Each retrieval case needs positive note-level relevance labels"
        )
    with store.db.engine.connect() as c:
        note_ids = {n["id"] for n in eligible(c)}
    for case in cases:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case["id"]):
            raise ValueError("Case IDs must be simple filesystem-safe labels")
        if not set(case["relevance"]) <= note_ids:
            raise ValueError("Relevance labels must refer to eligible current notes")
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v < 0
            for v in case["relevance"].values()
        ):
            raise ValueError("Relevance grades must be nonnegative integers")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Case IDs must be unique")
    implementation = sha(
        canonical(
            {
                m.__name__: sha(Path(m.__file__).read_bytes())
                for m in (model_calls, answering, search, vectors)
            }
        )
    )
    settings = Settings.from_env()
    if not settings.embedding_api_key:
        raise ValueError("Real embedding credentials are required")
    embedding = OpenAIEmbedding(settings)
    exact = Indexer(store, embedding)

    def build_if_needed(indexer, mode="HYBRID"):
        generation, manifest, _ = indexer.snapshot(mode)
        with store.db.engine.connect() as c:
            current = {n["revision_id"] for n in eligible(c)}
        if (
            generation
            and {m["revision_id"] for m in manifest} == current
            and not args.rebuild
        ):
            return {
                "status": "REUSED",
                "generation": generation["id"],
                "notes": len(current),
                "chunks": len(manifest),
                "backend": indexer.backend.name,
                "profile_id": generation["profile_id"],
            }
        return indexer.build(mode)

    builds = {
        "exact": build_if_needed(exact),
        "keyword": build_if_needed(exact, "KEYWORD_ONLY"),
    }
    chroma = None
    if args.chroma:
        chroma = Indexer(
            store, embedding, ChromaVectors.connect(args.chroma_host, args.chroma_port)
        )
        builds["chroma"] = build_if_needed(chroma)
    provider = QueryVectors(embedding, output / "query-vectors", exact.profile["id"])
    # Prewarm outside measured retrieval; all variants see identical real vectors.
    for case in cases:
        provider.embed([case["query"]])
    exact.embedding = provider
    if chroma:
        chroma.embedding = provider
    calls = Calls(store, real_agent(settings)) if args.rerank or args.qa else None
    reranker = ModelReranker(calls) if args.rerank else None
    variants = [
        ("keyword", exact, "KEYWORD_ONLY", None, "OFF"),
        ("hybrid_exact", exact, "HYBRID", None, "OFF"),
    ]
    if chroma:
        variants.append(("hybrid_chroma", chroma, "HYBRID", None, "OFF"))
    if reranker:
        variants.append(("hybrid_exact_rerank", exact, "HYBRID", reranker, "REQUIRED"))
        if chroma and args.rerank_both:
            variants.append(
                ("hybrid_chroma_rerank", chroma, "HYBRID", reranker, "REQUIRED")
            )
    all_results = []
    aggregates = {}
    for name, indexer, mode, ranker, policy in variants:
        searcher = Searcher(indexer, ranker)
        results = []
        for case in cases:
            request = Query(
                query=case["query"],
                top_k=20,
                retrieval_mode=mode,
                rerank=policy,
                **case.get("filters", {}),
            )
            fingerprint = sha(
                canonical(
                    [
                        case,
                        request.model_dump(),
                        indexer.profile,
                        builds["exact"]["generation"],
                        name,
                        implementation,
                        settings.llm_fast_model if ranker else None,
                    ]
                )
            )
            path = output / f"{name}-{case['id']}.json"
            if (
                path.exists()
                and json.loads(path.read_text(encoding="utf-8")).get("fingerprint")
                == fingerprint
            ):
                row = json.loads(path.read_text(encoding="utf-8"))
            else:
                started = perf_counter()
                response = searcher.search(request)
                ranked = ranked_notes(response["hits"])
                row = {
                    "fingerprint": fingerprint,
                    "variant": name,
                    "case_id": case["id"],
                    "query": case["query"],
                    "category": case.get("category", "direct"),
                    "ranked_note_ids": ranked,
                    "metrics": metrics(ranked, case["relevance"]),
                    "elapsed_ms": (perf_counter() - started) * 1000,
                    "response": response,
                }
                path.write_text(canonical(row), encoding="utf-8")
            results.append(row)
            print(
                name,
                case["id"],
                row["response"]["status"],
                row["metrics"]["hit@1"],
                flush=True,
            )
        aggregates[name] = {
            key: statistics.mean(r["metrics"][key] for r in results)
            for key in results[0]["metrics"]
        }
        aggregates[name].update(
            {
                "queries": len(results),
                "ready": sum(r["response"]["status"] == "READY" for r in results),
                "p50_ms": statistics.median(r["elapsed_ms"] for r in results),
                "p95_ms": percentile([r["elapsed_ms"] for r in results], 0.95),
            }
        )
        all_results.extend(results)
    ann = []
    if chroma:
        exact_gen, manifest, matrix = exact.snapshot("HYBRID")
        chroma_gen, other_manifest, _ = chroma.snapshot("HYBRID")
        ids = [m["id"] for m in manifest]
        assert ids == [m["id"] for m in other_manifest]
        for case in cases:
            vector = normalize(
                provider.embed([case["query"]]), exact.profile["dimensions"]
            )[0]
            a = exact.backend.search(exact_gen["id"], ids, matrix, vector, set(ids), 50)
            b = chroma.backend.search(
                chroma_gen["id"], ids, matrix, vector, set(ids), 50
            )
            ann.append(
                {
                    "case_id": case["id"],
                    "recall_at_50": len({k for k, _ in a} & {k for k, _ in b}) / len(a),
                    "exact_ids": [k for k, _ in a],
                    "chroma_ids": [k for k, _ in b],
                }
            )
    answers = []
    if args.qa:
        answerer = Answerer(Searcher(exact, reranker), calls)
        for case in raw_cases.get("answers", []):
            response = answerer.answer(
                Query(
                    query=case["query"],
                    rerank="REQUIRED" if args.rerank else "OFF",
                    **case.get("filters", {}),
                )
            )
            accepted = (
                response["status"] == "READY"
                if case["answerable"]
                else response["status"]
                in {"COVERAGE_GAP", "NO_EVIDENCE", "SCOPE_RESTRICTED"}
            )
            answers.append(
                {
                    "case_id": case["id"],
                    "query": case["query"],
                    "expected_answerable": case["answerable"],
                    "behavior_pass": accepted,
                    "response": response,
                }
            )
            (output / "answers-progress.json").write_text(
                canonical(answers), encoding="utf-8"
            )
            print("QA", case["id"], response["status"], accepted, flush=True)
    report = {
        "contract": "next-v1-rag-evaluation",
        "implementation_sha256": implementation,
        "reranking_model_request": settings.llm_fast_model if args.rerank else None,
        "cases_sha256": sha(args.cases.read_bytes()),
        "builds": builds,
        "embedding_profile": exact.profile,
        "retrieval_metrics_unit": "deduplicated-note IDs, graded labels; incomplete relevance judgments",
        "timing": "warm immutable indexes and prewarmed real query-vector cache; excludes network embedding latency and answers",
        "aggregates": aggregates,
        "ann": ann,
        "ann_recall_at_50": statistics.mean(x["recall_at_50"] for x in ann)
        if ann
        else None,
        "answers": answers,
        "rows": all_results,
    }
    (output / "report.json").write_text(canonical(report), encoding="utf-8")
    print(json.dumps(aggregates, ensure_ascii=False, indent=2))
    store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chroma", action="store_true")
    parser.add_argument("--chroma-host", default="127.0.0.1")
    parser.add_argument("--chroma-port", type=int, default=8001)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--rerank-both", action="store_true")
    parser.add_argument("--qa", action="store_true")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force a new index generation instead of reusing a current one",
    )
    evaluate(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
