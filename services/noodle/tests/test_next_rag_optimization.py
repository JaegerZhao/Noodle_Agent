"""Regression checks for chunk boundaries, model budgets and native rerank safety."""

import json
from dataclasses import replace
from itertools import pairwise

import httpx
import pytest
from test_next_rag_effectiveness import note

from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.retrieval.answering import Answerer, ModelReranker
from noodle_agent.next.retrieval.context import answer_knowledge, expanded_range
from noodle_agent.next.retrieval.contracts import AnswerResponse, Query, SearchResponse
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.options import RetrievalOptions
from noodle_agent.next.retrieval.reranking import DashScopeReranker, reranker_from_env
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.segmentation import retrieval_ranges, tokens
from noodle_agent.next.store import Store

URL = (
    "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "rag")
    yield value
    value.close()


def passages():
    return [
        {
            "chunk_id": "long-original-id-a",
            "title": "梯度",
            "section_title": "计算",
            "body": "链式法则",
            "sha256": "PRIVATE_HASH",
        },
        {
            "chunk_id": "long-original-id-b",
            "title": "梯度",
            "section_title": "定义",
            "body": "变化率",
            "sha256": "PRIVATE_HASH",
        },
    ]


def test_lexical_preparation_does_not_hold_the_database_write_lock(store, monkeypatch):
    import sqlite3

    from noodle_agent.next.retrieval import indexing

    note(store, '梯度', '梯度通过链式法则计算。')
    original = indexing.searchable
    probes = []
    def checked(value):
        with sqlite3.connect(store.root / 'noodle.db', timeout=.05) as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.rollback()
        probes.append(value)
        return original(value)
    monkeypatch.setattr(indexing, 'searchable', checked)
    assert Indexer(store).build('KEYWORD_ONLY')['status'] == 'READY'
    assert probes


def test_bulk_activation_rolls_back_all_rows_when_head_publication_fails(store):
    from sqlalchemy import event

    note(store, '原有内容', '已有知识。')
    indexer = Indexer(store)
    original = indexer.build('KEYWORD_ONLY')['generation']
    note(store, '新增内容', '这次新增的知识。')
    def fail_head(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO index_heads'):
            raise RuntimeError('Activation fixture failure')
    event.listen(store.db.engine, 'before_cursor_execute', fail_head)
    try:
        with pytest.raises(RuntimeError, match='Activation fixture'):
            indexer.build('KEYWORD_ONLY')
    finally:
        event.remove(store.db.engine, 'before_cursor_execute', fail_head)
    assert indexer.head('KEYWORD_ONLY')['id'] == original
    with store.db.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM index_generations').scalar_one() == 1
        assert connection.exec_driver_sql('SELECT count(*) FROM knowledge_fts WHERE generation!=?', (original,)).scalar_one() == 0


def test_answer_context_keeps_pdf_pages_without_full_provenance():
    passage = {**passages()[0], "citation_id": "K1", "provenance": [
        {"page": 4, "source_sha256": "PRIVATE_HASH"}, {"page": 3}, {"page": 4},
    ]}
    compact = answer_knowledge([passage])[0]
    assert compact["source_pages"] == [3, 4]
    assert "PRIVATE_HASH" not in canonical(compact)
    assert compact["body"] == passage["body"]


def test_fts_bounds_keep_generation_scope_and_identical_ranking(store):
    note(store, '梯度', '梯度通过链式法则计算。')
    index = Indexer(store)
    index.build('KEYWORD_ONLY')
    second, _, _ = note(store, '梯度优化', '梯度优化中使用步长。')
    index.build('KEYWORD_ONLY')
    generation, manifest, _ = index.snapshot('KEYWORD_ONLY')
    selected = {m['id']: m for m in manifest if m['note_id'] == second}
    bounded, _ = Searcher(index).lexical('梯度', generation, selected)
    unbounded, _ = Searcher(index).lexical('梯度', {k:v for k,v in generation.items() if k != 'fts_rowid_bounds'}, selected)
    assert bounded == unbounded and bounded
    assert all(selected[key]['note_id'] == second for key in bounded)


def test_fts_imported_interleaved_generation_uses_safe_fallback(tmp_path):
    import sqlite3

    from noodle_agent.next.retrieval.indexing import lexical_bounds
    database = tmp_path/'interleaved.db'
    with sqlite3.connect(database) as c:
        c.execute('CREATE VIRTUAL TABLE knowledge_fts USING fts5(generation UNINDEXED,chunk_id UNINDEXED,title,body)')
        c.executemany('INSERT INTO knowledge_fts VALUES (?,?,?,?)', [('a','one','x','x'),('b','other','x','x'),('a','two','x','x')])
    assert lexical_bounds(str(database.resolve()), 'a', ('one','two')) == (1,3)


def test_single_character_topic_is_not_lost_inside_question_prefix(store):
    target, _, _ = note(store, '熵', '熵衡量不确定性。')
    note(store, '什么是机器学习', '这是另一主题。')
    index = Indexer(store)
    index.build('KEYWORD_ONLY')
    result = Searcher(index).search(Query(query='请解释什么是熵', retrieval_mode='KEYWORD_ONLY', rerank='OFF'))
    assert result['status'] == 'READY'
    assert result['hits'][0]['note_id'] == target


def adapter(response=None, handler=None):
    def serve(request):
        value = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert value["parameters"] == {"top_n": 2, "return_documents": False}
        assert value["model"] == "qwen3-rerank"
        assert "PRIVATE_HASH" not in request.content.decode()
        assert len(value["input"]["documents"]) == 2
        return httpx.Response(200, json=response)

    return DashScopeReranker("test-key", URL, transport=httpx.MockTransport(handler or serve))


def test_native_rerank_preserves_ids_scores_and_real_usage():
    result = adapter(
        {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
            "usage": {"total_tokens": 42},
        }
    ).rank_with_details("梯度", passages())
    assert result.ordered_ids == ["long-original-id-b", "long-original-id-a"]
    assert result.scores["long-original-id-b"] == 0.9
    assert result.diagnostics["usage"] == {"total_tokens": 42}


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 0, "relevance_score": 0.8}],
        [{"index": 0, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.7}],
        [{"index": 0, "relevance_score": 0.8}, {"index": 2, "relevance_score": 0.7}],
        [{"index": True, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.7}],
        [{"index": 0, "relevance_score": "bad"}, {"index": 1, "relevance_score": 0.7}],
        [{"index": 0, "relevance_score": True}, {"index": 1, "relevance_score": 0.7}],
    ],
)
def test_native_rerank_rejects_malformed_permutations(rows):
    with pytest.raises(ValueError):
        adapter({"output": {"results": rows}}).rank("q", passages())


def test_native_http_errors_do_not_echo_provider_body_or_key():
    ranker = adapter(handler=lambda r: httpx.Response(403, text="test-key PRIVATE_NOTE"))
    with pytest.raises(ValueError) as exc:
        ranker.rank("q", passages())
    assert "403" in str(exc.value)
    assert "test-key" not in str(exc.value) and "PRIVATE_NOTE" not in str(exc.value)


def test_native_timeout_is_not_retried():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(httpx.ReadTimeout):
        adapter(handler=timeout).rank("q", passages())
    assert len(calls) == 1


def test_factory_enables_dashscope_without_an_answer_model(monkeypatch):
    monkeypatch.setenv("NOODLE_NEXT_RERANK", "on")
    monkeypatch.setenv("NOODLE_NEXT_RERANK_PROVIDER", "dashscope")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "existing-key")
    monkeypatch.setenv("RERANK_URL", URL)
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    assert reranker_from_env().api_key == "existing-key"
    monkeypatch.setenv("NOODLE_NEXT_RERANK", "off")
    assert reranker_from_env() is None


def test_compact_llm_input_uses_short_ids_and_complete_mapping(store):
    class Agent:
        name = "fixture"

        def structured_json(self, user, **kw):
            value = user["content"]
            assert [p["chunk_id"] for p in value["passages"]] == ["0", "1"]
            assert "PRIVATE_HASH" not in canonical(value)
            return {"ordered_chunk_ids": ["1", "0"]}

    assert ModelReranker(Calls(store, Agent())).rank("q", passages()) == [
        "long-original-id-b",
        "long-original-id-a",
    ]


def test_short_section_and_small_tail_keep_original_text_without_overlap():
    text = "gradient " * 430
    assert retrieval_ranges(text) == [(0, len(text))]
    longer = ("gradient " * 350 + "\n\n") * 2 + "tail " * 40
    spans = retrieval_ranges(longer)
    assert len(spans) == 2 and tokens(longer[slice(*spans[-1])]) > 100
    assert "".join(longer[a:b] for a, b in spans) == longer
    assert all(a[1] == b[0] for a, b in pairwise(spans))


def test_retrieval_chunks_preserve_equation_and_code_groups():
    body = (
        "解释。\n\n$$\ng = a b\n\n+ c\n$$\n\n其中 a 是导数。\n\n```python\nx = 1\n\nx += 2\n```\n"
    )
    spans = retrieval_ranges(body, target=1, soft_limit=1)
    assert "".join(body[a:b] for a, b in spans) == body
    assert any("解释" in body[a:b] and "其中" in body[a:b] and "$$" in body[a:b] for a, b in spans)
    assert any("x += 2" in body[a:b] and body[a:b].count("```") == 2 for a, b in spans)


def test_long_section_expands_neighbors_but_never_crosses_parent():
    body = "\n\n".join("段落" + str(i) + "。解释梯度。" * 25 for i in range(12))
    spans = retrieval_ranges(body)
    a, b = spans[3]
    start, end, reason = expanded_range(
        "PREFIX" + body + "SUFFIX",
        {"start_offset": 6, "end_offset": 6 + len(body)},
        {"start_offset": 6 + a, "end_offset": 6 + b},
        800,
    )
    assert 6 <= start <= 6 + a < 6 + b <= end <= 6 + len(body)
    assert reason == "ADJACENT_BLOCKS" and tokens(("PREFIX" + body)[start:end]) <= 800


@pytest.mark.parametrize("policy,status", [("AUTO", "READY"), ("REQUIRED", "PROVIDER_ERROR")])
def test_rerank_failure_has_explicit_policy_and_preserves_scope(store, policy, status):
    note(store, "梯度", "梯度使用链式法则。")
    index = Indexer(store)
    index.build("KEYWORD_ONLY")

    class Broken:
        def rank(self, *a):
            raise httpx.ReadTimeout("timeout")

    result = Searcher(index, Broken()).search(
        Query(query="梯度", retrieval_mode="KEYWORD_ONLY", rerank=policy)
    )
    assert result["status"] == status
    if policy == "AUTO":
        assert result["degraded_reason"] == "RERANK_UNAVAILABLE_USING_SAME_RRF_CANDIDATES"
        SearchResponse.model_validate(result)


def test_candidate_count_and_token_budget_are_independent_of_final_top_k(store):
    for i in range(5):
        note(store, "梯度" + str(i), "梯度变化。" * 8)
    index = Indexer(store)
    index.build("KEYWORD_ONLY")
    received = []

    class Ranker:
        def rank(self, q, ps):
            received.extend(ps)
            return [p["chunk_id"] for p in ps]

    opts = replace(RetrievalOptions(), rerank_candidates=2, rerank_tokens=1000)
    result = Searcher(index, Ranker(), options=opts).search(
        Query(query="梯度", retrieval_mode="KEYWORD_ONLY", top_k=1)
    )
    assert len(received) == 2 and len(result["hits"]) == 1
    assert result["diagnostics"]["rerank"]["input_tokens_estimate"] <= 1000


def test_query_keeps_matched_range_and_context_range_with_valid_hashes(store):
    note(store, "梯度", "\n\n".join("梯度段落。" * 40 for _ in range(5)))
    index = Indexer(store)
    index.build("KEYWORD_ONLY")
    result = Searcher(index).search(Query(query="梯度", retrieval_mode="KEYWORD_ONLY"))
    SearchResponse.model_validate(result)
    for hit in result["hits"]:
        md = store.get_note(hit["note_id"])["markdown"]
        assert sha(md[hit["start_offset"] : hit["end_offset"]]) == hit["sha256"]
        for r in hit["matched_ranges"]:
            assert hit["start_offset"] <= r["start_offset"] < r["end_offset"] <= hit["end_offset"]
            assert sha(md[r["start_offset"] : r["end_offset"]]) == r["sha256"]
    assert result["diagnostics"]["context_tokens"] == tokens(
        canonical(answer_knowledge(result["hits"]))
    )


def test_answer_and_review_receive_only_compact_cited_knowledge(store):
    note(store, "梯度", "梯度通过链式法则计算。")
    index = Indexer(store)
    index.build("KEYWORD_ONLY")

    class Agent:
        name = "fixture"

        def structured_json(self, operation, user, **kw):
            data = user["content"]
            assert all(
                set(p) == {"citation_id", "title", "section_title", "body", "source_pages"}
                for p in data["knowledge"]
            )
            if operation == "next.answer":
                return {"claims": [{"text": "梯度通过链式法则计算。", "citation_ids": ["K1"]}]}
            return {"passed": True, "issues": [], "supported_claim_indexes": [0]}

    result = Answerer(Searcher(index), Calls(store, Agent())).answer(
        Query(query="梯度", retrieval_mode="KEYWORD_ONLY")
    )
    assert result["status"] == "READY"
    AnswerResponse.model_validate(result)


@pytest.mark.parametrize("field", ["recall_k", "rerank_candidates", "context_tokens"])
def test_nonpositive_budgets_fail_at_startup(field):
    with pytest.raises(ValueError):
        RetrievalOptions(**{field: 0})
