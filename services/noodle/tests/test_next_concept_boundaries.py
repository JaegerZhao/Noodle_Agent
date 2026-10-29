"""Identity context survives source batches without adding a semantic reviewer."""

from types import SimpleNamespace

from test_next_entity_concepts import Agent, publish_atomic, store  # noqa: F401

from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.concepts.builder import published_scope
from noodle_agent.next.contracts import Scope
from noodle_agent.next.repository import one
from noodle_agent.next.runtime import Application


def test_outline_and_entity_scope_survive_a_later_source_batch(store):  # noqa: F811
    source = store.add_source(b"SVM separates data. Implementation uses the same SVM.", "SVM lecture", kind="AUTHOR")
    first = publish_atomic(store, source, "SVM geometry", "SVM", "SVM separates data.")
    with store.db.engine.connect() as c:
        event = one(c, "SELECT id FROM events WHERE event_type='SOURCE_ADDED'")
    store.jobs.enqueue(event["id"], "GENERATE_ATOMICS", {"revision_id": source["revision_id"]})
    lease = store.jobs.claim("fixture-plan", kind="GENERATE_ATOMICS")
    Checkpoints(store.repository, lease).save("source.sections.plan.fixture", {}, {"plans": [
        {"key": "geometry", "title": "SVM geometry", "parent_id": "svm", "parent_title": "Support Vector Machines"},
        {"key": "implementation", "title": "SVM implementation", "parent_id": "svm", "parent_title": "Support Vector Machines"},
    ]})
    store.jobs.finish(lease, {"status": "FIXTURE_PLAN"})

    class BoundaryAgent(Agent):
        def __init__(self):
            super().__init__()
            self.packets = []

        def structured_json(self, operation, user, **kwargs):
            assert operation == "next.entity_plan", "No second semantic reviewer is expected"
            assert kwargs.get("use_pro_model") is True
            self.packets.append(user["content"])
            data = user["content"]
            existing = data["candidates"]
            target = existing[0]["ref"] if existing else "N0"
            return {"new_entities": [] if existing else [{"ref": "N0", "title": "SVM", "domain": "ml",
                "identity_key": "svm", "description": "Support vector machines.", "scope_includes": ["Geometry", "Implementation"]}],
                "assignments": {a["ref"]: {"action": "REUSE" if existing else "CREATE", "target": target,
                    "topic": a["title"], "role": "examples" if existing else "principles", "reason": "Same object, different internal topic"}
                    for a in data["atomics"]}}

    agent = BoundaryAgent()
    app = Application(store, settings=SimpleNamespace(embedding_api_key=None, embedding_model=None), agent=agent, job_kinds=("ANALYZE_ATOMIC",))
    assert app.run_once("first")["status"] == "SUCCEEDED"
    second = publish_atomic(store, source, "SVM implementation", "Soft-margin SVM", "Implementation uses the same SVM.")
    assert app.run_once("second")["status"] == "SUCCEEDED"
    assert len(agent.packets) == 2
    assert len(agent.packets[0]["source_context"]["topics"]) == 2
    assert agent.packets[1]["source_context"]["same_source_bindings"][0]["atomic_title"] == "SVM geometry"
    assert agent.packets[1]["candidates"][0]["scope"]["includes"] == ["Geometry", "Implementation"]
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM notes WHERE kind='CONCEPT'").scalar_one() == 1
        assert c.exec_driver_sql("SELECT count(DISTINCT concept_id) FROM atomic_entity_routes").scalar_one() == 1
        assert c.exec_driver_sql("SELECT count(*) FROM contribution_decisions WHERE json_extract(decision_json,'$.topic') IS NOT NULL").scalar_one() == 2
        import json
        scheduled = c.exec_driver_sql("SELECT payload_json FROM events WHERE event_type='ORGANIZATION_REVIEW_REQUESTED'").scalars().all()
        assert scheduled and all(len(json.loads(v)['concept_ids'])==1 for v in scheduled)
    assert first != second


def test_scope_keeps_identity_and_new_topics_without_treating_them_as_aliases():
    result = published_scope(Scope(includes=["Support Vector Machines"]), [], ["Hard margin", "Soft margin", "Hard margin"])
    assert result.includes == ["Support Vector Machines", "Hard margin", "Soft margin"]


def test_candidate_scope_contains_initial_and_published_coverage():
    from noodle_agent.next.concepts.routing_protocol import coverage

    assert coverage({"definition": {"scope": {"includes": ["SVM"]}}, "scope": {"includes": ["SVM", "Kernel introduction"]}}) == {
        "includes": ["SVM", "Kernel introduction"]}
