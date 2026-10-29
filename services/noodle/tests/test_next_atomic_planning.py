"""Learning topics stay comprehensive without merging unrelated source material."""

from types import SimpleNamespace

import fitz

from noodle_agent.next.atomic_planning import coalesce_topics, granularity_report, structure_windows
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.section_contracts import Boundaries
from noodle_agent.next.source_sections import make_plan, units_from_blocks
from noodle_agent.next.store import Store


def unit(index, text="Source explanation", kind="TEXT", page=None):
    return Unit(unit_id=f"u{index}", block_id=f"b{index}", page=page or index + 1,
                start=0, end=len(text), text=text, kind=kind)


def test_compact_windows_preserve_page_and_unit_order():
    values = [unit(i, "Definition and code " * 20) for i in range(90)]
    windows = structure_windows(values, maximum=1000)
    assert [u.unit_id for items, cards in windows for u in items] == [u.unit_id for u in values]
    assert all([u.page for u in items] == sorted(u.page for u in items) for items, _ in windows)
    assert all(len(cards) <= 64 for _, cards in windows)


def test_same_topic_definition_training_example_and_code_coalesce():
    values = [unit(i) for i in range(6)]
    result = coalesce_topics(Boundaries(sections=[
        {"start_unit": "u0", "title": "A complete method", "topic_key": "method", "reason": "New problem"},
        {"start_unit": "u1", "title": "Training", "topic_key": "method", "reason": "Role"},
        {"start_unit": "u2", "title": "Worked example", "topic_key": "method", "reason": "Role"},
        {"start_unit": "u3", "title": "Code", "topic_key": "method", "reason": "Role"},
        {"start_unit": "u4", "title": "Independent method", "topic_key": "other", "reason": "Independent question"},
    ]), values)
    assert [b.start_unit for b in result.sections] == ["u0", "u4"]


def test_no_semantic_merge_without_explicit_shared_topic_or_over_budget():
    values = [unit(i, "different tokens " * 2500) for i in range(2)]
    boundaries = Boundaries(sections=[
        {"start_unit": "u0", "title": "Topic", "topic_key": "same"},
        {"start_unit": "u1", "title": "Topic continued", "topic_key": "same"},
    ])
    assert len(coalesce_topics(boundaries, values).sections) == 2
    plain = boundaries.model_copy(update={"sections": [b.model_copy(update={"topic_key": ""}) for b in boundaries.sections]})
    assert len(coalesce_topics(plain, [unit(0), unit(1)]).sections) == 2


def test_generic_slide_bookmarks_produce_comprehensive_source_ranges(tmp_path):
    store = Store(tmp_path / "topics")
    try:
        with fitz.open() as pdf:
            for _ in range(12):
                pdf.new_page()
            pdf.set_toc([[1, f"Slide Number {p}", p] for p in range(1, 13)])
            store.add_source(pdf.tobytes(), "Fixture slides")
        dispatch(store)
        lease = store.jobs.claim("test", kind="GENERATE_ATOMICS")
        artifact = {"page_count": 12, "blocks": [
            {"id": f"b{i}", "page": i + 1, "text": "Definition, procedure, example, code", "kind": "TEXT"}
            for i in range(12)
        ]}
        requests = []

        def call(stage, operation, instruction, payload, contract, validator):
            requests.append(payload)
            value = contract.model_validate({"sections": [
                {"start_unit": f"u{i:05d}", "title": f"Complete topic {i//4}", "topic_key": f"T{i//4}", "reason": "Independent source question"}
                for i in range(0, 12, 4)
            ]})
            validator(value)
            return value

        values = units_from_blocks(artifact)
        plan = make_plan(SimpleNamespace(store=store, lease=lease, call=call), {"parse_id": "fixture", "artifact": artifact}, values)
        assert len(requests) == 1 and requests[0]["granularity"]["count_target"] is None
        assert len(plan["plans"]) == 3
        assert [p["pages"] for p in plan["plans"]] == [list(range(1, 5)), list(range(5, 9)), list(range(9, 13))]
        assert [u for p in plan["plans"] for u in p["unit_ids"]] == [u.unit_id for u in values]
        report = granularity_report(plan["plans"], values)
        assert report["single_page_count"] == 0 and report["median_pages"] == 4
        assert all(p["reason"] for p in report["ranges"])
    finally:
        store.close()
