"""Book planning tests use only injected providers and temporary stores."""

import fitz
import pytest

from noodle_agent.next.document_outline import build_outline, check_book_coverage, placement
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation import Generation
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.source_structure import source_structure
from noodle_agent.next.store import Store
from noodle_agent.next.worker import GenerationWorker


def pdf_bytes(toc, pages=6, text=None):
    with fitz.open() as pdf:
        for i in range(pages):
            page = pdf.new_page()
            if text and i in text:
                page.insert_text((20, 30), text[i])
        pdf.set_toc(toc)
        return pdf.tobytes()


def test_outline_preserves_part_chapter_section_and_backmatter():
    content = pdf_bytes(
        [
            [1, "I Foundations", 1],
            [2, "1 Introduction", 2],
            [3, "1.1 Definition", 2],
            [3, "1.2 Example", 3],
            [2, "A Notation", 4],
            [2, "Index", 5],
            [2, "Bibliography", 5],
        ],
        text={4: "Index", 5: "Bibliography"},
    )
    outline = build_outline(content)
    by = {n.title: n for n in outline.nodes}
    assert by["I Foundations"].kind == "PART"
    assert by["1 Introduction"].parent_id == by["I Foundations"].node_id
    assert by["1.1 Definition"].parent_id == by["1 Introduction"].node_id
    assert by["Bibliography"].start == 6
    assert by["A Notation"].end == 4
    assert any(i["code"] == "CORRECTED_BOOKMARK_PAGE" for i in outline.issues)
    assert placement(outline, [2, 3])[0] == by["1 Introduction"].node_id
    assert [
        p for segment in outline.segments for p in range(segment["start"], segment["end"] + 1)
    ] == list(range(1, 7))


def test_same_page_subsections_use_common_parent_not_false_precision():
    outline = build_outline(pdf_bytes([[1, "1 Topic", 1], [2, "1.1 A", 1], [2, "1.2 B", 1]]))
    assert placement(outline, [1])[0] == outline.nodes[0].node_id


def test_no_long_outline_requires_review_and_never_silently_guesses():
    outline = build_outline(pdf_bytes([], 181))
    assert any(
        i["code"] == "LONG_DOCUMENT_NEEDS_OUTLINE" and i["severity"] == "ERROR"
        for i in outline.issues
    )


def test_book_unit_coverage_rejects_missing_or_duplicate_units():
    from noodle_agent.next.generation_contracts import Unit

    unit = Unit(unit_id="u", block_id="b", page=1, start=0, end=1, text="x", kind="TEXT")
    plan = {"topics": [{"units": ["u"]}], "coverage": {"exclusions": []}}
    check_book_coverage([unit], [plan])
    with pytest.raises(QualityFailure):
        check_book_coverage([unit], [plan, plan])
    with pytest.raises(QualityFailure):
        check_book_coverage([unit], [])


def book_fixture(tmp_path):
    from test_next_generation import Agent
    from test_next_large_documents import Parser

    from noodle_agent.domain import ParsedBlock, ParsedDocument

    class PointParser(Parser):
        def download_and_normalize(self, identity, payload, destination, title):
            return ParsedDocument(
                title=title,
                page_count=self.parts[identity],
                markdown="",
                parser_name=self.name,
                parser_version="test-v1",
                blocks=[
                    ParsedBlock(
                        block_id="point",
                        page_index=0,
                        page_label="1",
                        text="梯度下降沿负梯度更新。",
                    )
                ],
            )

    store = Store(tmp_path)
    source = store.add_source(
        pdf_bytes(
            [[1, "1 First", 1], [1, "2 Second", 181]],
            181,
            text={0: "Gradient descent", 180: "Gradient descent"},
        ),
        "Textbook",
    )
    parser = PointParser()
    parser.ready = True
    return store, source, parser, Agent()


def test_rolling_plan_writes_first_chapter_before_planning_second(tmp_path):
    store, source, parser, agent = book_fixture(tmp_path)
    try:
        result = GenerationWorker(store, parser=parser, agent=agent).run_once()
        assert result["status"] == "SUCCEEDED"
        first = agent.calls.index("next.write")
        assert agent.calls[:first].count("next.plan") == 1
        assert agent.calls[first:].count("next.plan") == 1
        view = source_structure(store, source["revision_id"])
        assert view["planning_status"] == "COMPLETE" and view["counts"] == {
            "published": 2,
            "planned": 2,
        }
        assert all(
            n["status"] == "COMPLETE" for n in view["outline"]["nodes"] if n["kind"] == "CHAPTER"
        )
        assert all(n["note_id"] for n in view["planned_notes"])
    finally:
        store.close()


def test_late_planning_failure_preserves_earlier_publication(tmp_path):
    store, _source, parser, agent = book_fixture(tmp_path)
    actual = agent.structured_json

    def fail(**kwargs):
        if (
            kwargs["operation"] == "next.plan"
            and kwargs["user"]["profile"]["chapter"]["title"] == "2 Second"
        ):
            raise QualityFailure("second chapter plan rejected")
        return actual(**kwargs)

    agent.structured_json = fail
    try:
        result = GenerationWorker(store, parser=parser, agent=agent).run_once()
        assert result["status"] == "REVIEW"
        assert agent.calls.count("next.write") == 1
        with store.db.engine.connect() as c:
            assert c.exec_driver_sql("select count(*) from note_revisions").scalar_one() >= 2
    finally:
        store.close()


def test_explicit_plan_only_produces_frozen_plan_without_note_or_moc(tmp_path):
    store, _source, parser, agent = book_fixture(tmp_path)
    try:
        dispatch(store)
        lease = store.jobs.claim("planning-test", kind="GENERATE_ATOMICS")
        generation = Generation(store, parser, agent, lease)
        generation.plan_only = True
        result = generation.run()
        assert result["status"] == "PLANNED_NOT_GENERATED"
        assert len(result["book_plan"]["notes"]) == 2 and result["notes"] == []
        assert "next.write" not in agent.calls
        with store.db.engine.connect() as c:
            assert c.exec_driver_sql("select count(*) from note_revisions").scalar_one() == 0
        before = list(agent.calls)
        assert generation.run()["book_plan"] == result["book_plan"]
        assert agent.calls == before
    finally:
        store.close()


def test_write_retry_reuses_whole_book_plan_and_preserves_atomic_ids(tmp_path):
    store, _source, parser, agent = book_fixture(tmp_path)
    state = {"failed": False}

    def crash():
        if not state["failed"]:
            state["failed"] = True
            raise ConnectionError("simulated transient write failure")

    agent.during_write = crash
    worker = GenerationWorker(store, parser=parser, agent=agent)
    try:
        assert worker.run_once()["status"] == "RETRY"
        plans = agent.calls.count("next.plan")
        assert worker.run_once()["status"] == "SUCCEEDED"
        assert agent.calls.count("next.plan") == plans + 1
        with store.db.engine.connect() as c:
            assert (
                c.exec_driver_sql("select count(*) from notes where kind='ATOMIC'").scalar_one()
                == 2
            )
    finally:
        store.close()


def test_outline_read_is_local_and_rejects_deleted_source(tmp_path):
    store, source, _parser, agent = book_fixture(tmp_path)
    try:
        before = list(agent.calls)
        view = source_structure(store, source["revision_id"])
        assert view["planning_status"] == "NOT_PLANNED"
        assert agent.calls == before
        store.sources.set_source_deleted(
            source["source_id"], True, expected_revision=source["revision_id"]
        )
        from noodle_agent.next.repository import Conflict

        with pytest.raises(Conflict):
            source_structure(store, source["revision_id"])
    finally:
        store.close()


def test_source_unit_heading_order_resolves_same_page_sections():
    from noodle_agent.next.document_outline import placement_for_units, unit_node_map
    from noodle_agent.next.generation_contracts import Unit

    outline = build_outline(pdf_bytes([[1, "1 Topic", 1], [2, "1.1 A", 1], [2, "1.2 B", 1]]))
    values = [
        ("a", "HEADING", "1.1 A"),
        ("b", "TEXT", "first explanation"),
        ("c", "HEADING", "1.2 B"),
        ("d", "TEXT", "second explanation"),
    ]
    units = [
        Unit(unit_id=k, block_id=k, page=1, start=0, end=len(t), text=t, kind=kind)
        for k, kind, t in values
    ]
    owners = unit_node_map(outline, units)
    titles = {n.title: n.node_id for n in outline.nodes}
    assert placement_for_units(outline, ["a", "b"], owners, [1])[0] == titles["1.1 A"]
    assert placement_for_units(outline, ["b", "d"], owners, [1])[0] == titles["1 Topic"]


def test_note_quality_failure_does_not_block_other_planned_chapters(tmp_path):
    store, source, parser, agent = book_fixture(tmp_path)
    actual = agent.structured_json

    def fail_first(**kwargs):
        if (
            kwargs["operation"] == "next.write"
            and kwargs["user"]["profile"]["chapter"]["title"] == "1 First"
        ):
            raise QualityFailure("first note fails content gate")
        return actual(**kwargs)

    agent.structured_json = fail_first
    try:
        result = GenerationWorker(store, parser=parser, agent=agent).run_once()
        assert result["status"] == "REVIEW"
        view = source_structure(store, source["revision_id"])
        assert view["counts"] == {"published": 1, "planned": 2}
        assert view["planning_status"] == "COMPLETE"
        assert sum(n["status"] == "PLANNED" for n in view["planned_notes"]) == 1
        with store.db.engine.connect() as c:
            assert (
                c.exec_driver_sql(
                    "select count(*) from note_revisions r join notes n on n.id=r.note_id where n.kind='ATOMIC'"
                ).scalar_one()
                == 1
            )
    finally:
        store.close()


def test_legacy_frozen_chapter_is_reused_and_remaining_ranges_use_outline(tmp_path):
    import copy
    from types import SimpleNamespace

    from noodle_agent.next.chapter_generation import ScopedJournal
    from noodle_agent.next.planning import source_units

    store, _source, parser, agent = book_fixture(tmp_path)
    try:
        dispatch(store)
        lease = store.jobs.claim("legacy-plan", kind="GENERATE_ATOMICS")
        root = Generation(store, parser, agent, lease)
        parsed = root.parsing.run(lease.input["revision_id"])
        units = source_units(parsed["artifact"])
        chapter = parsed["artifact"]["chapters"][0]
        child = copy.copy(root)
        child.chapter_units = [u for u in units if u.page <= chapter["end"]]
        child.chapter_key = "chapter0"
        child.plan_only = True
        child.profile = {**root.profile, "chapter": chapter}
        child.journal = ScopedJournal(root.journal, "chapter.0.")
        child.parsing = SimpleNamespace(
            vision=None,
            run=lambda revision: {
                "parse_id": parsed["parse_id"],
                "artifact": {**parsed["artifact"], "chapters": [chapter]},
            },
        )
        child.run()
        prior_calls = agent.calls.count("next.plan")
        root.plan_only = True
        result = root.run()["book_plan"]
        assert result["layout_mode"] == "MIXED_FROZEN_AND_OUTLINE"
        assert result["chapters"][0]["namespace"] == "chapter.0."
        assert result["chapters"][1]["namespace"].startswith("outline.")
        assert agent.calls.count("next.plan") == prior_calls + 1
        assert "next.write" not in agent.calls
    finally:
        store.close()
