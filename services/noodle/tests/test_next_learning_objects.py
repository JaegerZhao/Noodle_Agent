"""Whole-path regressions for the working planner and all historical failures."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_next_entity_concepts import Agent as EntityAgent
from test_next_source_sections import Agent as WritingAgent
from test_next_source_sections import Parser, prepared  # noqa: F401

from noodle_agent.next.convergence import convergence_report
from noodle_agent.next.generation import Generation
from noodle_agent.next.learning_objects import (
    ObjectPlan,
    apply_confirmation,
    fragmentation,
    plan_window,
    validate_locations,
)
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.source_sections import units_from_blocks
from noodle_agent.next.verification import verify_current_generation


class Journal:
    def __init__(self):
        self.receipts = []

    def save(self, stage, inputs, receipt):
        self.receipts.append(receipt)


def test_page_outline_is_advisory_after_confirmation_and_shared_topic_parts_coalesce():
    units = units_from_blocks({"blocks": [{"id": str(i), "page": i + 1, "kind": "TEXT", "text": "Definition, training, example and output"} for i in range(20)]})
    fragmented = ObjectPlan(topics=[{"start_unit": u.unit_id, "title": "Method part " + str(i), "topic_key": str(i)} for i, u in enumerate(units)])
    assert fragmentation(fragmented, units)["resembles_page_outline"]
    assert apply_confirmation(fragmented, fragmented, units)
    combined = ObjectPlan(topics=[t.model_copy(update={"topic_key": "complete_method"}) for t in fragmented.topics])
    assert not fragmentation(combined, units)["resembles_page_outline"]
    assert apply_confirmation(fragmented, combined, units)


def test_real_46_object_confirmation_retains_granularity_feedback_without_a_second_gate():
    repo = Path(__file__).resolve().parents[3]
    root = repo / ".local-only/week4-throughput-20261023T192027Z"
    if not root.exists():
        pytest.skip("Optional immutable rejected granularity fixture")
    c = sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
    (ref,) = c.execute("SELECT artifact_ref FROM job_checkpoints WHERE stage LIKE 'source.learning.objects.%.acceptance'").fetchone()
    c.close()
    value = json.loads((root / "library" / ref).read_text(encoding="utf-8"))
    plan = ObjectPlan.model_validate(value["final"])
    parse = json.loads((repo / ".local-only/week4-frozen-2026-10-23/library/artifacts/parses/533c4a282a0e06f194ff6076deb629906678f2d427f838fc4b2377620a500263.json").read_text(encoding="utf-8"))
    units = [u for u in units_from_blocks(parse) if u.kind.upper() not in {"HEADER", "FOOTER", "PAGE_NUMBER"}]
    assert fragmentation(plan, units)["object_count"] == 46
    assert fragmentation(apply_confirmation(plan, plan, units), units)["resembles_page_outline"]


def test_excluded_navigation_image_joins_next_object_without_a_new_cover_atomic():
    units = units_from_blocks({"blocks": [
        {"id": "c", "page": 1, "kind": "HEADING", "text": "Course title"},
        {"id": "o", "page": 2, "kind": "HEADING", "text": "Outline"},
        {"id": "i", "page": 2, "kind": "IMAGE", "text": "", "asset_ref": "assets/unknown.png"},
        {"id": "t", "page": 2, "kind": "TEXT", "text": "Key terms"},
        {"id": "m", "page": 3, "kind": "TEXT", "text": "Actual definition and method"},
    ]})
    plan = ObjectPlan(topics=[
        {"start_unit": "u00000", "title": "Cover", "disposition": "EXCLUDE", "reason": "Cover"},
        {"start_unit": "u00001", "title": "Outline", "disposition": "EXCLUDE", "reason": "Navigation"},
        {"start_unit": "u00004", "title": "Method", "topic_key": "method"},
    ])
    final = apply_confirmation(plan, plan, units)
    assert [(t.start_unit, t.disposition) for t in final.topics] == [
        ("u00000", "EXCLUDE"), ("u00001", "EXCLUDE"), ("u00002", "CONTENT")]
    assert final.topics[-1].title == "Method"
    assert plan.topics[-1].start_unit == "u00004"  # Immutable model response.
    assert apply_confirmation(final, final, units) == final


@pytest.mark.parametrize("decorative", [False, True])
def test_image_only_exclusion_is_retained_unless_visually_verified_decorative(decorative):
    units = units_from_blocks({"blocks": [{"id": "i", "page": 1, "kind": "IMAGE", "text": "",
        "asset_ref": "assets/image.png", "visual": {"is_decorative": decorative}}]})
    plan = ObjectPlan(topics=[{"start_unit": "u00000", "title": "Cover", "disposition": "EXCLUDE", "reason": "Cover"}])
    assert apply_confirmation(plan, plan, units).topics[0].disposition == ("EXCLUDE" if decorative else "CONTENT")


def test_trailing_unknown_image_is_kept_with_preceding_content_and_invalid_ids_still_block():
    units = units_from_blocks({"blocks": [
        {"id": "a", "page": 1, "kind": "TEXT", "text": "Method"},
        {"id": "b", "page": 2, "kind": "IMAGE", "text": "", "asset_ref": "assets/image.png"}]})
    plan = ObjectPlan(topics=[{"start_unit": "u00000", "title": "Method"},
        {"start_unit": "u00001", "title": "Closing", "disposition": "EXCLUDE", "reason": "Closing"}])
    assert len(apply_confirmation(plan, plan, units).topics) == 1
    wrong = ObjectPlan(topics=[{"start_unit": "invented", "title": "Method"}])
    with pytest.raises(QualityFailure, match="unknown source"):
        apply_confirmation(plan, wrong, units)


def test_planning_does_not_require_images_to_be_recognized_or_formulas_to_be_copied():
    units = units_from_blocks({"blocks": [
        {"id": "d", "page": 1, "kind": "HEADING", "text": "A classifier assigns inputs to categories."},
        {"id": "f", "page": 1, "kind": "TEXT", "text": "$$\n" + "x+" * 200 + "y=1\n$$"},
        {"id": "i", "page": 2, "kind": "IMAGE", "text": "", "asset_ref": "assets/actual.png"},
    ]})
    raw = {"topics": [{"start_unit": "u00000", "title": "Classification"}]}
    operations, journal = [], Journal()

    def call(stage, operation, instruction, content, contract, validator, **kwargs):
        operations.append(operation)
        value = contract.model_validate(raw)
        validator(value)
        assert "teaching_support" not in str(contract.model_json_schema())
        return value

    result = plan_window(SimpleNamespace(call=call, journal=journal), units, [])
    assert len(result.sections) == 1
    assert operations == ["next.structure", "next.structure_exclusion_review"]
    assert journal.receipts[0]["pending_image_unit_ids"] == ["u00002"]


def test_confirmation_repairs_prefix_and_keeps_real_image_exclusion_guard():
    units = units_from_blocks({"blocks": [
        {"id": "c", "page": 1, "kind": "TEXT", "text": "Course identification"},
        {"id": "d", "page": 2, "kind": "TEXT", "text": "A real definition."},
    ]})
    first = ObjectPlan(topics=[{"start_unit": "u00001", "title": "Method"}])
    validate_locations(first, units)
    final = ObjectPlan(topics=[
        {"start_unit": "u00000", "title": "Cover", "disposition": "EXCLUDE", "reason": "Course identification"},
        {"start_unit": "u00001", "title": "Method"}])
    assert validate_locations(final, units, final=True) == final
    wrong = final.model_copy(update={"topics": list(reversed(final.topics))})
    with pytest.raises(QualityFailure, match="source order"):
        validate_locations(wrong, units, final=True)


def test_confirmation_keeps_unmodified_exclusions_and_allows_explicit_restoration():
    units = units_from_blocks({"blocks": [
        {"id": "c", "page": 1, "kind": "TEXT", "text": "Course identification"},
        {"id": "b", "page": 2, "kind": "TEXT", "text": "Definition and procedure"},
        {"id": "e", "page": 3, "kind": "TEXT", "text": "Copyright"},
    ]})
    original = ObjectPlan(topics=[
        {"start_unit": "u00000", "title": "Cover", "disposition": "EXCLUDE", "reason": "Administrative"},
        {"start_unit": "u00001", "title": "Method"},
        {"start_unit": "u00002", "title": "Copyright", "disposition": "EXCLUDE", "reason": "Administrative"}])
    content_only = ObjectPlan(topics=[{"start_unit": "u00001", "title": "Complete method"}])
    result = apply_confirmation(original, content_only, units)
    assert [t.disposition for t in result.topics] == ["EXCLUDE", "CONTENT", "EXCLUDE"]
    restored = ObjectPlan(topics=[{"start_unit": "u00000", "title": "Teaching introduction"},
        {"start_unit": "u00001", "title": "Method"}], restore_exclusions=["u00000"])
    assert apply_confirmation(original, restored, units).topics[0].disposition == "CONTENT"


def test_all_recorded_planning_failures_can_reach_final_source_range_acceptance():
    repo = Path(__file__).resolve().parents[3]
    baseline = repo / ".local-only/week4-throughput-20261023T140703Z"
    if not baseline.exists():
        pytest.skip("Optional immutable historical fixtures")

    def checkpoints(root):
        c = sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
        rows = c.execute("SELECT stage,artifact_ref FROM job_checkpoints WHERE state='DONE' ORDER BY created_at").fetchall()
        c.close()
        return [(stage, json.loads((root / "library" / ref).read_text(encoding="utf-8"))) for stage, ref in rows]

    old = checkpoints(baseline)
    accepted = next(value for stage, value in reversed(old) if stage.startswith("source.boundaries."))
    parsed = next(value for stage, value in old if stage == "parse.normalized")
    units = [u for u in units_from_blocks(parsed["artifact"]) if u.kind.upper() not in {"HEADER", "FOOTER", "PAGE_NUMBER"}]
    seen = 0
    for name in ("week4-throughput-20261023T153613Z", "week4-throughput-20261023T160536Z", "week4-throughput-20261023T162629Z"):
        for stage, raw in checkpoints(repo / ".local-only" / name):
            if not (stage.startswith("source.boundaries.") or stage.startswith("source.learning.") and ".plan." in stage):
                continue
            operations = []

            def call(_stage, operation, instruction, payload, contract, validator, *, operations=operations, raw=raw, **kwargs):
                operations.append(operation)
                value = contract.model_validate(raw if operation == "next.structure" else accepted)
                validator(value)
                return value

            journal = Journal()
            result = plan_window(SimpleNamespace(call=call, journal=journal), units, [])
            expected = json.loads((baseline / "source-plan.json").read_text(encoding="utf-8"))
            assert sum(s.disposition == "CONTENT" for s in result.sections) == len(expected["plans"])
            assert len(operations) == 2 and journal.receipts
            seen += 1
    assert seen == 4


def test_new_planner_reaches_atomic_concept_index_and_export(prepared, monkeypatch):  # noqa: F811
    from noodle_agent.next import runtime
    from noodle_agent.next.runtime import Application
    from noodle_agent.next.vault import reading_zip

    store, lease, _ = prepared
    writer = WritingAgent()
    entity = EntityAgent()

    class WholeAgent:
        name = "whole-path-fixture"
        fast_model = "fixture-fast"
        pro_model = "fixture-pro"

        def __init__(self):
            self.operations = []

        def structured_json(self, *, operation, user, **kwargs):
            self.operations.append(operation)
            data = user["content"]
            if operation in {"next.structure", "next.structure_exclusion_review"}:
                return {"topics": [{"start_unit": "u00000", "title": "Term frequency", "topic_key": "tf"},
                                   {"start_unit": "u00002", "title": "Inverse document frequency", "topic_key": "idf"}]}
            if operation == "next.assembly_review":
                return {"checked_plans": [a["plan_key"] for a in data["assembly"]],
                        "checked_omissions": [], "findings": []}
            if operation == "next.section_write":
                value = writer.structured_json(operation=operation, user=user, **kwargs)
                value.update(summary="This note explains the supplied definition and its conditions.",
                    entity_hints={"intent": "EXPLAIN", "subject": "IDF" if "Inverse" in value["title"] else "TF", "context": "NLP", "subjects": []})
                return value
            if operation == "next.entity_write":
                return {"chapters": {s["key"]: {"markdown": "\n\n".join(f["text"] for f in s["fragments"]), "images": {}} for s in data["chapters"]}}
            if operation == "next.entity_review":
                return {"chapters": {s["key"]: {"supported_fragment_ids": [f["fragment_id"] for f in s["fragments"]], "checked_image_ids": [], "findings": []} for s in data["chapters"]}}
            if operation == "next.organization_plan":
                return {"links": []}
            return entity.structured_json(operation=operation, user=user, **kwargs)

    agent = WholeAgent()
    profile = {"parser": Parser.name, "generation_layout": "source-sections-v1", "concept_layout": "entity-v2",
        "execution_policy": "pipeline-throughput-v1", "checking_policy": "single-check-v1",
        "quality_policy": "structured-quality-v2", "review_policy": "essential-v1", "planning_mode": "learning-objects-v1"}
    result = Generation(store, Parser(), agent, lease, profile=profile, concurrency=2).run()
    assert len(result["notes"]) == 2
    store.jobs.finish(lease, result)
    monkeypatch.setattr(runtime, "Calls", lambda store, agent, journal=None, **kwargs: Calls(
        store, agent, journal, policy="structured-check-v2", review_policy="essential-v1"))
    app = Application(store, agent=agent, index_mode="KEYWORD_ONLY")
    for _ in range(150):
        outcome = app.run_once("whole-test")
        if outcome is None:
            break
        assert outcome["status"] != "REVIEW", outcome
    else:
        pytest.fail("Whole pipeline did not become idle")
    report = convergence_report(store)
    assert report["passed"], report
    assert report["eligible_counts"] == {"ATOMIC": 2, "CONCEPT": 2}
    assert verify_current_generation(store, lease.job_id)["passed"]
    assert reading_zip(store).startswith(b"PK")
    assert agent.operations.count("next.structure_exclusion_review") == 1
    assert "next.entity_plan_review" not in agent.operations and "next.organization_review" not in agent.operations
