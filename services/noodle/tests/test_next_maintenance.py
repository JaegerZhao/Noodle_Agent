"""P3/P4 integration uses explicit deterministic providers, never production fallbacks."""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from noodle_agent.next.api import create_app
from noodle_agent.next.contracts import Candidate, Review, Scope, Section
from noodle_agent.next.events import bootstrap, dispatch
from noodle_agent.next.repository import InvalidKnowledge, one
from noodle_agent.next.retrieval.contracts import Query
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def atomic(store, title, body, identity=None, protected=False):
    source = store.add_source((title + ": " + body).encode(), title, kind="AUTHOR")
    anchor = store.add_evidence(source["revision_id"], body)
    identity = identity or store.create_note("ATOMIC", title)
    with store.db.engine.connect() as c:
        previous = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)[
            "current_revision_id"
        ]
    value = Candidate(
        note_id=identity,
        title=title,
        scope=Scope(includes=["梯度计算"]),
        sections=[
            Section(
                section_id="explanation",
                title="原理",
                question="如何计算？",
                scope=Scope(includes=["梯度计算"]),
                markdown=body,
                evidence_ids=[anchor],
                protected=protected,
            )
        ],
    )
    revision = store.publish(
        value,
        expected_revision=previous,
        review=Review(
            candidate_sha256=value.fingerprint,
            policy_version=value.policy_version,
            reviewer="explicit-fixture",
            passed=True,
        ),
    )
    return source, identity, revision


class Agent:
    name = "explicit-injected-model"

    def __init__(self):
        self.calls = []
        self.on_answer = None
        self.fail_review = False

    def structured_json(self, operation, user, **kwargs):
        self.calls.append(operation)
        data = user["content"]
        if operation == "next.concept_decide":
            f = data["fragment"]
            catalog = data["catalogue"]
            target = catalog[0] if catalog else None
            if "INSUFFICIENT" in f["text"]:
                return {"action": "INSUFFICIENT", "reason": "不足以独立组织"}
            if "CONFLICT" in f["text"]:
                return {
                    "action": "CONFLICT",
                    "concept_id": target["id"] if target else None,
                    "reason": "同条件结论矛盾",
                }
            if not target:
                return {
                    "action": "CREATE_CONCEPT",
                    "reason": "独立主题",
                    "proposal": {
                        "title": "反向传播",
                        "domain": "nn",
                        "identity_key": "backprop",
                        "question": "如何反向计算梯度？",
                        "scope": {"includes": ["梯度计算"]},
                    },
                    "section_title": "误差传播",
                    "section_question": "梯度如何传播？",
                }
            question = "权重为何出现？" if "HIDDEN" in f["text"] else "梯度如何传播？"
            section = next((s for s in target["sections"] if s["question"] == question), None)
            if "LONG_CASE" in f["text"]:
                return {"action": "LINK_ONLY", "concept_id": target["id"], "reason": "长例单独阅读"}
            return {
                "action": "UPDATE_SECTION"
                if "NEW" in f["text"] and section
                else "ADD_EVIDENCE"
                if section or not target["sections"]
                else "CREATE_SECTION",
                "concept_id": target["id"],
                "section_id": section["section_id"] if section else None,
                "section_title": "权重来源" if "HIDDEN" in f["text"] else "误差传播",
                "section_question": question,
                "reason": "有效知识支持",
            }
        if operation == "next.concept_decision_review":
            return {"passed": True, "reason": "fixture verified"}
        if operation == "next.concept_write":
            return {
                "markdown": "\n\n".join(dict.fromkeys(f["text"].strip() for f in data["fragments"]))
            }
        if operation == "next.concept_review":
            return {
                "passed": not self.fail_review,
                "issues": ["failed"] if self.fail_review else [],
                "supported_fragment_ids": [f["fragment_id"] for f in data["fragments"]],
            }
        if operation == "next.answer":
            if self.on_answer:
                self.on_answer()
            return {
                "claims": [
                    {
                        "text": data["knowledge"][0]["body"],
                        "citation_ids": [data["knowledge"][0]["citation_id"]],
                    }
                ]
            }
        if operation == "next.answer_review":
            return {"passed": True, "supported_claim_indexes": list(range(len(data["claims"])))}
        if operation == "next.rerank":
            return {"ordered_chunk_ids": [p["chunk_id"] for p in reversed(data["passages"])]}
        raise AssertionError(operation)


class Embedding:
    model_name = "test-semantic-v1"
    dimensions = 4

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return [
            [
                float("梯度" in t or "gradient" in t),
                float("卷积" in t or "cnn" in t),
                float("权重" in t),
                0.1,
            ]
            for t in texts
        ]


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "next")
    yield value
    value.close()


def drain(application, limit=150):
    outcomes = []
    for _ in range(limit):
        outcome = application.run_once()
        if outcome is None:
            return outcomes
        outcomes.append(outcome)
        assert outcome["status"] != "REVIEW", outcome
    pytest.fail("Worker did not become idle")


def current_concept(store):
    with store.db.engine.connect() as c:
        row = one(c, "SELECT id FROM notes WHERE kind='CONCEPT'")
    return store.get_note(row["id"])


def test_concept_creation_evidence_only_update_and_local_new_chapter(store):
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    _a, _, _ = atomic(store, "课件A", "梯度沿链式法则反向传播。")
    drain(app)
    first = current_concept(store)
    body = first["markdown"]
    writers = agent.calls.count("next.concept_write")
    _b, _, _ = atomic(store, "课件B", "梯度沿链式法则反向传播。")
    drain(app)
    second = current_concept(store)
    assert second["markdown"] == body and second["revision"]["id"] != first["revision"]["id"]
    assert agent.calls.count("next.concept_write") == writers
    original = second["sections"][0]["sha256"]
    atomic(store, "课件C", "HIDDEN 权重来自链式法则的局部导数。")
    drain(app)
    third = current_concept(store)
    assert len(third["sections"]) == 2 and third["sections"][0]["sha256"] == original
    with store.db.engine.connect() as c:
        assert all(
            json.loads(r[0]) for r in c.exec_driver_sql("SELECT fragments_json FROM concept_inputs")
        )


def test_source_delete_immediately_blocks_old_search_then_rebuilds_and_restores(store):
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    a, _, _ = atomic(store, "A", "梯度沿链式法则反向传播。")
    drain(app)
    b, _, _ = atomic(store, "B", "梯度沿链式法则反向传播。")
    drain(app)
    concept = current_concept(store)
    store.set_source_deleted(a["source_id"], True)
    with pytest.raises(InvalidKnowledge):
        store.get_note(concept["id"])
    result = app.searcher.search(Query(query="梯度", retrieval_mode="KEYWORD_ONLY"))
    assert all(h["kind"] != "CONCEPT" for h in result["hits"])
    drain(app)
    assert current_concept(store)["eligible"]
    store.set_source_deleted(b["source_id"], True)
    drain(app)
    with pytest.raises(InvalidKnowledge):
        store.get_note(concept["id"])
    store.set_source_deleted(a["source_id"], False)
    drain(app)
    assert current_concept(store)["eligible"]


def test_atomic_revision_replaces_support_and_cannot_leave_old_dependency(store):
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    _, note, old = atomic(store, "A", "梯度通过链式法则。")
    drain(app)
    _, _, new = atomic(store, "A更新", "NEW 梯度通过链式法则，并解释局部导数。", identity=note)
    with pytest.raises(InvalidKnowledge):
        current_concept(store)
    drain(app)
    concept = current_concept(store)
    with store.db.engine.connect() as c:
        inputs = [
            r[0]
            for r in c.exec_driver_sql(
                "SELECT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=?",
                (concept["revision"]["id"],),
            )
        ]
    assert new["revision_id"] in inputs and old["revision_id"] not in inputs


def test_router_fans_out_both_consumers_and_ignores_author_source_generation(store):
    atomic(store, "A", "梯度计算。")
    dispatch(store)
    with store.db.engine.connect() as c:
        kinds = [r[0] for r in c.exec_driver_sql("SELECT kind FROM jobs")]
    assert set(kinds) == {"ANALYZE_ATOMIC", "INDEX_NOTE", "UPDATE_MAPS"}


def test_bootstrap_and_repeated_dispatch_are_idempotent(store):
    atomic(store, "A", "梯度计算。")
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    drain(app)
    bootstrap(store)
    drain(app)
    first = current_concept(store)["revision"]["id"]
    bootstrap(store)
    drain(app)
    assert current_concept(store)["revision"]["id"] == first


def test_hybrid_uses_one_query_vector_and_never_indexes_raw_or_moc(store):
    source, _, _ = atomic(store, "A", "梯度由链式法则计算。")
    emb = Embedding()
    indexer = Indexer(store, emb, allow_test_embeddings=True)
    indexer.build()
    before = len(emb.calls)
    result = Searcher(indexer).search(Query(query="梯度", rerank="OFF"))
    assert result["status"] == "READY" and len(emb.calls) - before == 1
    assert all(h["kind"] == "ATOMIC" for h in result["hits"])
    scoped = Searcher(indexer).search(
        Query(
            query="梯度",
            scope={"mode": "SOURCE", "source_ids": [source["source_id"]]},
            allowed_kinds=["ATOMIC"],
        )
    )
    assert scoped["counts"]["atomic_notes"] == 1


def test_query_contract_rejects_raw_moc_and_cross_source_concepts():
    for kind in ["RAW", "MOC"]:
        with pytest.raises(ValueError):
            Query(query="x", allowed_kinds=[kind])
    with pytest.raises(ValueError):
        Query(query="x", scope={"mode": "SOURCE", "source_ids": ["s"]})


def test_missing_embedding_never_silently_uses_hash(store):
    atomic(store, "A", "梯度计算。")
    indexer = Indexer(store)
    with pytest.raises(ValueError):
        indexer.build()
    assert Searcher(indexer).search(Query(query="梯度"))["status"] == "PROFILE_MISMATCH"
    indexer.build("KEYWORD_ONLY")
    assert (
        Searcher(indexer).search(Query(query="梯度", retrieval_mode="KEYWORD_ONLY"))["status"]
        == "READY"
    )


def test_vectors_are_float32_and_cache_reused_for_same_content(store):
    atomic(store, "A", "梯度计算。")
    emb = Embedding()
    indexer = Indexer(store, emb, allow_test_embeddings=True)
    indexer.build()
    count = len(emb.calls)
    indexer.build()
    assert len(emb.calls) == count
    _, manifest, matrix = indexer.snapshot("HYBRID")
    assert matrix.dtype == np.float32 and matrix.shape == (len(manifest), 4)


def test_answer_citations_and_content_change_during_generation(store):
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    source, _, _ = atomic(store, "A", "梯度通过链式法则计算。")
    app.indexer.build("KEYWORD_ONLY")
    query = Query(query="梯度如何计算？", retrieval_mode="KEYWORD_ONLY")
    good = app.answerer.answer(query)
    assert good["status"] == "READY" and good["citations"] and "[K1]" in good["answer_markdown"]
    agent.on_answer = lambda: store.set_source_deleted(source["source_id"], True)
    changed = app.answerer.answer(query)
    assert changed["status"] == "CONTENT_CHANGED" and not changed["answer_markdown"]
    assert not changed["hits"] and not changed["citations"]


def test_api_is_next_only_and_validates_scopes(store):
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度计算。")
    drain(app)
    with TestClient(create_app(application=app)) as client:
        assert client.get("/health").json()["legacy_loaded"] is False
        assert client.get("/internal/next/concepts").json()["total"] == 1
        assert (
            client.post(
                "/internal/next/search", json={"query": "梯度", "retrieval_mode": "KEYWORD_ONLY"}
            ).json()["status"]
            == "READY"
        )
        assert (
            client.post(
                "/internal/next/search", json={"query": "x", "allowed_kinds": ["RAW"]}
            ).status_code
            == 422
        )
        assert client.get("/internal/v1/search?q=x").status_code == 404


def test_conflict_resolution_blocks_old_head_until_rebuilt(store):
    from noodle_agent.next.concepts.review import Resolution, resolve
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.eligibility import eligible

    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    atomic(store, "B", "CONFLICT 梯度的条件不同，需明确适用范围。")
    stopped = app.run_once()
    assert stopped["status"] == "REVIEW"
    with store.db.engine.connect() as c:
        item = one(c, "SELECT * FROM contribution_decisions WHERE status='REVIEW'")
        assert before["revision"]["id"] not in {r["revision_id"] for r in eligible(c)}
    value = Resolution(
        expected_sha256=sha(item["decision_json"]),
        status="ACCEPTED",
        reason="保留条件差异",
        decision={
            "action": "UPDATE_SECTION",
            "concept_id": before["id"],
            "section_id": before["sections"][0]["section_id"],
            "reason": "补充条件差异",
        },
    )
    with pytest.raises(ValueError):
        resolve(store, item["id"], value.model_copy(update={"expected_sha256": "outdated"}))
    resolve(store, item["id"], value)
    with pytest.raises(InvalidKnowledge):
        store.get_note(before["id"])
    store.jobs.retry_review(stopped['job_id'])
    drain(app)
    after = current_concept(store)
    assert after["revision"]["id"] != before["revision"]["id"]
    assert "条件不同" in after["markdown"]


def test_skipped_and_link_only_fragments_are_accounted_without_rewriting(store):
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    count = agent.calls.count("next.concept_write")
    _, linked, _ = atomic(store, "案例", "LONG_CASE 梯度计算的长例。")
    atomic(store, "信息不足", "INSUFFICIENT 梯度。")
    drain(app)
    after = current_concept(store)
    assert agent.calls.count("next.concept_write") == count
    assert after["markdown"] == before["markdown"]
    with store.db.engine.connect() as c:
        assert (
            one(c, "SELECT status FROM contribution_decisions WHERE action='INSUFFICIENT'")[
                "status"
            ]
            == "SKIPPED"
        )
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM links WHERE target_note_id=?", (linked,)
            ).scalar_one()
            >= 1
        )


def test_review_failure_preserves_previous_concept_head(store):
    agent = Agent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    agent.fail_review = True
    atomic(store, "B", "NEW 梯度还依赖局部导数。")
    outcomes = []
    for _ in range(30):
        result = app.run_once()
        if result is None:
            break
        outcomes.append(result)
    assert any(r["kind"] == "BUILD_CONCEPT" and r["status"] == "REVIEW" for r in outcomes)
    assert current_concept(store)["revision"]["id"] == before["revision"]["id"]
    assert agent.calls.count("next.concept_review") == 4  # initial + three bounded repairs


def test_writer_source_change_cannot_publish_old_inputs(store):
    class ChangingAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            if operation == "next.concept_write":
                store.set_source_deleted(source["source_id"], True)
            return super().structured_json(operation, user, **kwargs)

    source, _, _ = atomic(store, "A", "梯度沿链式法则传播。")
    app = Application(store, agent=ChangingAgent(), index_mode="KEYWORD_ONLY")
    drain(app)
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE n.kind='CONCEPT'"
            ).scalar_one()
            == 0
        )


def test_concurrent_workers_reserve_one_identity_and_contribution(store):
    from concurrent.futures import ThreadPoolExecutor

    atomic(store, "A", "梯度沿链式法则传播。")
    atomic(store, "B", "梯度沿链式法则传播。")
    apps = [Application(store, agent=Agent(), index_mode="KEYWORD_ONLY") for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda a: a.run_once(), apps))
    assert all(r["status"] in {"SUCCEEDED", "RETRY"} for r in results)
    drain(apps[0])
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM concept_identity_claims").scalar_one() == 1
        assert c.exec_driver_sql("SELECT count(*) FROM contribution_decisions").scalar_one() == 2
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM notes WHERE kind='CONCEPT' AND current_revision_id IS NOT NULL"
            ).scalar_one()
            == 1
        )


def test_index_activation_rejects_content_change_during_embedding(store):
    from noodle_agent.next.repository import Conflict

    source, _, _ = atomic(store, "A", "梯度计算。")

    class ChangedEmbedding(Embedding):
        def embed(self, texts):
            store.set_source_deleted(source["source_id"], True)
            return super().embed(texts)

    indexer = Indexer(store, ChangedEmbedding(), allow_test_embeddings=True)
    with pytest.raises(Conflict):
        indexer.build()
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM index_heads").scalar_one() == 0


def test_vector_backend_failure_preserves_active_generation(store):
    from noodle_agent.next.retrieval.vectors import ExactVectors

    atomic(store, "A", "梯度计算。")
    indexer = Indexer(store, Embedding(), allow_test_embeddings=True)
    first = indexer.build()["generation"]

    class Broken(ExactVectors):
        def build(self, *args):
            raise ConnectionError("offline")

    indexer.backend = Broken()
    with pytest.raises(ConnectionError):
        indexer.build()
    assert indexer.snapshot("HYBRID")[0]["id"] == first


def test_corrupt_vector_cache_is_rejected(store):
    atomic(store, "A", "梯度计算。")
    indexer = Indexer(store, Embedding(), allow_test_embeddings=True)
    indexer.build()
    with store.db.engine.connect() as c:
        ref = c.exec_driver_sql("SELECT vector_ref FROM index_entries LIMIT 1").scalar_one()
    store.path(ref).write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        indexer.build()


def test_scope_entity_and_reranker_failure_are_explicit(store):
    _, note, _ = atomic(store, "A", "梯度计算。")
    indexer = Indexer(store)
    indexer.build("KEYWORD_ONLY")

    class BadReranker:
        def rank(self, *args):
            return ["unknown"]

    search = Searcher(indexer, BadReranker())
    assert search.search(Query(query="梯度", retrieval_mode="KEYWORD_ONLY"))["degraded_reason"]
    assert (
        search.search(Query(query="梯度", retrieval_mode="KEYWORD_ONLY", rerank="REQUIRED"))[
            "status"
        ]
        == "PROVIDER_ERROR"
    )
    assert (
        search.search(Query(query="它是什么", retrieval_mode="KEYWORD_ONLY"))["status"]
        == "CLARIFICATION_REQUIRED"
    )
    assert (
        search.search(
            Query(query="它是什么", entity_note_ids=[note], retrieval_mode="KEYWORD_ONLY")
        )["status"]
        == "READY"
    )
    assert (
        search.search(
            Query(
                query="梯度",
                scope={"mode": "NOTES", "note_ids": ["missing"]},
                retrieval_mode="KEYWORD_ONLY",
            )
        )["status"]
        == "NO_EVIDENCE"
    )


def test_profile_switch_requires_distinct_generation(store):
    atomic(store, "A", "梯度计算。")
    indexer = Indexer(store, Embedding(), allow_test_embeddings=True)
    indexer.build()
    alternate = Embedding()
    alternate.model_name = "test-semantic-v2"
    other = Indexer(store, alternate, allow_test_embeddings=True)
    assert Searcher(other).search(Query(query="梯度"))["status"] == "INDEX_PENDING"
    other.build()
    assert other.snapshot("HYBRID")[0]["profile_id"] != indexer.snapshot("HYBRID")[0]["profile_id"]


def test_semantic_ranges_preserve_display_math_code_and_table():
    from noodle_agent.next.segmentation import ranges

    body = "解释梯度。\n\n$$\ng = a b\n\n+ c\n$$\n\n其中 a 表示导数。\n\n|变量|含义|\n|---|---|\n|a|导数|\n\n```python\nx = 1\n\nx += 2\n```\n"
    spans = ranges(body, target=1)
    pieces = [body[a:b] for a, b in spans]
    assert any("解释梯度" in p and "其中 a" in p and "$$" in p for p in pieces)
    assert any("|变量|" in p and "|a|" in p for p in pieces)
    assert any("```python" in p and "x += 2" in p and p.count("```") == 2 for p in pieces)
    assert "".join(pieces) == body


def test_chroma_backend_matches_exact_and_detects_missing_generation(store):
    chromadb = pytest.importorskip(
        "chromadb", reason="Optional vector backend; install noodle-agent[vector]"
    )
    from chromadb.config import Settings

    from noodle_agent.next.retrieval.vectors import ChromaVectors

    atomic(store, "A", "梯度计算。")
    atomic(store, "B", "卷积 cnn 的局部连接。")
    embedding = Embedding()
    exact = Indexer(store, embedding, allow_test_embeddings=True)
    exact.build()
    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    backend = ChromaVectors(client)
    indexed = Indexer(store, embedding, backend, allow_test_embeddings=True)
    indexed.build()
    query = Query(query="梯度", rerank="OFF")
    expected = Searcher(exact).search(query)
    actual = Searcher(indexed).search(query)
    assert [r["note_id"] for r in actual["hits"]] == [r["note_id"] for r in expected["hits"]]
    generation, manifest, _ = indexed.snapshot("HYBRID")
    backend.delete(generation["id"], [manifest[0]["id"]])
    with pytest.raises(ValueError):
        Searcher(indexed).search(query)
    client.delete_collection("noodle_" + generation["id"])
    with pytest.raises(chromadb.errors.NotFoundError):
        Searcher(indexed).search(query)


def test_restoring_identical_inputs_republishes_withdrawn_concept(store):
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    source, _, _ = atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    store.set_source_deleted(source["source_id"], True)
    drain(app)
    store.set_source_deleted(source["source_id"], False)
    drain(app)
    after = current_concept(store)
    assert after["markdown"] == before["markdown"]
    assert after["revision"]["id"] != before["revision"]["id"]
    assert after["revision"]["status"] == "PUBLISHED"


def test_catalogue_exposes_reserved_sections_before_publication(store):
    from noodle_agent.next.concepts.analysis import catalogue

    atomic(store, "A", "梯度沿链式法则传播。")
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    assert app.run_once()["kind"] == "ANALYZE_ATOMIC"
    with store.db.engine.connect() as c:
        planned = catalogue(store, c)[0]
    assert planned["sections"][0]["planned"]
    assert planned["sections"][0]["section_id"].startswith("section_")
    assert planned["sections"][0]["markdown"]


def test_protected_concept_chapter_cannot_be_automatically_rewritten(store):
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    with store.db.engine.connect() as c:
        proof = one(
            c,
            "SELECT * FROM build_artifacts WHERE storage_ref=:ref",
            ref=f"artifacts/builds/{before['revision']['id']}.json",
        )
    payload = json.loads(store.read(proof["storage_ref"], proof["sha256"]))["candidate"]
    payload["sections"][0]["protected"] = True
    candidate = Candidate.model_validate(payload)
    revision = store.publish(
        candidate,
        expected_revision=before["revision"]["id"],
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="explicit-human-fixture",
            passed=True,
        ),
    )
    atomic(store, "B", "NEW 梯度依赖局部导数。")
    results = []
    for _ in range(40):
        result = app.run_once()
        if result is None:
            break
        results.append(result)
    assert any(r["status"] == "REVIEW" and r["kind"] == "BUILD_CONCEPT" for r in results)
    assert store.get_note(before["id"])["revision"]["id"] == revision["revision_id"]


def test_runtime_response_contracts_and_api_failures(store):
    from noodle_agent.next.retrieval.contracts import AnswerResponse, SearchResponse

    atomic(store, "A", "梯度计算。")
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    app.indexer.build("KEYWORD_ONLY")
    query = Query(query="梯度", retrieval_mode="KEYWORD_ONLY")
    assert SearchResponse.model_validate(app.searcher.search(query)).status == "READY"
    assert AnswerResponse.model_validate(app.answerer.answer(query)).citations
    with TestClient(create_app(application=app)) as client:
        assert client.post("/internal/next/answer", json=query.model_dump()).status_code == 200
        response = client.get("/health")
        assert response.headers["cache-control"] == "no-store"
        app.indexer.embedding = type(
            "Failed",
            (),
            {
                "embed": lambda self, texts: (_ for _ in ()).throw(
                    ConnectionError("private provider error")
                )
            },
        )()
        # Keyword mode remains usable and never silently becomes vector mode.
        assert (
            client.post("/internal/next/search", json=query.model_dump()).json()["status"]
            == "READY"
        )


def test_forged_concept_fragment_cannot_be_published(store):
    app = Application(store, agent=Agent(), index_mode="KEYWORD_ONLY")
    atomic(store, "A", "梯度沿链式法则传播。")
    drain(app)
    before = current_concept(store)
    with store.db.engine.connect() as c:
        proof = one(
            c,
            "SELECT * FROM build_artifacts WHERE storage_ref=:ref",
            ref=f"artifacts/builds/{before['revision']['id']}.json",
        )
    value = json.loads(store.read(proof["storage_ref"], proof["sha256"]))["candidate"]
    value["sections"][0]["inputs"][0]["fragments"][0]["sha256"] = "0" * 64
    candidate = Candidate.model_validate(value)
    with pytest.raises(InvalidKnowledge):
        store.publish(
            candidate,
            expected_revision=before["revision"]["id"],
            review=Review(
                candidate_sha256=candidate.fingerprint,
                policy_version=candidate.policy_version,
                reviewer="fixture",
                passed=True,
            ),
        )
    assert current_concept(store)["revision"]["id"] == before["revision"]["id"]


def test_provider_error_response_does_not_leak_details_or_use_keyword_fallback(store):
    atomic(store, "A", "梯度计算。")
    embedding = Embedding()
    app = Application(store, agent=Agent(), embedding=embedding, allow_test_embeddings=True)
    app.indexer.build()

    def unavailable(texts):
        raise ConnectionError("private service configuration")

    embedding.embed = unavailable
    with TestClient(create_app(application=app)) as client:
        response = client.post("/internal/next/search", json={"query": "梯度"})
        assert response.status_code == 503
        from noodle_agent.next.retrieval.contracts import SearchResponse
        result = SearchResponse.model_validate(response.json())
        assert result.status == "PROVIDER_ERROR" and not result.hits
        assert "private service configuration" not in response.text


def test_concept_proposal_section_fields_are_canonicalized_without_discarding_conflicts():
    from noodle_agent.next.concepts.contracts import Decision

    proposal = {
        "title": "数值稳定性",
        "domain": "数学",
        "identity_key": "numerical-stability",
        "question": "如何保持数值稳定？",
        "scope": {"includes": ["稳定计算"]},
        "section_title": "LogSumExp",
        "section_question": "如何稳定计算指数和？",
    }
    raw = {"action": "CREATE_CONCEPT", "proposal": proposal, "reason": "独立主题"}
    parsed = Decision.model_validate(raw)
    assert parsed.section_title == "LogSumExp"
    assert parsed.section_question == "如何稳定计算指数和？"
    assert parsed.proposal.identity_key == "numerical-stability"
    assert raw["proposal"]["section_title"] == "LogSumExp"
    with pytest.raises(ValueError, match="Conflicting"):
        Decision.model_validate({**raw, "section_title": "另一章"})
