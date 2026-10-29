"""Offline convergence regressions; all services are deterministic fixtures."""

import pytest
from fastapi.testclient import TestClient
from test_next_maintenance import Agent, atomic
from test_next_source_sections import prepared as _source_prepared

from noodle_agent.next.api import create_app
from noodle_agent.next.concepts.entity_contracts import EntityPlan
from noodle_agent.next.concepts.entity_routing import validate_plan
from noodle_agent.next.retrieval.contracts import AnswerResponse, Query
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store

source_prepared = _source_prepared


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "convergence")
    yield value
    value.close()


@pytest.mark.parametrize(
    "mode", ["ready", "refusal", "review_failed", "corrected", "provider_failed", "partial"]
)
def test_answer_api_branches_obey_one_contract(store, mode):
    atomic(store, "A", "梯度计算。")

    class AnswerAgent(Agent):
        checks = 0

        def structured_json(self, *, operation, **kwargs):
            if operation == "next.answer":
                if mode == "provider_failed":
                    raise ConnectionError("private provider configuration")
                if mode == "refusal":
                    return {"claims": [], "insufficient": True}
                if mode == "partial":
                    draft = super().structured_json(operation=operation, **kwargs)
                    draft["claims"].append(
                        {**draft["claims"][0], "text": "Another claim with incomplete support"}
                    )
                    return draft
            if operation == "next.answer_review":
                self.checks += 1
                if mode == "review_failed" or mode == "corrected" and self.checks == 1:
                    return {
                        "passed": False,
                        "issues": ["unsupported claim"],
                        "supported_claim_indexes": [],
                    }
                if mode == "partial":
                    return {"passed": True, "issues": [], "supported_claim_indexes": [0]}
            return super().structured_json(operation=operation, **kwargs)

    app = Application(store, agent=AnswerAgent(), index_mode="KEYWORD_ONLY")
    app.indexer.build("KEYWORD_ONLY")
    query = Query(query="梯度", retrieval_mode="KEYWORD_ONLY")
    with TestClient(create_app(application=app)) as client:
        response = client.post("/internal/next/answer", json=query.model_dump())
    assert response.status_code == 200
    result = AnswerResponse.model_validate(response.json())
    expected = (
        "PROVIDER_ERROR"
        if mode == "provider_failed"
        else "COVERAGE_GAP"
        if mode in {"refusal", "review_failed", "partial"}
        else "READY"
    )
    assert result.status == expected
    assert "private provider configuration" not in response.text


@pytest.mark.parametrize(
    "title",
    ["Slide Number 3", "PAGE No. ２", "第十二页", "第 3 張投影片", "幻灯片 4", "Page 2 / 40"],
)
def test_page_labels_are_not_semantic_titles(title):
    from noodle_agent.next.source_sections import generic_title

    assert generic_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "Chapter 2 Probability",
        "2 Methods",
        "Abstract",
        "Results and discussion",
        "第十二章 概率",
        "Page rank algorithm",
    ],
)
def test_books_papers_and_real_titles_remain_semantic(title):
    from noodle_agent.next.source_sections import generic_title

    assert not generic_title(title)


def test_existing_catalog_relation_and_invalid_optional_relation_are_local():
    entity = {
        "title": "Child",
        "domain": "test",
        "identity_key": "child",
        "question": "What is child?",
        "scope": {"includes": ["child"]},
        "description": "Child entity",
    }
    plan = EntityPlan.model_validate(
        {
            "entities": [entity],
            "routes": {
                "a": {"entity_key": "child", "sections": {"s": "principles"}, "reason": "valid"}
            },
            "relations": [
                {
                    "source_key": "child",
                    "target_key": "parent",
                    "support_atomic_ids": ["a"],
                    "reason": "existing parent",
                },
                {
                    "source_key": "child",
                    "target_key": "missing",
                    "support_atomic_ids": ["a"],
                    "reason": "invalid optional target",
                },
            ],
        }
    )
    cards = [{"id": "a", "sections": [{"id": "s"}]}]
    catalog = [
        {
            "id": "parent-id",
            "title": "Parent",
            "domain": "test",
            "definition": {"identity_key": "parent"},
        }
    ]
    validate_plan(plan, cards, catalog)
    assert [r.target_key for r in plan.relations] == ["parent"]
    assert plan.unresolved_relations[0]["reason"] == "UNKNOWN_ENDPOINT"
    assert plan.routes["a"].entity_key == "child"


def test_mixed_outline_only_plans_untrusted_range(store):
    from types import SimpleNamespace

    import fitz

    from noodle_agent.next.events import dispatch
    from noodle_agent.next.source_sections import make_plan, units_from_blocks

    with fitz.open() as pdf:
        for _ in range(3):
            pdf.new_page()
        pdf.set_toc([[1, "Introduction", 1], [1, "Slide Number 2", 2], [1, "Results", 3]])
        source = store.add_source(pdf.tobytes(), "Mixed")
    dispatch(store)
    lease = store.jobs.claim("test", kind="GENERATE_ATOMICS")
    blocks = [
        {"id": f"b{i}", "page": i, "text": f"Topic {i}", "kind": "HEADING"} for i in range(1, 4)
    ]
    calls = []

    def call(stage, operation, instruction, payload, contract, check):
        calls.append(payload)
        value = contract.model_validate(
            {"sections": [{"start_unit": payload["first_unit"], "title": "Repaired topic"}]}
        )
        check(value)
        return value

    g = SimpleNamespace(store=store, lease=lease, call=call)
    parsed = {"parse_id": "fixture", "artifact": {"blocks": blocks, "page_count": 3}}
    plan = make_plan(g, parsed, units_from_blocks(parsed["artifact"]))
    assert [p["title"] for p in plan["plans"]] == ["Introduction", "Repaired topic", "Results"]
    assert len(calls) == 1 and [p["page"] for p in calls[0]["pages"]] == [2]
    assert plan["source_revision_id"] == source["revision_id"]


def test_unrelated_review_does_not_block_organization(store):
    from noodle_agent.next.events import dispatch
    from noodle_agent.next.repository import identifier

    atomic(store, "Unrelated", "梯度计算。")
    dispatch(store)
    lease = store.jobs.claim("test", kind="ANALYZE_ATOMIC")
    store.jobs.fail(lease, {"code": "FIXTURE_REVIEW"}, retry=False)
    with store.repository.transaction() as c:
        store.repository.event(
            c,
            "ORGANIZATION_REVIEW_REQUESTED",
            "concept-organization",
            None,
            {"source_revision": identifier()},
            key="fixture-organization",
        )
    app = Application(
        store, agent=Agent(), index_mode="KEYWORD_ONLY", job_kinds=["REVIEW_ORGANIZATION"]
    )
    result = app.run_once()
    assert result["status"] == "SUCCEEDED"
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql("SELECT status FROM jobs WHERE id=?", (lease.job_id,)).scalar_one()
            == "REVIEW"
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_native_closed_batch_parallel_writes_and_single_routing(source_prepared, reverse):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from time import perf_counter_ns

    from test_next_entity_concepts import Agent as EntityAgent
    from test_next_entity_concepts import drain
    from test_next_source_sections import Agent as SourceAgent
    from test_next_source_sections import Parser

    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.exports import article
    from noodle_agent.next.worker import GenerationWorker

    store, lease, _ = source_prepared
    profile = {
        "parser": "source-test",
        "generation_layout": "source-sections-v1",
        "concept_layout": "entity-v2",
    }
    Checkpoints(store.repository, lease).save("profile", profile, profile)
    store.jobs.defer(lease, {"status": "FIXTURE_READY"})
    barrier = threading.Barrier(2)
    review_ready, allow_review, first_finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class PipelineAgent(EntityAgent):
        def __init__(self):
            super().__init__()
            self.source = SourceAgent()
            self.intervals = {}

        def structured_json(self, *, operation, user, **kwargs):
            if operation.startswith("next.section_") or operation == "next.assembly_review":
                if operation == "next.section_write":
                    title = user["content"]["plan"]["title"]
                    start = perf_counter_ns()
                    barrier.wait(timeout=10)
                    finishes_first = (title == "Term frequency") != reverse
                    if not finishes_first:
                        assert first_finished.wait(10)
                    result = self.source.structured_json(operation=operation, user=user, **kwargs)
                    result.update(
                        summary=title + "的内容说明",
                        entity_hints={
                            "subject": "PCA",
                            "subjects": ["PCA"],
                            "context": "fixture",
                            "intent": "EXPLAIN",
                        },
                    )
                    self.intervals[title] = (start, perf_counter_ns())
                    if finishes_first:
                        first_finished.set()
                    return result
                if operation == "next.assembly_review":
                    review_ready.set()
                    assert allow_review.wait(10)
                return self.source.structured_json(operation=operation, user=user, **kwargs)
            return super().structured_json(operation=operation, user=user, **kwargs)

    agent = PipelineAgent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    worker = GenerationWorker(store, parser=Parser(), agent=agent, draft_concurrency=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.run_once)
        try:
            assert review_ready.wait(10)
            assert app.run_once() is None
            assert not agent.calls
        finally:
            allow_review.set()
        result = future.result(timeout=20)
    assert result["status"] == "SUCCEEDED", result
    intervals = list(agent.intervals.values())
    assert max(a for a, b in intervals) < min(b for a, b in intervals)
    with store.db.engine.connect() as c:
        batch_events = (
            c.exec_driver_sql(
                "SELECT payload_json FROM events WHERE event_type='ATOMIC_BATCH_CLOSED'"
            )
            .scalars()
            .all()
        )
        assert len(batch_events) == 1
        assert (
            c.exec_driver_sql("SELECT count(*) FROM jobs WHERE kind='ANALYZE_ATOMIC'").scalar_one()
            == 0
        )
    batch = json.loads(batch_events[0])["routing_batch"]
    assert batch["state"] == "CLOSED" and len(batch["atomics"]) == 2
    drain(app)
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == 1
    assert sum(op == "next.entity_plan_review" for op, _ in agent.calls) == 1
    from noodle_agent.next.metrics import root_for

    with store.db.engine.connect() as c:
        analysis_job = c.exec_driver_sql(
            "SELECT id FROM jobs WHERE kind='ANALYZE_ATOMIC'"
        ).scalar_one()
        assert root_for(c, analysis_job) == result["job_id"]
    with store.repository.transaction() as c:
        store.repository.event(
            c,
            "ATOMIC_BATCH_CLOSED",
            batch["source_revision_id"],
            batch["source_revision_id"],
            {"routing_batch": batch},
            key="fixture-duplicate",
        )
    drain(app)
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == 1
    with store.db.engine.connect() as c:
        moc = c.exec_driver_sql(
            "SELECT id FROM notes WHERE kind='MOC' AND creation_key LIKE 'source-map:%'"
        ).scalar_one()
    body, _ = article(store, moc)
    assert body.index("Term frequency") < body.index("Inverse document frequency")


def closed_fixture(store):
    from noodle_agent.next.contracts import Candidate, EntityHints, Review, Scope, Section, sha

    source = store.add_source(
        b"PCA fixture explanation\nSVD fixture explanation", "Closed batch", kind="AUTHOR"
    )
    checked = []
    for title in ["PCA", "SVD"]:
        identity = store.content.create_note(
            "ATOMIC", title, creation_key=f"atomic:{source['revision_id']}:source-section:{title}"
        )
        evidence = store.add_evidence(source["revision_id"], title + " fixture explanation")
        candidate = Candidate(
            note_id=identity,
            title=title,
            summary=title + " fixture description",
            entity_hints=EntityHints(
                subject=title, subjects=[title], context="fixture", intent="EXPLAIN"
            ),
            scope=Scope(includes=[title]),
            sections=[
                Section(
                    section_id="s",
                    title="原理",
                    question=title,
                    scope=Scope(includes=[title]),
                    markdown=title + " fixture explanation",
                    evidence_ids=[evidence],
                )
            ],
        )
        checked.append(
            (
                candidate,
                Review(
                    candidate_sha256=candidate.fingerprint,
                    policy_version=candidate.policy_version,
                    reviewer="fixture",
                    passed=True,
                ),
            )
        )
    batch = {
        "source_revision_id": source["revision_id"],
        "input_hash": sha(source["revision_id"]),
        "policy": "fixture-closed-v1",
    }
    store.publisher.publish_batch(checked, routing_batch=batch)
    import json

    with store.db.engine.connect() as c:
        payload = c.exec_driver_sql(
            "SELECT payload_json FROM events WHERE event_type='ATOMIC_BATCH_CLOSED'"
        ).scalar_one()
    return source, json.loads(payload)["routing_batch"]


@pytest.mark.parametrize(
    "mode", ["invalid_target", "review", "missing", "source_only", "semantic", "relation"]
)
def test_closed_batch_isolates_bad_inputs_and_caches_decision(store, mode):
    import json

    from test_next_entity_concepts import Agent as EntityAgent

    from noodle_agent.next.convergence import convergence_report

    _, batch = closed_fixture(store)

    class PartialAgent(EntityAgent):
        def structured_json(self, *, operation, user, **kwargs):
            result = super().structured_json(operation=operation, user=user, **kwargs)
            if operation == "next.entity_plan":
                first = result["assignments"][0]
                if mode == "invalid_target":
                    first["target"] = "C999"
                elif mode in {"review", "source_only"}:
                    if mode == "source_only":
                        result["new_entities"] = [
                            e for e in result["new_entities"] if e["ref"] != first["target"]
                        ]
                    first.update(
                        action="REVIEW" if mode == "review" else "SOURCE_ONLY",
                        target=None,
                        sections={},
                        reason="Explicit fixture disposition",
                    )
                elif mode == "missing":
                    result["assignments"] = result["assignments"][1:]
                elif mode == "relation":
                    result["relations"] = [
                        {
                            "source_key": first["target"],
                            "target_key": "C999",
                            "support_atomic_ids": [first["atomic"]],
                            "reason": "invalid optional edge",
                        }
                    ]
            if operation == "next.entity_plan_review" and mode == "semantic":
                result.update(passed=False, issues=["A0 has the wrong entity"])
            return result

    agent = PartialAgent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    outcomes = []
    for _ in range(60):
        outcome = app.run_once()
        if outcome is None:
            break
        outcomes.append(outcome)
    expected_review = mode != "source_only"
    batch_result = next(o for o in outcomes if o["kind"] == "ANALYZE_ATOMIC")
    assert (batch_result["status"] == "REVIEW") == expected_review
    assert len(batch_result["result"]["outcomes"]) == 2
    with store.db.engine.connect() as c:
        routes = c.exec_driver_sql("SELECT status FROM atomic_entity_routes").scalars().all()
        assert len(routes) == 2 and "ACCEPTED" in routes
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM notes WHERE kind='CONCEPT' AND current_revision_id IS NOT NULL"
            ).scalar_one()
            >= 1
        )
        receipt = c.exec_driver_sql(
            "SELECT artifact_ref,artifact_sha256 FROM concept_entity_plans"
        ).one()
    artifact = json.loads(store.read(*receipt))
    if mode == "relation":
        assert artifact["unresolved_relations"]
    with store.repository.transaction() as c:
        store.repository.event(
            c,
            "ATOMIC_BATCH_CLOSED",
            batch["source_revision_id"],
            batch["source_revision_id"],
            {"routing_batch": batch},
            key="partial-duplicate",
        )
    for _ in range(20):
        if app.run_once() is None:
            break
    assert sum(op == "next.entity_plan" for op, _ in agent.calls) == 1
    assert convergence_report(store)["passed"] is (not expected_review)


def test_explicit_retry_replans_only_reviewed_atomics(store):
    from test_next_entity_concepts import Agent as EntityAgent

    _, _batch = closed_fixture(store)

    class RecoverableAgent(EntityAgent):
        failed = True

        def __init__(self):
            super().__init__()
            self.inputs = []

        def structured_json(self, *, operation, user, **kwargs):
            result = super().structured_json(operation=operation, user=user, **kwargs)
            if operation == "next.entity_plan":
                self.inputs.append(user["content"]["atomics"])
                if self.failed:
                    pending_target = result["assignments"][0]["target"]
                    result["new_entities"] = [
                        e for e in result["new_entities"] if e["ref"] != pending_target
                    ]
                    result["assignments"][0].update(
                        action="REVIEW",
                        target=None,
                        sections={},
                        reason="fixture needs confirmation",
                    )
            return result

    agent = RecoverableAgent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY", job_kinds=["ANALYZE_ATOMIC"])
    outcome = app.run_once()
    assert outcome["status"] == "REVIEW"
    agent.failed = False
    store.jobs.retry_review(outcome['job_id'])
    recovered = app.run_once()
    assert recovered["status"] == "SUCCEEDED", recovered
    assert len(agent.inputs) == 2 and len(agent.inputs[0]) == 2 and len(agent.inputs[1]) == 1


def test_source_invalidation_during_routing_cannot_accept_stale_inputs(store):
    from test_next_entity_concepts import Agent as EntityAgent

    source, _batch = closed_fixture(store)

    class InvalidatingAgent(EntityAgent):
        def structured_json(self, *, operation, **kwargs):
            result = super().structured_json(operation=operation, **kwargs)
            if operation == "next.entity_plan":
                store.set_source_deleted(source["source_id"], True)
            return result

    app = Application(
        store, agent=InvalidatingAgent(), index_mode="KEYWORD_ONLY", job_kinds=["ANALYZE_ATOMIC"]
    )
    outcome = app.run_once()
    assert outcome["status"] == "RETRY" and outcome["error"] == "Conflict"
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM atomic_entity_routes").scalar_one() == 0
    retry = app.run_once()
    assert retry["status"] == "SUCCEEDED"
    assert all(o["status"] == "SUPERSEDED_OR_INELIGIBLE" for o in retry["result"]["outcomes"])


def test_published_diagnostics_and_routing_schemas_match_runtime():
    import json
    from pathlib import Path

    from noodle_agent.next.concepts.entity_contracts import RoutingDecision
    from noodle_agent.next.retrieval.contracts import Diagnostics

    root = Path(__file__).resolve().parents[3] / "docs" / "schemas"
    assert (
        json.loads((root / "next-v1-p3p4.schema.json").read_text(encoding="utf-8"))["$defs"][
            "Diagnostics"
        ]
        == Diagnostics.model_json_schema()
    )
    assert (
        json.loads((root / "routing-decision-v1.schema.json").read_text(encoding="utf-8"))
        == RoutingDecision.model_json_schema()
    )


def test_native_batch_pause_and_resume_preserves_heads_and_root(source_prepared):
    from test_next_entity_concepts import Agent as EntityAgent
    from test_next_source_sections import Agent as SourceAgent
    from test_next_source_sections import Parser

    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.task_control import request_pause, resume
    from noodle_agent.next.worker import GenerationWorker

    store, lease, _ = source_prepared
    profile = {
        "parser": "source-test",
        "generation_layout": "source-sections-v1",
        "concept_layout": "entity-v2",
    }
    Checkpoints(store.repository, lease).save("profile", profile, profile)
    store.jobs.defer(lease, {"status": "FIXTURE_READY"})

    class PausingAgent(SourceAgent):
        def structured_json(self, *, operation, **kwargs):
            result = super().structured_json(operation=operation, **kwargs)
            if operation == "next.section_write":
                request_pause(store, lease.job_id)
                result.update(
                    summary="Fixture source description",
                    entity_hints={
                        "subject": "PCA",
                        "subjects": ["PCA"],
                        "context": "fixture",
                        "intent": "EXPLAIN",
                    },
                )
            return result

    agent = PausingAgent()
    worker = GenerationWorker(store, parser=Parser(), agent=agent, draft_concurrency=2)
    assert worker.run_once()["status"] == "PAUSED"
    with store.db.engine.connect() as c:
        heads = c.exec_driver_sql(
            "SELECT id,current_revision_id FROM notes WHERE kind='ATOMIC' ORDER BY id"
        ).all()
    assert len(heads) == 2
    maintenance = Application(
        store, agent=EntityAgent(), index_mode="KEYWORD_ONLY", job_kinds=["ANALYZE_ATOMIC"]
    )
    assert maintenance.run_once() is None
    resume(store, lease.job_id)

    class NoReplayParser(Parser):
        def submit(self, *args):
            pytest.fail("Resume must reuse its parse receipt")

        def download_and_normalize(self, *args):
            pytest.fail("Resume must reuse normalized units")

    assert (
        GenerationWorker(
            store, parser=NoReplayParser(), agent=agent, draft_concurrency=2
        ).run_once()["status"]
        == "SUCCEEDED"
    )
    assert agent.operations.count("next.section_write") == 2
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT id,current_revision_id FROM notes WHERE kind='ATOMIC' ORDER BY id"
            ).all()
            == heads
        )
    assert maintenance.run_once()["status"] == "SUCCEEDED"


def test_repair_requested_answer_diagnostics_are_valid_http(store, monkeypatch):
    from types import SimpleNamespace

    class RejectedAgent(Agent):
        def structured_json(self, *, operation, **kwargs):
            if operation == "next.answer_review":
                return {
                    "passed": False,
                    "issues": ["unsupported claim"],
                    "supported_claim_indexes": [],
                }
            return super().structured_json(operation=operation, **kwargs)

    atomic(store, "A", "梯度计算。")
    app = Application(store, agent=RejectedAgent(), index_mode="KEYWORD_ONLY")
    app.indexer.build("KEYWORD_ONLY")
    app.answerer.calls = SimpleNamespace(run=app.answerer.calls.run, journal=object())
    monkeypatch.setattr(
        "noodle_agent.next.quality_repair.request_retrieval_repair", lambda *args: True
    )
    with TestClient(create_app(application=app)) as client:
        response = client.post(
            "/internal/next/answer", json={"query": "梯度", "retrieval_mode": "KEYWORD_ONLY"}
        )
    assert response.status_code == 200
    result = AnswerResponse.model_validate(response.json())
    assert result.status == "COVERAGE_GAP" and result.diagnostics.knowledge_repair_requested is True


def test_replacement_coverage_explains_old_review_without_changing_queue(store):
    from test_next_entity_concepts import Agent as EntityAgent

    from noodle_agent.next.contracts import Review
    from noodle_agent.next.convergence import convergence_report
    from noodle_agent.next.navigation import frozen_candidate

    _, batch = closed_fixture(store)

    class PendingAgent(EntityAgent):
        pending = True

        def structured_json(self, *, operation, **kwargs):
            result = super().structured_json(operation=operation, **kwargs)
            if operation == "next.entity_plan" and self.pending:
                target = result["assignments"][0]["target"]
                result["new_entities"] = [e for e in result["new_entities"] if e["ref"] != target]
                result["assignments"][0].update(
                    action="REVIEW", target=None, sections={}, reason="fixture pending"
                )
            return result

    agent = PendingAgent()
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY", job_kinds=["ANALYZE_ATOMIC"])
    old = app.run_once()
    assert old["status"] == "REVIEW"
    first = batch["atomics"][0]
    candidate = frozen_candidate(store, first["revision_id"]).model_copy(
        update={"summary": "Updated source description"}
    )
    store.publish(
        candidate,
        expected_revision=first["revision_id"],
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="fixture",
            passed=True,
        ),
    )
    agent.pending = False
    assert app.run_once()["status"] == "SUCCEEDED"
    report = convergence_report(store, require_idle=False)
    coverage = report["superseded_jobs"][0]
    assert coverage["job_id"] == old["job_id"] and coverage["business_status"] == "SUPERSEDED"
    assert len(coverage["covered_inputs"]) == 2
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql("SELECT status FROM jobs WHERE id=?", (old["job_id"],)).scalar_one()
            == "REVIEW"
        )
