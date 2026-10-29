"""Regress real layout false positives and enforce the one-review/one-patch budget."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_next_source_sections import Parser, prepared  # noqa: F401

from noodle_agent.next.atomic_planning import coalesce_topics, page_cards
from noodle_agent.next.contracts import canonical
from noodle_agent.next.generation import Generation
from noodle_agent.next.learning_plan import (
    Patch,
    TopicPlan,
    apply_patch,
    plan_window,
    program_issues,
    source_cards,
)
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.section_contracts import Boundaries
from noodle_agent.next.segmentation import tokens
from noodle_agent.next.source_sections import units_from_blocks


def source():
    return units_from_blocks({"blocks": [
        {"id": "nav", "page": 1, "kind": "HEADING", "text": "Methods overview"},
        {"id": "definition", "page": 2, "kind": "HEADING", "text": "A classifier assigns inputs to predefined categories."},
        {"id": "example", "page": 3, "kind": "TEXT", "text": "The worked example assigns an input to category A."},
    ]})


def topic(start="u00001", unit="u00001", quote="A classifier assigns inputs to predefined categories.", **kwargs):
    return {"start_unit": start, "title": "Classification", "topic_key": "classification",
            "reason": "Explain the classification learning problem", "teaching_support": [
                {"unit_id": unit, "quote": quote, "role": "DEFINITION"}], **kwargs}


class Journal:
    def __init__(self):
        self.receipts = []

    def save(self, stage, inputs, receipt):
        self.receipts.append(receipt)


def generator(*, invalid_patch=False, advisory_only=False):
    operations = []
    journal = Journal()

    def call(stage, operation, instruction, inputs, contract, validator=None, **kwargs):
        operations.append(operation)
        assert kwargs == {"use_pro_model": True, "max_attempts": 1}
        if operation == "next.structure":
            value = {"sections": [topic("u00000", "u00000", "Methods overview"), topic()]}
        elif operation == "next.structure_exclusion_review":
            value = {"checked_starts": ["u00000", "u00001"], "findings": [
                {"category": "ADVISORY" if advisory_only else "BLOCKING", "range_starts": ["u00000"], "issue": "Pure navigation has no teaching content"}]}
        elif operation == "next.structure_patch":
            value = {"edits": [{"replace_starts": ["u00000"], "sections": [
                topic("u00000", "u00000", "invented") if invalid_patch else {
                    "start_unit": "u00000", "title": "Navigation", "disposition": "EXCLUDE", "reason": "Pure navigation"}]}]}
        else:
            raise AssertionError("Unexpected model call, including any post-patch semantic review")
        result = contract.model_validate(value)
        if validator:
            validator(result)
        return result

    return SimpleNamespace(call=call, journal=journal), operations


def test_valid_definition_labeled_heading_is_not_rejected_by_program():
    units = source()[1:]
    value = Boundaries(sections=[topic()])
    assert not program_issues(value, units)


def test_semantic_navigation_error_is_patched_once_without_second_model_review():
    # Distinct keys prevent accidental coalescing of the invalid navigation range.
    g, operations = generator()
    original = g.call

    def distinct_keys(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[1] == "next.structure":
            value = value.model_copy(update={"sections": [value.sections[0].model_copy(update={"topic_key": "navigation"}), value.sections[1]]})
        return value

    g.call = distinct_keys
    result = plan_window(g, source())
    assert [s.disposition for s in result.sections] == ["EXCLUDE", "CONTENT"]
    assert operations == ["next.structure", "next.structure_exclusion_review", "next.structure_patch"]
    assert g.journal.receipts[-1]["patched"] is True
    assert g.journal.receipts[-1]["post_patch_semantic_review"] is False


def test_invalid_repair_remains_blocked_without_another_model_attempt():
    g, operations = generator(invalid_patch=True)
    original = g.call

    def distinct_keys(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[1] == "next.structure":
            value = value.model_copy(update={"sections": [value.sections[0].model_copy(update={"topic_key": "navigation"}), value.sections[1]]})
        return value

    g.call = distinct_keys
    with pytest.raises(QualityFailure, match="deterministic acceptance"):
        plan_window(g, source())
    assert len(operations) == 3 and not g.journal.receipts


def test_merged_ranges_retain_child_witnesses():
    values = source()[1:]
    value = Boundaries(sections=[topic(), topic("u00002", "u00002", "The worked example assigns an input to category A.")])
    result = coalesce_topics(value, values)
    assert len(result.sections) == 1
    assert [p.unit_id for p in result.sections[0].teaching_support] == ["u00001", "u00002"]
    assert not program_issues(result, values)


def test_local_patch_cannot_change_accepted_neighbor_or_move_outer_bounds():
    values = source()
    initial = Boundaries(sections=[topic("u00000", "u00000", "Methods overview"), topic()])
    patch = Patch(edits=[{"replace_starts": ["u00000", "u00001"], "sections": [topic("u00000", "u00001")]}])
    with pytest.raises(QualityFailure, match="unrelated accepted"):
        apply_patch(initial, patch, values, {"u00000"})
    shifted = Patch(edits=[{"replace_starts": ["u00000"], "sections": [topic()]}])
    with pytest.raises(QualityFailure, match="Invalid|first unit"):
        apply_patch(initial, shifted, values, {"u00000"})


def test_image_locator_is_never_semantic_proof_but_image_can_wait_for_vision():
    values = units_from_blocks({"blocks": [{"id": "img", "page": 1, "kind": "IMAGE", "text": "", "asset_ref": "assets/test.png"}]})
    bad = Boundaries(sections=[topic("u00000", "u00000", values[0].text)])
    assert program_issues(bad, values)[0]["code"] == "IMAGE_PLACEHOLDER_WITNESS"
    pending = Boundaries(sections=[topic("u00000", support_state="PENDING_VISION", teaching_support=[])])
    assert not program_issues(pending, values)
    assert "assets/test.png" not in canonical(source_cards(values))


def test_plan_schema_prevents_full_source_echo_in_witnesses():
    with pytest.raises(ValidationError):
        TopicPlan(sections=[topic(quote="x" * 181)])
    item = topic()
    item["teaching_support"] *= 4
    with pytest.raises(ValidationError):
        TopicPlan(sections=[item])


def test_explicit_single_attempt_does_not_repeat_invalid_model_output(prepared):  # noqa: F811
    store, lease, _ = prepared

    class InvalidAgent:
        calls = 0

        def structured_json(self, **kwargs):
            self.calls += 1
            return {"unexpected": "Invalid plan"}

    agent = InvalidAgent()
    g = Generation(store, Parser(), agent, lease, profile={"checking_policy": "single-check-v1"})
    with pytest.raises(QualityFailure, match="after 1 checked attempts"):
        g.call("source.test.single", "next.structure", "Plan", {}, TopicPlan, max_attempts=1)
    assert agent.calls == 1


def test_real_r3_definition_false_positive_and_context_duplication_regression():
    root = Path(__file__).resolve().parents[3] / ".local-only/week4-throughput-20261023T153613Z"
    baseline = root.parent / "week4-frozen-2026-10-23/library/artifacts/parses/533c4a282a0e06f194ff6076deb629906678f2d427f838fc4b2377620a500263.json"
    if not baseline.exists():
        pytest.skip("Optional immutable local replay fixture")
    values = [u for u in units_from_blocks(json.loads(baseline.read_text(encoding="utf-8"))) if u.kind.upper() not in {"HEADER", "FOOTER", "PAGE_NUMBER"}]
    raw_path = root / "library/artifacts/checkpoints/9cc2a7b41f08a9295be203994d37ba3a7a06ab4d3100c9914501f2344ab113bd.json"
    raw = Boundaries.model_validate_json(raw_path.read_text(encoding="utf-8"))
    issues = program_issues(coalesce_topics(raw, values), values)
    assert not any(i.get("unit_id") == "u00008" for i in issues)
    assert any(i["code"] == "IMAGE_PLACEHOLDER_WITNESS" for i in issues)
    assert tokens(canonical(source_cards(values))) < tokens(canonical(page_cards(values)))
