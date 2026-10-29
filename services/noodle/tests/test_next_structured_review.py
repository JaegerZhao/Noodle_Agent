"""Regression gates from frozen W4 R2 failures; no provider calls."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from noodle_agent.next.assembly_generation import SourceSectionPipeline
from noodle_agent.next.concepts.canonical_relations import CanonicalDecision, expand_canonical
from noodle_agent.next.concepts.entity_contracts import Chapter
from noodle_agent.next.concepts.entity_patch_v2 import write_group
from noodle_agent.next.concepts.entity_routing import validate_plan
from noodle_agent.next.json_format import close_outer_object
from noodle_agent.next.organization_v2 import OrganizationProposal, compile_proposal
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.section_contracts import Boundaries
from noodle_agent.next.source_sections import units_from_blocks, validate_assisted_boundaries
from noodle_agent.next.structured_quality import QualityFinding, fixed_chapters, validate_findings


def test_fixed_concept_role_schema_rejects_source_section_keys():
    schema = fixed_chapters(["entity_principles", "entity_examples"], Chapter)
    valid = {"chapters": {k: {"markdown": "Supported text"} for k in ("entity_principles", "entity_examples")}}
    assert set(schema.model_validate(valid).chapters) == {"entity_principles", "entity_examples"}
    with pytest.raises(ValidationError):
        schema.model_validate({"chapters": {"elm-code": {"markdown": "Wrong namespace"}}})
    with pytest.raises(ValidationError):
        schema.model_validate({"chapters": {"entity_principles": {"markdown": "Missing role"}}})


@pytest.mark.parametrize("category", ["ADVISORY", "SOURCE_ISSUE"])
def test_source_problem_and_advice_are_not_candidate_patch_instructions(category):
    finding = QualityFinding(category=category, issue="Source ambiguity; candidate already explains it",
        source_quote="Ambiguous source", source_ids=["f"])
    validate_findings([finding], "Candidate explicitly states the uncertainty", {"f": "Ambiguous source"})
    assert finding.category != "CANDIDATE_ERROR"


def test_candidate_error_requires_current_text_and_omission_requires_support():
    with pytest.raises(QualityFailure, match="current candidate"):
        validate_findings([QualityFinding(category="CANDIDATE_ERROR", issue="Old assertion", candidate_quote="Old false text")], "Correct new text", {})
    with pytest.raises(QualityFailure, match="actual source"):
        validate_findings([QualityFinding(category="CANDIDATE_ERROR", kind="OMISSION", issue="Missing condition")], "Some text", {})


class PatchCalls:
    policy = "structured-check-v2"
    journal = None

    def __init__(self, source_issue=False):
        self.writes = []
        self.source_issue = source_issue

    def begin_unit(self, *args, **kwargs):
        pass

    def run(self, operation, instruction, content, contract, check=None, **kwargs):
        specs = content["chapters"]
        if operation == "next.entity_review":
            result = {"chapters": {s["key"]: {"supported_fragment_ids": [f["fragment_id"] for f in s["fragments"]], "checked_image_ids": [],
                "findings": ([{"category": "SOURCE_ISSUE", "issue": "Actual upstream contradiction", "source_quote": "Source condition", "source_ids": [s["key"]]}] if self.source_issue else
                    [{"category": "CANDIDATE_ERROR", "issue": "Incorrect condition", "candidate_quote": "Old explanation", "source_ids": [s["key"]]}] if s["key"] == "b" and len(self.writes) == 1 else
                    [{"category": "ADVISORY", "issue": "Verified calculation is consistent; no blocker"}])} for s in specs}}
        else:
            self.writes.append([s["key"] for s in specs])
            result = {"chapters": {s["key"]: {"markdown": "Correct explanation" if len(self.writes) > 1 else "Old explanation"} for s in specs}}
        value = contract.model_validate(result)
        if check:
            check(value)
        return value


def specs():
    return [{"key": k, "title": k, "question": "Explain", "scope": {"includes": ["Explain"]},
             "fragments": [{"fragment_id": k, "sha256": k, "text": "Source condition", "evidence_ids": []}]} for k in ("a", "b")]


def test_typed_patch_reuses_good_chapter_and_repairs_only_actual_blocker():
    calls = PatchCalls()
    result = write_group(None, calls, specs())
    assert calls.writes == [["a", "b"], ["b"]]
    assert result["a"][0] == "Old explanation" and result["b"][0] == "Correct explanation"


def test_confirmed_upstream_issue_uses_repair_channel_not_fragment_mutation(monkeypatch):
    from noodle_agent.next.quality_repair import UpstreamRepairPending
    captured = []
    monkeypatch.setattr("noodle_agent.next.quality_repair.request_upstream_repair", lambda store, calls, fragments, issues: captured.extend(issues) or True)
    values = specs()
    with pytest.raises(UpstreamRepairPending):
        write_group(None, PatchCalls(source_issue=True), values)
    assert captured and values[0]["fragments"][0]["text"] == "Source condition"


def decision(relation):
    return CanonicalDecision(assignments=[{"atomic": f"A{i}", "action": "CREATE", "target": f"N{i}", "reason": "Supported topic"} for i in range(3)],
        new_entities=[{"ref": f"N{i}", "title": f"Topic {i}", "domain": "test", "identity_key": f"t{i}", "description": "Source-supported object"} for i in range(3)], relations=[relation])


def cards():
    return [{"id": f"a{i}", "sections": [{"id": "s"}]} for i in range(3)]


def test_two_endpoint_support_keeps_ownership_and_derives_child_storage_direction():
    d = decision({"kind": "TOPIC", "parent_ref": "N0", "child_ref": "N1", "supports": [
        {"atomic_ref": "A0", "endpoint": "PARENT"}, {"atomic_ref": "A1", "endpoint": "CHILD"}], "reason": "Specific method under a problem"})
    plan = expand_canonical(d, cards(), [])
    validate_plan(plan, cards(), [])
    relation = plan.relations[0]
    assert relation.source_key == "t1" and relation.target_key == "t0" and relation.navigation_role == "TOPIC_PARENT"
    assert relation.support_atomic_ids == ["a1"] and relation.target_support_atomic_ids == ["a0"]


@pytest.mark.parametrize("unknown", [True, False])
def test_bad_optional_relation_is_explicit_suggestion_and_does_not_forge_identity(unknown):
    d = decision({"kind": "RELATED_TO", "source_ref": "N0", "target_ref": "UNDECLARED" if unknown else "N1",
        "supports": [{"atomic_ref": "A2", "endpoint": "SOURCE"}], "reason": "Potential relation"})
    plan = expand_canonical(d, cards(), [])
    assert not plan.relations and plan._unresolved_relations[0]["status"] == "SUGGESTED"
    assert all(route.entity_key for route in plan.routes.values())


def test_organization_computes_roots_and_direction_from_semantics_and_evidence():
    p = OrganizationProposal(links=[{"parent_note_id": "parent", "child_note_id": child,
        "support_revision_ids": [child + "-r"], "reason": "Child is a specific method"} for child in ("c1", "c2")])
    nodes = {k: {} for k in ("parent", "c1", "c2")}
    support = {k: {k + "-r"} for k in nodes}
    links, roots = compile_proposal(p, nodes, support, list(nodes), set())
    assert roots == ["parent"] and all(l.navigation_role == "TOPIC_PARENT" for l in links)
    assert {l.owner_note_id for l in links} == {"c1", "c2"}
    cyclic = OrganizationProposal(links=[{"parent_note_id": "c1", "child_note_id": "parent", "support_revision_ids": ["c1-r"], "reason": "Bad cycle"}])
    with pytest.raises(QualityFailure, match="cycle"):
        compile_proposal(cyclic, nodes, support, ["parent", "c2"], {("parent", "c1")})


@pytest.mark.parametrize("text,finish,accepted", [
    ('{"chapters":{"a":{"markdown":"complete"}}', "stop", True),
    ('{"chapters":{"a":{"markdown":"complete"}}', "length", False),
    ('{"chapters":{"a":{"markdown":"truncated', "stop", False),
    ('{"chapters":{"a":{},', "stop", False),
    ('{"chapters":{"a":{}}', "stop", True),
    ('{"chapters":{}}', "stop", False),
    ('{"chapters":{"a":"bad"quote"}}', "stop", False),
])
def test_json_recovery_is_only_one_outer_object_closer(text, finish, accepted):
    recovered = close_outer_object(text, finish)
    assert (recovered is not None) == accepted
    if recovered:
        assert recovered[1] == text + "}"


def test_v6_heading_cannot_be_its_own_teaching_proof():
    units = units_from_blocks({"blocks": [{"id": "title", "page": 1, "text": "Methods overview", "kind": "HEADING"}]})
    plan = Boundaries(sections=[{"start_unit": units[0].unit_id, "title": "Methods", "teaching_support": [{"unit_id": units[0].unit_id, "quote": "Methods overview", "role": "DEFINITION"}]}])
    with pytest.raises(QualityFailure, match="bare heading"):
        validate_assisted_boundaries(plan, units, True, True, True)


def test_atomic_semantic_check_repairs_a_prose_formula_contradiction():
    record = {"key": "p", "draft": {"title": "Loss", "summary": "Loss conditions", "sections": [{"key": "s", "title": "Conditions", "markdown": "$L=1-z$ when z<1. Same sign always means zero."}], "assignments": {"u": "s"}, "omissions": {}},
        "inputs": {"units": [{"unit_id": "u", "text": "$L=\\max(0,1-z)$"}]}, "contexts": [], "evidence": {"u": "e"}}
    seen = []

    def call(stage, operation, system, content, contract, validator, **kwargs):
        seen.append(kwargs["use_pro_model"])
        raw = {"checked_plans": ["p"], "checked_omissions": [], "findings": []}
        if len(seen) == 1:
            raw["findings"] = [{"plan_key": "p", "section_key": "s", "category": "CANDIDATE_ERROR", "issue": "Positive sign alone is insufficient", "candidate_quote": "Same sign always means zero.", "source_ids": ["u"]}]
        value = contract.model_validate(raw)
        validator(value)
        return value

    g = SimpleNamespace(store=None, parsing=SimpleNamespace(vision=None), profile={"checking_policy": "single-check-v1"}, call=call)
    pipeline = SourceSectionPipeline(g)
    pipeline.repair_records = lambda values, issues, **kwargs: values[0]["draft"]["sections"][0].update(markdown="$L=1-z$ for z<1, so positive z below one still has loss.")
    _records, report, _digest = pipeline.typed_review([record], "batch")
    assert report.passed and seen == [True, True]


def test_native_queue_builds_typed_concepts_and_preserves_optional_suggestions(tmp_path):
    from test_next_convergence import closed_fixture
    from test_next_entity_concepts import Agent

    from noodle_agent.next.convergence import convergence_report
    from noodle_agent.next.runtime import Application
    from noodle_agent.next.store import Store

    class StructuredAgent(Agent):
        def structured_json(self, operation, user, **kwargs):
            data = user["content"]
            if operation == "next.entity_write":
                return {"chapters": {s["key"]: {"markdown": "\n\n".join(f["text"] for f in s["fragments"]), "images": {}} for s in data["chapters"]}}
            if operation == "next.entity_review":
                return {"chapters": {s["key"]: {"supported_fragment_ids": [f["fragment_id"] for f in s["fragments"]], "checked_image_ids": [], "findings": []} for s in data["chapters"]}}
            if operation == "next.organization_plan":
                return {"links": []}
            if operation == "next.organization_review":
                return {"passed": True, "issues": [], "checked_root_ids": data["roots"], "checked_link_indexes": [],
                    "independent_roots": [{"note_id": i, "reason": "Independent supported object"} for i in data["remaining_root_ids"]]}
            value = super().structured_json(operation, user, **kwargs)
            if operation == "next.entity_plan":
                value["relations"] = [{"kind": "RELATED_TO", "source_ref": "N0", "target_ref": "UNDECLARED", "supports": [{"atomic_ref": "A0", "endpoint": "SOURCE"}], "reason": "Unresolved optional mention"}]
            return value

    store = Store(tmp_path / "native-v2")
    try:
        closed_fixture(store)
        app = Application(store, settings=SimpleNamespace(embedding_api_key=None, embedding_model=None), agent=StructuredAgent(), index_mode="KEYWORD_ONLY")
        results = []
        for _ in range(100):
            result = app.run_once()
            if result is None:
                break
            results.append(result)
        assert results and all(r["status"] == "SUCCEEDED" for r in results), results
        report = convergence_report(store)
        assert report["passed"] and report["eligible_counts"]["CONCEPT"] == 2
        assert report["optional_relation_suggestions"] and not report["routing_reviews"]
    finally:
        store.close()


def test_failed_upstream_repair_cannot_be_deferred_forever(tmp_path):
    from test_next_convergence import closed_fixture

    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.concepts.fragments import fragments
    from noodle_agent.next.events import dispatch
    from noodle_agent.next.quality_repair import QualityTriage, request_upstream_repair
    from noodle_agent.next.store import Store

    store = Store(tmp_path / "bounded-upstream")
    try:
        _source, batch = closed_fixture(store)
        dispatch(store)
        lease = store.jobs.claim("analysis", kind="ANALYZE_ATOMIC")
        note = store.get_note(batch["atomics"][0]["note_id"])
        material = fragments(store, note)
        calls = SimpleNamespace(journal=Checkpoints(store.repository, lease), run=lambda *a, **kw: QualityTriage(problems=[{"fragment_id": material[0]["fragment_id"], "issue": "Actual source contradiction"}]))
        assert request_upstream_repair(store, calls, material, ["Candidate finding"])
        dispatch(store)
        repair = store.jobs.claim("repair", kind="REPAIR_ATOMIC")
        assert repair is not None
        store.jobs.fail(repair, {"code": "FIXTURE_REPAIR_REJECTED"}, retry=False)
        with pytest.raises(QualityFailure, match="already in REVIEW"):
            request_upstream_repair(store, calls, material, ["Same finding"])
    finally:
        store.close()
