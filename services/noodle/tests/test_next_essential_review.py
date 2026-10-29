"""Essential checks survive relaxation; real planning failures reach one repair."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_next_structured_review import PatchCalls, cards, decision, specs

from noodle_agent.next.assembly_generation import SourceSectionPipeline
from noodle_agent.next.concepts.entity_patch_v2 import write_group
from noodle_agent.next.concepts.entity_routing import decide
from noodle_agent.next.learning_plan_v8 import (
    Plan,
    bind,
    plan_window,
    prepare,
    problems,
)
from noodle_agent.next.organization_v2 import review as organization_review
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.source_sections import units_from_blocks


class Journal:
    def __init__(self):
        self.receipts = []

    def save(self, stage, inputs, value):
        self.receipts.append(value)


def test_initial_contract_errors_reach_one_patch_and_evidence_is_program_bound():
    formula = "$$\n" + "x + " * 90 + "y=1\n$$"
    units = units_from_blocks(
        {
            "blocks": [
                {
                    "id": "cover",
                    "page": 1,
                    "kind": "HEADING",
                    "text": "Course identification",
                },
                {
                    "id": "definition",
                    "page": 2,
                    "kind": "HEADING",
                    "text": "The method minimizes a loss under its stated conditions.",
                },
                {"id": "formula", "page": 2, "kind": "TEXT", "text": formula},
                {
                    "id": "copyright",
                    "page": 3,
                    "kind": "TEXT",
                    "text": "Copyright all rights reserved",
                },
            ]
        }
    )
    raw = {
        "sections": [
            {
                "start_unit": "u00001",
                "title": "Method",
                "topic_key": "method",
                "teaching_support": [
                    {
                        "unit_id": "u00002",
                        "quote": formula.replace("\n", "\\n"),
                        "role": "FORMULA",
                    }
                ],
            }
        ]
    }
    original = json.dumps(raw)
    operations, journal = [], Journal()

    def call(
        stage, operation, instruction, payload, contract, validator=None, **kwargs
    ):
        operations.append(operation)
        assert kwargs == {"use_pro_model": True, "max_attempts": 1}
        if operation == "next.structure":
            value = raw
        elif operation == "next.structure_exclusion_review":
            assert any(
                i["code"] == "UNCOVERED_SOURCE_RANGE" for i in payload["program_issues"]
            )
            value = {
                "findings": [
                    {
                        "category": "BLOCKING",
                        "range_starts": ["u00001"],
                        "issue": "Separate trailing copyright",
                    }
                ]
            }
        elif operation == "next.structure_patch":
            value = {
                "edits": [
                    {
                        "replace_starts": ["u00000"],
                        "sections": [
                            {
                                "start_unit": "u00000",
                                "title": "Cover",
                                "disposition": "EXCLUDE",
                                "reason": "Course identification",
                            }
                        ],
                    },
                    {
                        "replace_starts": ["u00001"],
                        "sections": [
                            {
                                "start_unit": "u00001",
                                "title": "Method",
                                "topic_key": "method",
                                "teaching_units": ["u00001", "u00002"],
                            },
                            {
                                "start_unit": "u00003",
                                "title": "Copyright",
                                "disposition": "EXCLUDE",
                                "reason": "Pure copyright",
                            },
                        ],
                    },
                ]
            }
        else:
            raise AssertionError("No second semantic review permitted")
        result = contract.model_validate(value)
        if validator:
            validator(result)
        return result

    result = plan_window(SimpleNamespace(call=call, journal=journal), units)
    assert operations == [
        "next.structure",
        "next.structure_exclusion_review",
        "next.structure_patch",
    ]
    assert [s.disposition for s in result.sections] == ["EXCLUDE", "CONTENT", "EXCLUDE"]
    assert (
        result.sections[1].teaching_support[1].quote == formula and len(formula) > 330
    )
    assert json.dumps(raw) == original
    assert journal.receipts[-1]["post_patch_semantic_review"] is False


def test_r4_raw_response_no_long_quote_or_newline_gate_and_prefix_is_repairable():
    root = (
        Path(__file__).resolve().parents[3]
        / ".local-only/week4-throughput-20261023T160536Z/library/artifacts/checkpoints"
    )
    if not root.exists():
        pytest.skip("Optional immutable local replay")
    raw = json.loads(
        (
            root
            / "4f1e709c09efc125ab65cf328e73a060e863b8bfc06939b3850905af2ae5489a.json"
        ).read_text(encoding="utf-8")
    )
    parse = json.loads(
        (
            root
            / "f0cafe6c1f5a2a17f063e5def6180e7f9f074ca001c056cf8fa4b5dc2ec1d5a5.json"
        ).read_text(encoding="utf-8")
    )
    units = [
        u
        for u in units_from_blocks(parse["artifact"])
        if u.kind.upper() not in {"HEADER", "FOOTER", "PAGE_NUMBER"}
    ]
    prepared = prepare(Plan.model_validate(raw), units)
    assert problems(prepared, units) == [
        {"range_start": "u00000", "code": "UNCOVERED_SOURCE_RANGE"}
    ]
    assert len(prepared.sections) == 13


def test_unknown_support_and_unresolved_gaps_still_block_final_acceptance():
    units = units_from_blocks(
        {
            "blocks": [
                {"id": "b", "page": 1, "kind": "TEXT", "text": "An actual definition"}
            ]
        }
    )
    value = Plan(
        sections=[
            {"start_unit": "u00000", "title": "Method", "teaching_units": ["UNKNOWN"]}
        ]
    )
    with pytest.raises(QualityFailure, match="source integrity|essential planning"):
        bind(value, units)


def test_route_has_one_pro_plan_and_no_duplicate_model_review():
    operations = []

    def run(operation, instruction, payload, contract, **kwargs):
        operations.append(operation)
        if operation != "next.entity_plan":
            raise AssertionError("Redundant identity review")
        return decision(
            {
                "kind": "RELATED_TO",
                "source_ref": "N0",
                "target_ref": "UNDECLARED",
                "supports": [{"atomic_ref": "A0", "endpoint": "SOURCE"}],
                "reason": "Optional unresolved mention",
            }
        )

    plan, checked, _ = decide(
        SimpleNamespace(
            policy="structured-check-v2", review_policy="essential-v1", run=run
        ),
        {"atomics": [{"ref": f"A{i}"} for i in range(3)]},
        cards(),
        [],
    )
    assert operations == ["next.entity_plan"] and checked.passed
    assert checked.review_mode == "SINGLE_PRO_PLAN_AND_PROGRAM_VALIDATION"
    assert plan._unresolved_relations and all(
        r.entity_key for r in plan.routes.values()
    )


def test_organization_keeps_cycle_and_evidence_checks_without_second_llm():
    operations = []

    def run(operation, instruction, payload, contract, check=None, **kwargs):
        operations.append(operation)
        value = contract(
            links=[
                {
                    "parent_note_id": "p",
                    "child_note_id": "c",
                    "support_revision_ids": ["cr"],
                    "reason": "Specific method",
                }
            ]
        )
        check(value)
        return value

    plan, checked = organization_review(
        None,
        SimpleNamespace(review_policy="essential-v1", run=run),
        {},
        {"p": {}, "c": {}},
        {"p": {"pr"}, "c": {"cr"}},
        ["p", "c"],
        set(),
    )
    assert (
        operations == ["next.organization_plan"]
        and checked.review_mode == "SINGLE_MODEL_PROPOSAL_AND_PROGRAM_VALIDATION"
    )
    assert [r.note_id for r in plan.independent_roots] == ["p"]


def test_ordinary_concept_repair_skips_second_semantic_review():
    class EssentialCalls(PatchCalls):
        review_policy = "essential-v1"

        def __init__(self):
            super().__init__()
            self.reviews = 0

        def run(self, operation, *args, **kwargs):
            if operation == "next.entity_review":
                self.reviews += 1
            return super().run(operation, *args, **kwargs)

    calls = EssentialCalls()
    result = write_group(None, calls, specs())
    assert calls.reviews == 1 and calls.writes == [["a", "b"], ["b"]]
    assert result["b"][1]["review"]["post_repair_semantic_review"] is False


def test_single_concept_repair_keeps_hard_python_validation_without_second_llm_review():
    class Calls(PatchCalls):
        review_policy = "essential-v2"

        def __init__(self):
            super().__init__()
            self.reviews = 0

        def run(self, operation, instruction, content, contract, check=None, **kwargs):
            chapters = content["chapters"]
            if operation == "next.entity_review":
                self.reviews += 1
                value = contract.model_validate(
                    {
                        "chapters": {
                            s["key"]: {
                                "supported_fragment_ids": [s["key"]],
                                "checked_image_ids": [],
                                "findings": [],
                            }
                            for s in chapters
                        }
                    }
                )
            else:
                self.writes.append([s["key"] for s in chapters])
                body = (
                    "```python\nx = (\n```"
                    if len(self.writes) == 1
                    else "```python\nx = 1\n```"
                )
                value = contract.model_validate(
                    {"chapters": {s["key"]: {"markdown": body} for s in chapters}}
                )
            if check:
                check(value)
            return value

    calls = Calls()
    result = write_group(None, calls, specs())
    assert calls.reviews == 1 and calls.writes == [["a", "b"], ["a", "b"]]
    assert all(
        value[1]["review"]["post_repair_semantic_review"] is False
        for value in result.values()
    )


def test_concept_false_positive_is_recorded_without_changing_candidate(monkeypatch):
    class Calls(PatchCalls):
        review_policy = "essential-v2"

    seen = []

    def adjudicate(store, calls, title, markdown, fragments, issues):
        seen.extend(issues)
        return (
            [],
            [{"issue": issues[0], "reason": "Source and current text agree"}],
            "candidate-hash",
        )

    monkeypatch.setattr("noodle_agent.next.guard_review.adjudicate_chapter", adjudicate)
    calls = Calls()
    result = write_group(None, calls, specs())
    assert seen == ["Incorrect condition"] and calls.writes == [["a", "b"]]
    assert result["b"][0] == "Old explanation"
    assert result["b"][1]["guard_markdown_sha256"] == "candidate-hash"
    assert result["b"][1]["guard_adjudications"][0]["issue"] == "Incorrect condition"


@pytest.mark.parametrize("category", ["CANDIDATE_ERROR", "SOURCE_ISSUE"])
def test_inaccurate_review_locator_reaches_adjudication_not_source_or_writer(
    monkeypatch, category
):
    class Calls(PatchCalls):
        review_policy = "essential-v2"

        def run(self, operation, instruction, content, contract, check=None, **kwargs):
            if operation == "next.entity_review":
                value = contract.model_validate(
                    {
                        "chapters": {
                            s["key"]: {
                                "supported_fragment_ids": [s["key"]],
                                "checked_image_ids": [],
                                "findings": [
                                    {
                                        "category": category,
                                        "issue": "Alleged error from stale quote",
                                        "candidate_quote": "Not in current candidate",
                                        "source_ids": [
                                            "raw-unit-in-a-different-namespace"
                                        ],
                                        "source_quote": "Not in actual source",
                                    }
                                ],
                            }
                            for s in content["chapters"]
                        }
                    }
                )
                check(value)
                return value
            return super().run(
                operation, instruction, content, contract, check, **kwargs
            )

    seen = []

    def adjudicate(store, calls, title, markdown, fragments, issues):
        assert (
            markdown == "Old explanation" and fragments[0]["text"] == "Source condition"
        )
        seen.extend(issues)
        return (
            [],
            [
                {
                    "issue": issues[0],
                    "reason": "Actual candidate does not contain the alleged error",
                }
            ],
            "hash",
        )

    monkeypatch.setattr("noodle_agent.next.guard_review.adjudicate_chapter", adjudicate)
    monkeypatch.setattr(
        "noodle_agent.next.quality_repair.request_upstream_repair",
        lambda *a: pytest.fail("A refuted stale finding must not repair Atomic"),
    )
    calls = Calls()
    result = write_group(None, calls, specs())
    assert len(seen) == 2 and calls.writes == [["a", "b"]]
    assert result["b"][1]["review_protocol_issues"]


@pytest.mark.parametrize("scientific", [False, True])
def test_atomic_recheck_only_for_scientific_repairs(scientific):
    source = "$L=1-z$" if scientific else "The method follows a defined procedure."
    record = {
        "key": "p",
        "draft": {
            "title": "Method",
            "summary": "Procedure",
            "sections": [
                {"key": "s", "title": "Procedure", "markdown": "Old explanation"}
            ],
            "assignments": {"u": "s"},
            "omissions": {},
        },
        "inputs": {"units": [{"unit_id": "u", "text": source}]},
        "contexts": [],
        "evidence": {"u": "e"},
    }
    reviews = []

    def call(stage, operation, instruction, payload, contract, validator, **kwargs):
        reviews.append(operation)
        value = contract.model_validate(
            {
                "checked_plans": ["p"],
                "checked_omissions": [],
                "findings": [
                    {
                        "plan_key": "p",
                        "section_key": "s",
                        "category": "CANDIDATE_ERROR",
                        "issue": "Wrong explanation",
                        "candidate_quote": "Old explanation",
                        "source_ids": ["u"],
                    }
                ]
                if len(reviews) == 1
                else [],
            }
        )
        validator(value)
        return value

    g = SimpleNamespace(
        store=None,
        parsing=SimpleNamespace(vision=None),
        profile={"checking_policy": "single-check-v1", "review_policy": "essential-v1"},
        call=call,
    )
    pipeline = SourceSectionPipeline(g)
    pipeline.repair_records = lambda values, issues, **kwargs: values[0]["draft"][
        "sections"
    ][0].update(markdown="Correct explanation")
    _, final, _ = pipeline.typed_review([record], "batch")
    assert final.passed and len(reviews) == (2 if scientific else 1)


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("from_computation", [False, True])
def test_disputed_scientific_findings_are_adjudicated_before_mutating_correct_text(
    confirmed, from_computation
):
    record = {
        "key": "p",
        "draft": {
            "title": "Method",
            "summary": "Procedure",
            "sections": [{"key": "s", "title": "Formula", "markdown": "$L=1-z$"}],
            "assignments": {"u": "s"},
            "omissions": {},
        },
        "inputs": {
            "units": [{"unit_id": "u", "page": 1, "text": "$L=1-z$"}],
            "image_paths": [],
        },
        "contexts": [],
        "evidence": {"u": "e"},
    }
    operations, repairs = [], []

    def call(stage, operation, instruction, payload, contract, validator, **kwargs):
        operations.append(operation)
        if operation == "next.local_finding_review":
            assert payload["related_source_units"]["p"][0]["text"] == "$L=1-z$"
            value = {
                "findings": {
                    "F0": {
                        "status": "CONFIRMED" if confirmed else "FALSE_POSITIVE",
                        "reason": "Compared actual formula and candidate",
                    }
                }
            }
        else:
            value = {
                "checked_plans": ["p"],
                "checked_omissions": [],
                "findings": [
                    {
                        "plan_key": "p",
                        "section_key": "s",
                        "category": "CANDIDATE_ERROR",
                        "issue": "Change formula",
                        "candidate_quote": "$L=1-z$",
                        "source_ids": ["u"],
                    }
                ]
                if len(operations) == 1
                else [],
            }
            if from_computation and len(operations) == 1:
                value["findings"] = []
                value["data_checks"] = [
                    {
                        "plan_key": "p",
                        "section_key": "s",
                        "recomputation": "Recomputed actual data differs",
                        "candidate_quote": "$L=1-z$",
                        "source_ids": ["u"],
                        "passed": False,
                    }
                ]
        result = contract.model_validate(value)
        validator(result)
        return result

    pipeline = SourceSectionPipeline(
        SimpleNamespace(
            store=None,
            parsing=SimpleNamespace(vision=None),
            profile={
                "checking_policy": "single-check-v1",
                "review_policy": "essential-v2",
            },
            call=call,
        )
    )

    def repair(records, issues, **kwargs):
        assert confirmed, "False positive must never reach the writer"
        repairs.append(issues)
        records[0]["draft"]["sections"][0]["markdown"] = "$L=2-z$"

    pipeline.repair_records = repair
    _, final, _ = pipeline.typed_review([record], "batch")
    assert final.passed and len(repairs) == int(confirmed)
    assert operations == ["next.assembly_review", "next.local_finding_review"]
    if confirmed:
        assert record["post_repair_semantic_review"] is False
    assert len(pipeline.guard_waivers) == int(not confirmed)
    if not confirmed:
        from noodle_agent.next.contracts import canonical, sha

        assert pipeline.guard_waivers[0]["draft_sha256"] == sha(
            canonical(record["draft"])
        )


def test_noop_patch_is_rejected_inside_the_bounded_provider_acceptance():
    from noodle_agent.next.section_contracts import Finding, SourceDraft

    record = {
        "key": "p",
        "draft": SourceDraft(
            title="Method",
            sections=[{"key": "s", "title": "Loss", "markdown": "Old explanation"}],
            assignments={"u": "s"},
        ).model_dump(),
        "inputs": {
            "units": [
                {"unit_id": "u", "page": 1, "text": "A squared residual is minimized"}
            ],
            "image_paths": [],
        },
        "contexts": [],
        "evidence": {"u": "e"},
    }
    attempts = []

    def call(stage, operation, instruction, payload, contract, validator, **kwargs):
        assert operation == "next.section_repair"
        for markdown in (
            "Old explanation",
            "Minimize squared residual, not the signed residual",
        ):
            value = contract(sections=[{"key": "s", "markdown": markdown}])
            try:
                validator(value)
            except QualityFailure as exc:
                assert "REPAIR_NO_PROGRESS" in str(exc)
                attempts.append("rejected-before-acceptance")
                continue
            attempts.append("accepted")
            return value
        pytest.fail("No valid bounded response")

    pipeline = SourceSectionPipeline(
        SimpleNamespace(
            store=None,
            parsing=SimpleNamespace(vision=None),
            profile={"checking_policy": "single-check-v1"},
            call=call,
            journal=SimpleNamespace(compute=lambda stage, inputs, fn: fn()),
        )
    )
    pipeline.repair_records(
        [record],
        [
            Finding(
                plan_key="p",
                section_key="s",
                issue="Clarify signed versus squared residual",
                source_units=["u"],
            )
        ],
        use_pro_model=True,
    )
    assert attempts == ["rejected-before-acceptance", "accepted"]
    assert "squared residual" in record["draft"]["sections"][0]["markdown"]
