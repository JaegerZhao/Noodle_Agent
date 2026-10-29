"""Pause/resume, immutable reuse, compact context and metrics regression gates."""

import json

import pytest
from test_next_book_planning import book_fixture

from noodle_agent.next.generation_contracts import allocation_contract, writing_contract
from noodle_agent.next.metrics import project_call, timeline
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.task_control import control, request_pause, resume
from noodle_agent.next.worker import GenerationWorker


def test_pause_drains_current_chapter_then_resume_skips_published(tmp_path):
    store, _source, parser, agent = book_fixture(tmp_path)
    try:

        def pause():
            with store.db.engine.connect() as c:
                job = c.exec_driver_sql(
                    "SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS'"
                ).scalar_one()
            request_pause(store, job)

        agent.during_write = pause
        worker = GenerationWorker(store, parser=parser, agent=agent)
        assert worker.run_once()["status"] == "PAUSED"
        with store.db.engine.connect() as c:
            job = c.exec_driver_sql(
                "SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS'"
            ).scalar_one()
            heads = list(
                c.exec_driver_sql("SELECT id,current_revision_id FROM notes WHERE kind='ATOMIC'")
            )
        assert len(heads) == 1 and agent.calls.count("next.plan") == 1
        assert control(store, job)["state"] == "PAUSED"
        assert worker.run_once() is None
        request_pause(store, job)
        agent.during_write = None
        resume(store, job)
        resume(store, job)
        assert worker.run_once()["status"] == "SUCCEEDED"
        assert agent.calls.count("next.write") == 2
        with store.db.engine.connect() as c:
            assert (
                c.exec_driver_sql(
                    "SELECT current_revision_id FROM notes WHERE id=?", (heads[0][0],)
                ).scalar_one()
                == heads[0][1]
            )
    finally:
        store.close()


def test_paused_current_chapter_failure_does_not_start_next(tmp_path):
    store, _source, parser, agent = book_fixture(tmp_path)
    real = agent.structured_json

    def failed(**kwargs):
        if kwargs["operation"] == "next.write":
            with store.db.engine.connect() as c:
                job = c.exec_driver_sql(
                    "SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS'"
                ).scalar_one()
            request_pause(store, job)
            raise QualityFailure("checked content failure")
        return real(**kwargs)

    agent.structured_json = failed
    try:
        assert GenerationWorker(store, parser=parser, agent=agent).run_once()["status"] == "PAUSED"
        assert agent.calls.count("next.plan") == 1
    finally:
        store.close()


def test_compact_schema_size_does_not_grow_per_unit():
    from noodle_agent.next.generation_contracts import Unit

    units = [
        Unit(unit_id="unit" + str(i), block_id="b", page=1, start=0, end=3, text="abc", kind="TEXT")
        for i in range(32)
    ]
    old = len(json.dumps(allocation_contract(units).model_json_schema()))
    compact = len(json.dumps(allocation_contract(units, compact=True).model_json_schema()))
    assert compact < old
    assert "RequiredUnitAssignments" not in allocation_contract(
        units, compact=True
    ).model_json_schema().get("$defs", {})
    assert len(json.dumps(writing_contract(units, compact=True).model_json_schema())) < len(
        json.dumps(writing_contract(units).model_json_schema())
    )


def test_unknown_usage_is_not_zero_and_call_aggregation_is_unique(tmp_path):
    store, _source, _parser, _agent = book_fixture(tmp_path)
    try:
        from noodle_agent.next.events import dispatch

        dispatch(store)
        with store.db.engine.connect() as c:
            job = c.exec_driver_sql(
                "SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS'"
            ).scalar_one()
        with store.repository.transaction() as c:
            project_call(
                c,
                job,
                "LLM_CALL",
                {
                    "operation": "next.write",
                    "usage": {"total_tokens": 12, "prompt_tokens": 10, "completion_tokens": 2},
                },
                "artifacts/test-one.json",
            )
            project_call(c, job, "LLM_CALL", {"operation": "next.write"}, "artifacts/test-two.json")
        result = timeline(store, job)
        assert result["summary"]["total_tokens"] == 12
        assert result["summary"]["missing_usage"] == 1
        assert result["summary"]["calls"] == 2
    finally:
        store.close()


def test_python_comments_and_html_strings_in_fenced_code_are_not_headings():
    from noodle_agent.next.contracts import Scope
    from noodle_agent.next.generation import Generation
    from noodle_agent.next.generation_contracts import Draft, DraftSection

    def check(body):
        value = Draft(
            title="LDA",
            summary="Training",
            sections=[
                DraftSection(
                    key="train",
                    title="Training",
                    question="How?",
                    scope=Scope(includes=["LDA"]),
                    markdown=body,
                    units=["u1"],
                )
            ],
        )
        Generation.validate_draft(value, {"u1"}, set())

    check(
        '使用代码训练模型：\n\n```python\n# number of topics\nnum_topics = 7\nlabel = "<topic>"\n```\n'
    )
    with pytest.raises(QualityFailure):
        check("正常正文\n\n# 不允许覆盖文档标题")
    with pytest.raises(QualityFailure):
        check("```python\n# unfinished")
    with pytest.raises(QualityFailure):
        check("<script>bad</script>")


def test_python_parameters_are_not_mistaken_for_math_variables():
    from noodle_agent.next.contracts import Scope
    from noodle_agent.next.generation import Generation
    from noodle_agent.next.generation_contracts import Draft, DraftSection

    def check(body):
        Generation.validate_draft(
            Draft(
                title="Code",
                summary="Parameters",
                sections=[
                    DraftSection(
                        key="api",
                        title="API",
                        question="How?",
                        scope=Scope(includes=["code"]),
                        markdown=body,
                        units=["u"],
                    )
                ],
            ),
            {"u"},
            set(),
        )

    check("参数 `n_components=50` 与 `n_iter=100`，预处理器 `p_stemmer`。")
    with pytest.raises(QualityFailure):
        check("错误的数学变量 `x_i`。")


def test_concept_nested_role_normalization_retains_conflict_checks():
    from noodle_agent.next.concepts.contracts import Decision

    proposal = {
        "title": "Backprop",
        "domain": "nn",
        "identity_key": "backprop",
        "question": "How?",
        "scope": {"includes": ["gradients"]},
        "role": "SUPPORT",
    }
    value = Decision.model_validate(
        {"action": "CREATE_CONCEPT", "proposal": proposal, "reason": "independent concept"}
    )
    assert value.role == "SUPPORT"
    with pytest.raises(ValueError):
        Decision.model_validate(
            {
                "action": "CREATE_CONCEPT",
                "proposal": proposal,
                "role": "CONTRAST",
                "reason": "conflicting role",
            }
        )


def test_fragment_manifest_is_frozen_before_first_decision(tmp_path):
    from test_next_maintenance import atomic

    from noodle_agent.next.concepts.fragments import fragments
    from noodle_agent.next.contracts import canonical
    from noodle_agent.next.repository import identifier, insert, now
    from noodle_agent.next.store import Store

    store = Store(tmp_path / "fragment-cache")
    try:
        _, note_id, _ = atomic(
            store,
            "Long explanation",
            ("A complete explanation of gradient computation. " * 200)
            + "\n\n"
            + ("Another paragraph with conditions. " * 200),
        )
        note = store.get_note(note_id)
        first = fragments(store, note)
        with store.repository.transaction() as c:
            insert(
                c,
                "contribution_decisions",
                id=identifier(),
                atomic_revision_id=note["revision"]["id"],
                fragment_id=first[0]["fragment_id"],
                policy="concept-decisions-v1",
                concept_id=None,
                section_id=None,
                action="INSUFFICIENT",
                status="SKIPPED",
                fragment_json=canonical(first[0]),
                decision_json="{}",
                applied_revision_id=None,
                created_at=now(),
            )
        assert fragments(store, note) == first
    finally:
        store.close()
