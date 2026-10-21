"""Retrieval effectiveness wiring, scope and backend invariants without API calls."""

import numpy as np
import pytest

from noodle_agent.next.contracts import Candidate, Review, Scope, Section
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.retrieval.answering import Answerer
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.retrieval.vectors import ChromaVectors, ExactVectors, normalize
from noodle_agent.next.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "rag")
    yield value
    value.close()


def note(store, title, body):
    source = store.add_source((body + "\nRAW_ONLY_MARKER").encode(), title, kind="AUTHOR")
    evidence = store.add_evidence(source["revision_id"], body)
    identity = store.create_note("ATOMIC", title)
    candidate = Candidate(
        note_id=identity,
        title=title,
        scope=Scope(includes=[title]),
        sections=[
            Section(
                section_id="main",
                title=title,
                question=title + "？",
                scope=Scope(includes=[title]),
                markdown=body,
                evidence_ids=[evidence],
            )
        ],
    )
    revision = store.publish(
        candidate,
        expected_revision=None,
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="test-fixture",
            passed=True,
        ),
    )
    return identity, source, revision


class Embedding:
    dimensions = 3
    model_name = "test-rag-effectiveness"

    def embed(self, texts):
        return [[1.0, 0.5, 0.1] for _ in texts]


def test_rerank_reorders_same_pool_before_top_k_and_retains_rrf_scores(store):
    bad, _, _ = note(store, "Sigmoid", "Sigmoid 是平滑激活。")
    good, _, _ = note(store, "Leaky ReLU", "Leaky ReLU 在负数区域保留小斜率。")
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")
    _, manifest, _ = indexer.snapshot("KEYWORD_ONLY")
    ids = {m["note_id"]: m["id"] for m in manifest}

    class Reranker:
        def rank(self, query, passages):
            assert {p["chunk_id"] for p in passages} == set(ids.values())
            return [ids[good], ids[bad]]

    searcher = Searcher(indexer)
    searcher.lexical = lambda *args: ([ids[bad], ids[good]], 0.0)
    query = Query(query="负输入仍保留梯度的激活", retrieval_mode="KEYWORD_ONLY", top_k=1)
    before = searcher.search(query)
    searcher.reranker = Reranker()
    after = searcher.search(query.model_copy(update={"rerank": "REQUIRED"}))
    assert before["hits"][0]["note_id"] == bad
    assert after["hits"][0]["note_id"] == good
    assert after["hits"][0]["score"] < before["hits"][0]["score"]


def test_reranker_cannot_recover_a_candidate_missing_from_recall(store):
    note(store, "激活", "负区域保留斜率。")
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")

    class NeverCalled:
        def rank(self, *args):
            raise AssertionError("No recall pool")

    searcher = Searcher(indexer, NeverCalled())
    searcher.lexical = lambda *args: ([], 0.0)
    result = searcher.search(
        Query(query="负区域", retrieval_mode="KEYWORD_ONLY", rerank="REQUIRED")
    )
    assert result["status"] == "NO_EVIDENCE"


def test_rrf_reinforces_agreement_instead_of_adding_incompatible_scores(store):
    for title in ("A", "B", "C"):
        note(store, title, title + " 梯度")
    indexer = Indexer(store, Embedding(), allow_test_embeddings=True)
    indexer.build()
    _, manifest, _ = indexer.snapshot("HYBRID")
    a, b, c = [m["id"] for m in manifest]
    searcher = Searcher(indexer)
    searcher.lexical = lambda *args: ([a, b], 0.0)
    searcher.semantic = lambda *args: ([c, b], {})
    result = searcher.search(Query(query="梯度", rerank="OFF"))
    assert result["hits"][0]["chunk_id"] == b
    assert result["hits"][0]["score"] == pytest.approx(2 / 62)


def test_raw_source_text_is_not_a_keyword_corpus(store):
    note(store, "梯度", "梯度沿链式法则传播。")
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")
    assert (
        Searcher(indexer).search(Query(query="RAW_ONLY_MARKER", retrieval_mode="KEYWORD_ONLY"))[
            "status"
        ]
        == "NO_EVIDENCE"
    )


@pytest.mark.parametrize(
    "value",
    [
        [[0.0, 0.0, 0.0]],
        [[float("nan"), 0.0, 1.0]],
        [[float("inf"), 0.0, 1.0]],
        [[1.0, 2.0]],
        [[1.0, 2.0, 3.0, 4.0]],
    ],
)
def test_invalid_embedding_never_enters_a_semantic_index(value):
    with pytest.raises(ValueError):
        normalize(value, 3)


def test_chroma_scope_batches_preserve_exact_nearest_neighbors():
    chromadb = pytest.importorskip("chromadb")
    from chromadb.config import Settings

    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    backend = ChromaVectors(client)
    rng = np.random.default_rng(20261021)
    matrix = normalize(rng.normal(size=(1500, 32)), 32)
    query = matrix[1234]
    ids = [f"scope-{i:04}" for i in range(len(matrix))]
    generation = "scope-batch-test"
    backend.build(generation, ids, matrix)
    try:
        for allowed in (set(ids), set(ids[1150:])):
            expected = ExactVectors().search(generation, ids, matrix, query, allowed, 30)
            actual = backend.search(generation, ids, matrix, query, allowed, 30)
            assert actual[0][0] == ids[1234]
            assert all(k in allowed for k, _ in actual)
            assert len({k for k, _ in expected} & {k for k, _ in actual}) / 30 >= 0.96
    finally:
        client.delete_collection("noodle_" + generation)


def test_context_budget_counts_metadata_and_deduplicates_parent_ranges(store):
    note(store, "梯度", "梯度解释。\n\n" + "变量用于反向传播。" * 250)
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")
    result = Searcher(indexer, context_budget=1000).search(
        Query(query="梯度", retrieval_mode="KEYWORD_ONLY", top_k=20)
    )
    assert result["diagnostics"]["context_tokens"] <= 1000
    ranges = [(h["revision_id"], h["start_offset"], h["end_offset"]) for h in result["hits"]]
    assert len(ranges) == len(set(ranges))


@pytest.mark.parametrize("mode", ["insufficient", "failed_review"])
def test_source_change_during_rejected_answer_cannot_return_stale_hit_body(store, mode):
    _, source, _ = note(store, "梯度", "梯度通过链式法则计算。")
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")

    class Agent:
        name = "explicit-rejection-fixture"

        def structured_json(self, operation, user, **kwargs):
            if operation == "next.answer":
                if mode == "insufficient":
                    store.set_source_deleted(source["source_id"], True)
                    return {"insufficient": True, "claims": []}
                return {"claims": [{"text": "梯度沿链式法则传播。", "citation_ids": ["K1"]}]}
            if operation == "next.answer_review":
                store.set_source_deleted(source["source_id"], True)
                return {"passed": False, "issues": ["unsupported"], "supported_claim_indexes": []}
            raise AssertionError(operation)

    result = Answerer(Searcher(indexer), Calls(store, Agent())).answer(
        Query(query="梯度", retrieval_mode="KEYWORD_ONLY")
    )
    assert result["status"] == "CONTENT_CHANGED"
    assert result["hits"] == [] and result["citations"] == [] and not result["answer_markdown"]


def test_real_reranker_adapter_repairs_incomplete_and_duplicate_permutations(store):
    from noodle_agent.next.retrieval.answering import ModelReranker

    class Agent:
        name = "explicit-permutation-test"

        def __init__(self):
            self.requests = []

        def structured_json(self, operation, user, **kwargs):
            self.requests.append(user)
            return {
                "ordered_chunk_ids": (
                    ["a"]
                    if len(self.requests) == 1
                    else ["a", "a"]
                    if len(self.requests) == 2
                    else ["b", "a"]
                )
            }

    agent = Agent()
    ranking = ModelReranker(Calls(store, agent)).rank(
        "question", [{"chunk_id": "a", "body": "A"}, {"chunk_id": "b", "body": "B"}]
    )
    assert ranking == ["b", "a"] and len(agent.requests) == 3
    assert agent.requests[-1]["corrections"]
    assert all(r["content"]["required_count"] == 2 for r in agent.requests)


def test_reranker_stops_after_three_checked_semantic_attempts(store):
    from noodle_agent.next.retrieval.answering import ModelReranker

    class Agent:
        name = "explicit-failed-permutation-test"

        def __init__(self):
            self.count = 0

        def structured_json(self, **kwargs):
            self.count += 1
            return {"ordered_chunk_ids": ["unknown"]}

    agent = Agent()
    with pytest.raises(ValueError):
        ModelReranker(Calls(store, agent)).rank("q", [{"chunk_id": "a", "body": "A"}])
    assert agent.count == 3


@pytest.mark.parametrize("policy", ["OFF", "AUTO", "REQUIRED"])
def test_cli_forwards_explicit_rerank_policy(store, monkeypatch, policy):
    from types import SimpleNamespace

    from noodle_agent.cli import build_parser
    from noodle_agent.next import app_cli

    queries = []
    monkeypatch.setattr(app_cli.Settings, "from_env", lambda: SimpleNamespace())
    monkeypatch.setattr(app_cli, "real_agent", lambda settings: None)
    monkeypatch.setattr(app_cli, "backend_from_env", lambda: ExactVectors())
    monkeypatch.setattr(
        app_cli,
        "Application",
        lambda *a, **kw: SimpleNamespace(
            searcher=SimpleNamespace(search=lambda q: queries.append(q) or {"status": "READY"})
        ),
    )
    args = build_parser().parse_args(
        ["next-search", "--data-dir", str(store.root), "--query", "梯度", "--rerank", policy]
    )
    output = []
    app_cli.run(args, store, output.append)
    assert queries[0].rerank == policy and output == [{"status": "READY"}]


def test_evaluation_metrics_deduplicate_notes_and_distinguish_primary_targets():
    import runpy
    from pathlib import Path

    module = runpy.run_path(
        str(Path(__file__).resolve().parents[3] / "scripts/evaluate-next-rag.py")
    )
    ranked = module["ranked_notes"](
        [{"note_id": "alternative"}, {"note_id": "alternative"}, {"note_id": "primary"}]
    )
    measured = module["metrics"](ranked, {"primary": 2, "alternative": 1})
    assert ranked == ["alternative", "primary"]
    assert measured["hit@1"] == 1 and measured["primary_hit@1"] == 0
    assert measured["recall@3"] == 1 and 0 < measured["ndcg@5"] < 1
