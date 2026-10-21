"""Workflow tests use explicitly injected providers, never a real-service fallback."""

from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest

from noodle_agent.domain import ParsedBlock, ParsedDocument
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.contracts import Scope
from noodle_agent.next.generation import Generation
from noodle_agent.next.generation_contracts import Draft, MergePlan, Unit, WindowPlan
from noodle_agent.next.planning import (
    QualityFailure,
    source_units,
    validate_merge,
    validate_window,
    windows,
)
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.store import Store
from noodle_agent.next.worker import GenerationWorker

SCOPE = {"includes": ["梯度下降"]}


class Parser:
    name = "injected-test"

    def __init__(self, pending=0, uncertain=False):
        self.submissions = self.polls = self.downloads = 0
        self.pending, self.uncertain = pending, uncertain

    def submit(self, path, data_id):
        self.submissions += 1
        assert path.read_bytes().startswith(b"%PDF-")
        if self.uncertain:
            raise ConnectionError("Remote acceptance is unknown")
        return {"external_id": "test-provider-42", "state": "pending"}

    def poll(self, external_id):
        assert external_id == "test-provider-42"
        self.polls += 1
        return {"state": "running" if self.polls <= self.pending else "done"}

    def download_and_normalize(self, external_id, result, destination, title):
        self.downloads += 1
        return ParsedDocument(
            title=title,
            page_count=2,
            markdown="",
            blocks=[
                ParsedBlock(
                    block_id="a",
                    page_index=0,
                    page_label="1",
                    text="梯度下降沿负梯度方向更新权重，学习率控制步长。",
                ),
                ParsedBlock(
                    block_id="b",
                    page_index=1,
                    page_label="2",
                    text="反向传播不保证收敛到全局最优解。",
                ),
            ],
            parser_name=self.name,
            parser_version="test-v1",
        )


class Agent:
    def __init__(self, fail_judge=False, during_write=None):
        self.calls = []
        self.fail_judge = fail_judge
        self.during_write = during_write

    def structured_json(self, *, operation, user, **kwargs):
        self.calls.append(operation)
        data = user["content"]
        if operation == "next.plan":
            return {
                "topics": [
                    {
                        "key": f"topic-{i}",
                        "title": u["text"],
                        "question": "如何理解？",
                        "scope": SCOPE,
                    }
                    for i, u in enumerate(data["units"])
                ],
                "assignments": {
                    u["unit_id"]: {"mode": "TOPIC", "topic_key": f"topic-{i}"}
                    for i, u in enumerate(data["units"])
                },
            }
        if operation == "next.merge":
            return {
                "summary": "本资料介绍梯度下降及其限制。",
                "groups": [
                    {
                        "key": f"note-{i}",
                        "title": t["title"],
                        "question": t["question"],
                        "scope": SCOPE,
                        "topic_keys": [t["key"]],
                    }
                    for i, t in enumerate(data["topics"])
                ],
            }
        if operation in {"next.plan_review", "next.judge"}:
            failed = operation == "next.judge" and self.fail_judge
            if operation == "next.judge":
                self.fail_judge = False
            return {
                "passed": not failed,
                "issues": ["需说明方向"] if failed else [],
                "supported_units": [u["unit_id"] for u in data["units"]],
                "reason": "逐项核对来源。",
            }
        if operation == "next.write":
            if self.during_write:
                self.during_write()
            return {
                "title": data["plan"]["title"],
                "summary": "测试摘要。",
                "sections": [
                    {
                        "key": "explanation",
                        "title": "含义与条件",
                        "question": "如何理解？",
                        "scope": SCOPE,
                        "markdown": "\n\n".join(u["text"] for u in data["units"]),
                    }
                ],
                "assignments": {u["unit_id"]: "explanation" for u in data["units"]},
            }
        raise AssertionError(operation)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "next")
    with fitz.open() as pdf:
        for text in (
            "Gradient descent updates opposite to gradient.",
            "Backpropagation does not guarantee global convergence.",
        ):
            page = pdf.new_page()
            page.insert_text((30, 30), text)
        content = pdf.tobytes()
    store.add_source(content, "测试课件")
    yield store
    store.close()


def worker(store, parser=None, agent=None):
    return GenerationWorker(store, parser=parser or Parser(), agent=agent or Agent())


def query(store, sql):
    with store.db.engine.connect() as c:
        return c.exec_driver_sql(sql).scalar_one()


def test_pending_polling_resumes_same_submission_without_spending_retry_budget(store):
    parser, agent = Parser(pending=4), Agent()
    consumer = worker(store, parser, agent)
    for _ in range(4):
        assert consumer.run_once()["status"] == "PENDING"
    result = consumer.run_once()
    assert result["status"] == "SUCCEEDED" and result["note_count"] == 2
    assert parser.submissions == parser.downloads == 1
    assert query(store, "SELECT attempts FROM jobs WHERE kind='GENERATE_ATOMICS'") == 1
    assert query(store, "SELECT count(*) FROM notes") == 3
    assert query(store, "SELECT count(*) FROM note_revisions") == 3
    assert query(store, "SELECT count(*) FROM evidence_anchors") == 2
    assert consumer.run_once() is None
    assert query(store, "SELECT status FROM events WHERE event_type='SOURCE_ADDED'") == "COMPLETED"


def test_crash_after_publication_recovers_same_head_without_duplicate_note_or_model_call(
    store, monkeypatch
):
    parser, agent = Parser(), Agent()
    consumer = worker(store, parser, agent)
    original = Checkpoints.save
    failed = False

    def interrupt(self, stage, inputs, value):
        nonlocal failed
        if stage == "publish.note-0" and not failed:
            failed = True
            raise ConnectionError("Process stopped after committed publication")
        return original(self, stage, inputs, value)

    monkeypatch.setattr(Checkpoints, "save", interrupt)
    assert consumer.run_once()["status"] == "RETRY"
    assert query(store, "SELECT count(*) FROM note_revisions") == 1
    count = len(agent.calls)
    assert consumer.run_once()["status"] == "SUCCEEDED"
    assert parser.submissions == parser.downloads == 1
    assert query(store, "SELECT count(*) FROM note_revisions") == 3
    assert len(agent.calls) - count == 2  # only the second note's writer and judge


def test_uncertain_submit_is_not_silently_repeated(store):
    parser = Parser(uncertain=True)
    consumer = worker(store, parser)
    assert consumer.run_once()["status"] == "RETRY"
    assert consumer.run_once()["status"] == "REVIEW"
    assert parser.submissions == 1
    assert query(store, "SELECT count(*) FROM note_revisions") == 0
    assert query(store, "SELECT status FROM events WHERE event_type='SOURCE_ADDED'") != "COMPLETED"


def test_content_judge_repairs_instead_of_publishing_rejected_draft(store):
    agent = Agent(fail_judge=True)
    result = worker(store, agent=agent).run_once()
    assert result["status"] == "SUCCEEDED"
    assert agent.calls.count("next.write") == 3
    assert query(store, "SELECT count(*) FROM note_revisions") == 3


def test_source_deleted_during_write_cannot_publish(store):
    with store.db.engine.connect() as c:
        source = one(c, "SELECT id FROM sources")["id"]
    agent = Agent(during_write=lambda: store.set_source_deleted(source, True))
    result = worker(store, agent=agent).run_once()
    assert result["status"] == "REVIEW"
    assert query(store, "SELECT count(*) FROM note_revisions") == 0


def test_note_creation_is_idempotent_under_concurrent_retry(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(
            pool.map(
                lambda _: store.content.create_note(
                    "ATOMIC", "梯度下降", creation_key="source:topic"
                ),
                range(2),
            )
        )
    assert ids[0] == ids[1]
    assert query(store, "SELECT count(*) FROM notes") == 1
    with pytest.raises(Conflict):
        store.content.create_note("CONCEPT", "梯度下降", creation_key="source:topic")


def test_all_long_source_text_is_accounted_for_without_truncation():
    text = "条件与公式" * 2000
    units = source_units(
        {"blocks": [{"id": "long", "page": 1, "kind": "TEXT", "text": text}]}, max_chars=800
    )
    assert "".join(u.text for u in units) == text
    assert sum(len(window) for window in windows(units, budget=3000)) == len(units)
    assert units[-1].end == len(text)


def test_window_and_merge_reject_missing_duplicate_or_unknown_units():
    unit = Unit(unit_id="u0", block_id="a", page=1, start=0, end=1, text="A", kind="TEXT")
    with pytest.raises(QualityFailure, match="every unit"):
        validate_window(WindowPlan(topics=[]), [unit])
    plan = MergePlan.model_validate(
        {
            "summary": "x",
            "groups": [
                {"key": "a", "title": "x", "question": "x", "scope": SCOPE, "topic_keys": ["wrong"]}
            ],
        }
    )
    with pytest.raises(QualityFailure, match="preserve"):
        validate_merge(plan, {"expected"})
    excluded = WindowPlan.model_validate(
        {
            "topics": [],
            "exclusions": [
                {
                    "unit_id": "u0",
                    "category": "DUPLICATE",
                    "reason": "duplicate",
                    "duplicate_of": "u0",
                }
            ],
        }
    )
    with pytest.raises(QualityFailure, match="counterpart"):
        validate_window(excluded, [unit])


@pytest.mark.parametrize(
    "body",
    [
        "## 重复标题",
        "数学 `$x_i$`",
        "查询向量 `q_i`",
        "矩阵元素 `A_{i,j}`",
        "![图](https://evil.example/image.png)",
        "<script>bad</script>",
        "未闭合 $x_i",
    ],
)
def test_draft_gate_rejects_rendering_errors_and_unapproved_assets(body):
    draft = Draft(
        title="t",
        summary="s",
        sections=[
            {
                "key": "s",
                "title": "t",
                "question": "q",
                "scope": Scope(includes=["x"]),
                "markdown": body,
                "units": ["u0"],
            }
        ],
    )
    with pytest.raises(QualityFailure):
        Generation.validate_draft(draft, {"u0"}, set())


def test_checkpoint_is_immutable_and_expired_worker_cannot_advance(store):
    store.jobs.dispatch({"SOURCE_ADDED": ["TEST"]})
    lease = store.jobs.claim("test", kind="TEST")
    journal = Checkpoints(store.repository, lease)
    journal.save("stage", {"input": 1}, {"value": 1})
    with pytest.raises(Conflict, match="input changed"):
        journal.load("stage", {"input": 2})
    with pytest.raises(Conflict, match="differs"):
        journal.save("stage", {"input": 1}, {"value": 2})
    with store.repository.transaction() as c:
        c.exec_driver_sql("UPDATE jobs SET lease_until='2000-01-01'")
    with pytest.raises(Conflict, match="lease"):
        journal.save("late", {}, {})


def test_markdown_only_parse_cannot_forge_page_one_evidence(store):
    class NoPageParser(Parser):
        def download_and_normalize(self, *args):
            doc = super().download_and_normalize(*args)
            return doc.model_copy(
                update={
                    "blocks": [
                        doc.blocks[0].model_copy(
                            update={"metadata": {"page_precision": "UNAVAILABLE"}}
                        )
                    ]
                }
            )

    result = worker(store, parser=NoPageParser()).run_once()
    assert result["status"] == "REVIEW"
    assert query(store, "SELECT count(*) FROM parse_runs") == 0
    assert query(store, "SELECT count(*) FROM note_revisions") == 0


def test_asset_escape_is_rejected_before_visual_service(store, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"private outside file")

    class EscapeParser(Parser):
        def download_and_normalize(self, *args):
            doc = super().download_and_normalize(*args)
            return doc.model_copy(
                update={"blocks": [doc.blocks[0].model_copy(update={"asset_path": str(outside)})]}
            )

    result = worker(store, parser=EscapeParser()).run_once()
    assert result["status"] == "REVIEW"
    assert "outside" in result["detail"]
    assert not list(store.root.glob("assets/*"))


def test_duplicate_parse_and_evidence_writes_are_idempotent(store):
    with store.db.engine.connect() as c:
        revision = one(c, "SELECT current_revision_id FROM sources")["current_revision_id"]
    artifact = {"blocks": [{"id": "a", "page": 1, "text": "原始条件"}]}
    first = store.record_parse(revision, "v1", {}, artifact)
    assert store.record_parse(revision, "v1", {}, artifact) == first
    evidence = store.add_evidence(revision, "原始条件", page=1, block_id="a", parse_id=first)
    assert (
        store.add_evidence(revision, "原始条件", page=1, block_id="a", parse_id=first) == evidence
    )
    assert query(store, "SELECT count(*) FROM parse_runs") == 1
    assert query(store, "SELECT count(*) FROM evidence_anchors") == 1


def test_repeated_semantic_rejection_leaves_event_incomplete_and_no_publication(store):
    class RejectingAgent(Agent):
        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.judge":
                result.update(passed=False, issues=["来源不支持结论"])
            return result

    agent = RejectingAgent()
    result = worker(store, agent=agent).run_once()
    assert result["status"] == "REVIEW"
    assert agent.calls.count("next.write") == 3
    assert query(store, "SELECT count(*) FROM note_revisions") == 0
    assert query(store, "SELECT status FROM events WHERE event_type='SOURCE_ADDED'") != "COMPLETED"


def test_malformed_planning_retries_are_bounded_and_cannot_silently_skip_units(store):
    class IncompleteAgent(Agent):
        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.plan":
                result["topics"] = result["topics"][:1]
            return result

    agent = IncompleteAgent()
    result = worker(store, agent=agent).run_once()
    assert result["status"] == "REVIEW"
    assert agent.calls == ["next.plan"] * 3
    assert query(store, "SELECT count(*) FROM note_revisions") == 0


def test_missing_nonblank_pdf_page_stops_publication(store):
    class TruncatedParser(Parser):
        def download_and_normalize(self, *args):
            doc = super().download_and_normalize(*args)
            return doc.model_copy(update={"blocks": doc.blocks[:1], "page_count": 1})

    result = worker(store, parser=TruncatedParser()).run_once()
    assert result["status"] == "REVIEW"
    assert "omitted nonblank PDF pages" in result["detail"]
    assert query(store, "SELECT count(*) FROM note_revisions") == 0


def test_visual_batch_has_persistent_assets_provenance_and_resumes_from_receipts(store):

    from noodle_agent.domain import VisualUnderstanding
    from noodle_agent.next.contracts import sha

    class ImageParser(Parser):
        def download_and_normalize(self, external_id, result, destination, title):
            doc = super().download_and_normalize(external_id, result, destination, title)
            destination.mkdir(parents=True, exist_ok=True)
            image = destination / "diagram.png"
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
            pixmap.clear_with(255)
            pixmap.save(image)
            return doc.model_copy(
                update={
                    "blocks": [b.model_copy(update={"asset_path": str(image)}) for b in doc.blocks]
                }
            )

    class Vision:
        enabled = True
        calls = 0

        def analyze_batch(self, items):
            self.calls += 1
            return {
                item["external_block_id"]: VisualUnderstanding(
                    external_block_id=item["external_block_id"],
                    image_sha256=sha(item["image_path"].read_bytes()),
                    model="injected-vision",
                    caption="测试流程图",
                    confidence=0.9,
                )
                for item in items
            }

    vision = Vision()
    consumer = GenerationWorker(store, parser=ImageParser(), agent=Agent(), vision=vision)
    assert consumer.run_once()["status"] == "SUCCEEDED"
    assert vision.calls == 1
    assert len(list((store.root / "assets").glob("*.png"))) == 1
    with store.db.engine.connect() as c:
        row = one(c, "SELECT artifact_ref,artifact_sha256 FROM parse_runs")
    import json

    parsed = json.loads(store.read(row["artifact_ref"], row["artifact_sha256"]))
    assert all(b["evidence_origin"] == "DERIVED_VISION" for b in parsed["blocks"])
    assert parsed["page_coverage"]["represented"] == [1, 2]


def test_bundled_katex_accepts_formulas_and_rejects_unknown_commands():
    from noodle_agent.next.math_check import validate_math

    report = validate_math(r"变量 $q_i$，以及 $$A_{i,j}=\frac{q_i^Tk_j}{\sqrt{D}}$$。")
    assert report["count"] == 2 and not report["errors"]
    with pytest.raises(QualityFailure, match="KaTeX rendering errors"):
        validate_math(r"错误公式 $\unknownNoodleCommand{x}$")


def test_generated_content_cannot_execute_javascript_in_math_validation(tmp_path):
    from noodle_agent.next.math_check import validate_math

    target = tmp_path / "must-not-exist"
    text = (
        "Code-like source: '); require('node:fs').writeFileSync("
        + repr(str(target))
        + ", 'bad'); // $x$"
    )
    assert validate_math(text)["count"] == 1
    assert not target.exists()


def test_independent_readback_verifies_completed_job_and_refuses_corrupt_body(store):
    from noodle_agent.next.verification import verify_generation

    result = worker(store).run_once()
    report = verify_generation(store, result["job_id"])
    assert report["status"] == "ENGINEERING_READBACK_PASSED"
    assert report["source_units"] == report["included_units"] == 2
    assert len(report["notes"]) == 3
    with store.db.engine.connect() as c:
        body = one(c, "SELECT markdown_ref FROM note_revisions LIMIT 1")["markdown_ref"]
    store.path(body).write_text("corrupted body", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        verify_generation(store, result["job_id"])


def test_worker_reports_retry_exhaustion_as_review(store):
    class OfflineAgent(Agent):
        def structured_json(self, **kwargs):
            raise ConnectionError("Temporary provider outage")

    consumer = worker(store, agent=OfflineAgent())
    assert [consumer.run_once()["status"] for _ in range(3)] == ["RETRY", "RETRY", "REVIEW"]
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "REVIEW"
    assert query(store, "SELECT attempts FROM jobs WHERE kind='GENERATE_ATOMICS'") == 3


def test_failed_semantic_plan_gets_bounded_feedback_repair_before_any_publication(store):
    class PlanRepairAgent(Agent):
        def __init__(self):
            super().__init__()
            self.reviews = 0

        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.plan_review":
                self.reviews += 1
                if self.reviews == 1:
                    result.update(passed=False, issues=["术语表被错误排除"])
            if kwargs["operation"] == "next.plan" and self.reviews:
                assert "术语表被错误排除" in kwargs["user"]["content"]["repair_issues"]
            return result

    agent = PlanRepairAgent()
    assert worker(store, agent=agent).run_once()["status"] == "SUCCEEDED"
    assert agent.calls.count("next.plan") == 2
    assert query(store, "SELECT count(*) FROM note_revisions") == 3


def test_operator_resume_preserves_receipts_and_audits_the_reason(store):
    class RejectingPlan(Agent):
        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.plan_review":
                result.update(passed=False, issues=["仍需人工复核"])
            return result

    consumer = worker(store, agent=RejectingPlan())
    result = consumer.run_once()
    assert result["status"] == "REVIEW"
    receipts = query(store, "SELECT count(*) FROM job_checkpoints")
    store.jobs.retry_review(result["job_id"], reason="Inspected malformed provider output")
    assert query(store, "SELECT count(*) FROM job_checkpoints") == receipts
    assert query(store, "SELECT count(*) FROM events WHERE event_type='JOB_RETRY_REQUESTED'") == 1
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "PENDING"
    with pytest.raises(Conflict):
        store.jobs.retry_review(result["job_id"], reason="Do not duplicate a running recovery")


def test_bounded_parallel_writing_keeps_distinct_notes_and_map_order(store):
    import threading

    class ConcurrentAgent(Agent):
        barrier = threading.Barrier(2)

        def structured_json(self, **kwargs):
            if kwargs["operation"] == "next.write":
                self.barrier.wait(timeout=5)
            return super().structured_json(**kwargs)

    result = GenerationWorker(
        store, parser=Parser(), agent=ConcurrentAgent(), draft_concurrency=2
    ).run_once()
    assert result["status"] == "SUCCEEDED"
    with store.db.engine.connect() as c:
        import json

        job = one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")
        outputs = json.loads(job["result_json"])
        map_note = store.get_note(outputs["source_map"]["note_id"])
    assert len({n["note_id"] for n in outputs["notes"]}) == 2
    markdown = map_note["markdown"]
    assert markdown.index("梯度下降沿") < markdown.index("反向传播不保证")


def test_malformed_provider_json_gets_bounded_repair_not_a_published_fallback(store):
    import json

    class BrokenJsonAgent(Agent):
        broken = True

        def structured_json(self, **kwargs):
            if self.broken:
                self.broken = False
                raise json.JSONDecodeError("Invalid abbreviated object", "{...}", 1)
            return super().structured_json(**kwargs)

    result = worker(store, agent=BrokenJsonAgent()).run_once()
    assert result["status"] == "SUCCEEDED"
    assert query(store, "SELECT count(*) FROM job_checkpoints WHERE stage LIKE '%.error'") == 1


def test_dynamic_assignment_schema_prevents_missing_or_double_decisions():
    from pydantic import ValidationError

    from noodle_agent.next.generation_contracts import allocation_contract

    unit = Unit(unit_id="u0", block_id="a", page=1, start=0, end=1, text="A", kind="TEXT")
    schema = allocation_contract([unit])
    for value in (
        {"topics": [], "assignments": {}},
        {"topics": [], "assignments": {"u0": {"mode": "DECORATIVE", "reason": "not allowed"}}},
        {
            "topics": [],
            "assignments": {"u0": {"mode": "TOPIC", "topic_key": "x", "reason": "second decision"}},
        },
    ):
        with pytest.raises(ValidationError):
            schema.model_validate(value)


def test_known_asset_storage_path_normalizes_to_relative_markdown_before_review():
    draft = Draft(
        title="t",
        summary="s",
        sections=[
            {
                "key": "s",
                "title": "t",
                "question": "q",
                "scope": SCOPE,
                "markdown": "说明。![图](assets/known.png)",
                "units": ["u0"],
            }
        ],
    )
    checked = Generation.checked_draft(draft, {"u0"}, {"../assets/known.png"})
    assert "(../assets/known.png)" in checked.sections[0].markdown
    assert "(assets/known.png)" in draft.sections[0].markdown
    with pytest.raises(QualityFailure):
        Generation.checked_draft(draft, {"u0"}, {"../assets/another.png"})


def test_math_inequalities_are_not_mistaken_for_html():
    draft = Draft(
        title="t",
        summary="s",
        sections=[
            {
                "key": "s",
                "title": "t",
                "question": "q",
                "scope": SCOPE,
                "markdown": "负值 $x<0$ 与正值 $x>0$ 采用不同分支。",
                "units": ["u0"],
            }
        ],
    )
    Generation.validate_draft(draft, {"u0"}, set())
    broken = draft.model_copy(
        update={
            "sections": [
                draft.sections[0].model_copy(update={"markdown": "公式 $" + chr(12) + "rac{1}{2}$"})
            ]
        }
    )
    with pytest.raises(QualityFailure, match="control character"):
        Generation.validate_draft(broken, {"u0"}, set())


def test_required_writer_assignments_reject_missing_evidence_and_unknown_sections():
    from pydantic import ValidationError

    from noodle_agent.next.generation_contracts import allocated_draft, writing_contract

    unit = Unit(unit_id="u0", block_id="a", page=1, start=0, end=1, text="A", kind="TEXT")
    schema = writing_contract([unit])
    base = {
        "title": "A",
        "summary": "A",
        "sections": [{"key": "s", "title": "A", "question": "A?", "scope": SCOPE, "markdown": "A"}],
    }
    with pytest.raises(ValidationError):
        schema.model_validate({**base, "assignments": {}})
    with pytest.raises(ValueError, match="Unknown section"):
        allocated_draft(schema.model_validate({**base, "assignments": {"u0": "missing"}}))


def test_explicit_retry_returns_previous_semantic_feedback_to_writer(store):
    class NeedsRepair(Agent):
        fixed = False
        saw_feedback = False

        def structured_json(self, **kwargs):
            if kwargs["operation"] == "next.write" and kwargs["user"]["content"]["repair_issues"]:
                self.saw_feedback = True
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.write" and self.fixed:
                result["sections"][0]["markdown"] += "\n\n矩阵之间的关系尚未明确，应保留不确定性。"
            if kwargs["operation"] == "next.judge" and not self.fixed:
                result.update(passed=False, issues=["图中矩阵关系没有证据，请保留不确定性"])
            return result

    agent = NeedsRepair()
    consumer = worker(store, agent=agent)
    outcome = consumer.run_once()
    assert outcome["status"] == "REVIEW"
    agent.fixed = True
    agent.saw_feedback = False
    store.jobs.retry_review(outcome["job_id"], reason="Inspected concrete semantic issue")
    assert consumer.run_once()["status"] == "SUCCEEDED"
    assert agent.saw_feedback


def test_executable_numeric_check_rejects_the_real_convolution_mismatch():
    from noodle_agent.next.math_check import validate_math

    with pytest.raises(QualityFailure, match="arithmetic mismatch"):
        validate_math(
            r"逐项乘积 $1\times(-1)+1\times(-1)+1\times1+0\times0+1\times1+1\times(-1)+0\times0+0\times1+1\times1$，求和结果为 $4$。"
        )
    assert validate_math(r"计算 $2\times3=6$。")["arithmetic_checks"] == 1


def test_failed_published_output_withdraws_immediately_and_repairs_as_new_revision(store):
    import json

    from noodle_agent.next.repository import InvalidKnowledge

    consumer = worker(store)
    first = consumer.run_once()
    with store.db.engine.connect() as c:
        result = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )
    output = result["notes"][0]
    old = store.get_note(output["note_id"])
    store.jobs.reopen_invalid_output(
        first["job_id"],
        output["note_id"],
        output["revision_id"],
        reason=f"Content review failed for {output['title']}: arithmetic recheck",
    )
    with pytest.raises(InvalidKnowledge):
        store.get_note(output["note_id"])
    store.jobs.retry_review(first["job_id"], reason="Correct the checked numeric statement")
    assert consumer.run_once()["status"] == "SUCCEEDED"
    repaired = store.get_note(output["note_id"])
    assert repaired["revision"]["ordinal"] == 2
    assert repaired["revision"]["parent_revision_id"] == old["revision"]["id"]
    assert query(store, "SELECT count(*) FROM notes") == 3
    assert (
        store.get_note(output["note_id"], revision_id=output["revision_id"], historical=True)[
            "revision"
        ]["status"]
        == "WITHDRAWN"
    )


def test_whole_page_context_is_anchored_and_reused_without_repeated_calls(store):
    from noodle_agent.domain import VisualUnderstanding
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.visual_context import page_contexts

    class WholeVision:
        enabled = True
        calls = 0

        def analyze(self, path, *, external_block_id, context):
            self.calls += 1
            return VisualUnderstanding(
                external_block_id=external_block_id,
                image_sha256=sha(path.read_bytes()),
                model="injected-whole",
                caption="Two separate convolution examples",
                confidence=0.9,
                factual_observations=["Do not combine matrices from separate panels."],
            )

    store.jobs.dispatch({"SOURCE_ADDED": ["TEST"]})
    lease = store.jobs.claim("test", kind="TEST")
    journal = Checkpoints(store.repository, lease)
    with store.db.engine.connect() as c:
        revision = one(c, "SELECT current_revision_id FROM sources")["current_revision_id"]
    units = [
        Unit(
            unit_id=f"u{i}",
            block_id=f"b{i}",
            page=1,
            start=0,
            end=1,
            text="x",
            kind="IMAGE",
            asset_ref="assets/test.png",
        )
        for i in range(3)
    ]
    vision = WholeVision()
    first = page_contexts(store, journal, vision, revision, units)
    assert page_contexts(store, journal, vision, revision, units) == first
    assert vision.calls == 1
    with store.db.engine.connect() as c:
        anchor = one(c, "SELECT * FROM evidence_anchors WHERE id=:id", id=first[0]["evidence_id"])
    assert anchor["source_revision_id"] == revision and anchor["page_number"] == 1
    assert anchor["quoted_text"] == first[0]["text"]


def test_repeated_quality_repair_keeps_current_unchanged_map_revision(store):
    import json

    class ChangedTitle(Agent):
        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.write" and kwargs["user"]["content"].get(
                "repair_parent"
            ):
                result["title"] = "修复后的笔记标题"
            return result

    consumer = worker(store, agent=ChangedTitle())
    outcome = consumer.run_once()
    map_heads = []
    for _ in range(2):
        with store.db.engine.connect() as c:
            result = json.loads(
                one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
            )
        note = result["notes"][0]
        store.jobs.reopen_invalid_output(
            outcome["job_id"],
            note["note_id"],
            note["revision_id"],
            reason="Inspected quality issue",
        )
        store.jobs.retry_review(outcome["job_id"], reason="Repair current output")
        assert consumer.run_once()["status"] == "SUCCEEDED"
        with store.db.engine.connect() as c:
            result = json.loads(
                one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
            )
        map_heads.append(result["source_map"]["revision_id"])
    assert map_heads[0] == map_heads[1]
    assert query(store, "SELECT count(*) FROM notes") == 3


def test_recovery_after_corrective_publication_uses_new_head_not_original_receipt(
    store, monkeypatch
):
    import json

    agent = Agent()
    consumer = worker(store, agent=agent)
    outcome = consumer.run_once()
    with store.db.engine.connect() as c:
        result = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )
    note = result["notes"][0]
    store.jobs.reopen_invalid_output(
        outcome["job_id"], note["note_id"], note["revision_id"], reason="Inspected quality issue"
    )
    store.jobs.retry_review(outcome["job_id"], reason="Publish correction")
    original = Checkpoints.save
    failed = False

    def interrupt(self, stage, inputs, value):
        nonlocal failed
        if stage.startswith("publish.source-map.current.") and not failed:
            failed = True
            raise ConnectionError("Crash after corrective note publication")
        return original(self, stage, inputs, value)

    monkeypatch.setattr(Checkpoints, "save", interrupt)
    assert consumer.run_once()["status"] == "RETRY"
    calls = len(agent.calls)
    assert consumer.run_once()["status"] == "SUCCEEDED"
    assert len(agent.calls) == calls
    assert query(store, "SELECT count(*) FROM notes") == 3
    assert store.get_note(note["note_id"])["revision"]["ordinal"] == 2


def test_page_grounded_panels_replace_stale_roles_and_bind_their_own_evidence(store):
    from noodle_agent.domain import VisualUnderstanding
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.visual_context import contextual_panels, page_contexts

    class Vision:
        enabled = True
        calls = 0

        def analyze(self, path, *, external_block_id, context):
            return VisualUnderstanding(
                external_block_id=external_block_id,
                image_sha256=sha(path.read_bytes()),
                model="injected",
                caption="RGB input channels feed one output",
                confidence=0.9,
            )

        def analyze_batch(self, items):
            self.calls += 1
            assert all(
                item["page_context_path"].read_bytes().startswith(b"\x89PNG") for item in items
            )
            return {
                item["external_block_id"]: VisualUnderstanding(
                    external_block_id=item["external_block_id"],
                    image_sha256=sha(item["image_path"].read_bytes()),
                    model="injected",
                    caption="RGB INPUT matrix",
                    confidence=0.9,
                )
                for item in items
            }

    store.jobs.dispatch({"SOURCE_ADDED": ["TEST"]})
    lease = store.jobs.claim("test", kind="TEST")
    journal = Checkpoints(store.repository, lease)
    with store.db.engine.connect() as c:
        revision = one(c, "SELECT current_revision_id FROM sources")["current_revision_id"]
    store.put("assets/crop.png", b"crop")
    units = [
        Unit(
            unit_id=f"u{i}",
            block_id=f"b{i}",
            page=1,
            start=0,
            end=10,
            text="Original caption\n[视觉识别，需结合原图核对]\nSTALE OUTPUT GUESS",
            kind="IMAGE",
            asset_ref="assets/crop.png",
        )
        for i in range(3)
    ]
    vision = Vision()
    contexts = page_contexts(store, journal, vision, revision, units)
    panels = contextual_panels(store, journal, vision, revision, units, contexts)
    assert contextual_panels(store, journal, vision, revision, units, contexts) == panels
    assert vision.calls == 1 and len(panels) == 3
    for panel in panels:
        assert "STALE" not in panel["text"] and "RGB INPUT" in panel["text"]
        assert panel["text"].startswith("Original caption")
        with store.db.engine.connect() as c:
            anchor = one(c, "SELECT * FROM evidence_anchors WHERE id=:id", id=panel["evidence_id"])
        assert anchor["parse_run_id"] == panel["parse_id"]
        assert anchor["source_revision_id"] == revision
        assert anchor["quoted_text"] == panel["text"]
    assert all("STALE" in u.text for u in units)  # frozen original manifest remains auditable


def test_shared_checkpoint_computes_once_for_concurrent_writers(store):
    import threading
    import time

    store.jobs.dispatch({"SOURCE_ADDED": ["TEST"]})
    journal = Checkpoints(store.repository, store.jobs.claim("test", kind="TEST"))
    ready = threading.Barrier(6)
    calls = []

    def build():
        calls.append(1)
        time.sleep(0.1)  # model response arrives after competing writers enter compute
        return {"shared_page": "one frozen response"}

    def request(_):
        ready.wait(timeout=5)
        return journal.compute("same-page", {"page": 31}, build)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(request, range(6)))
    assert len(calls) == 1
    assert all(value == outputs[0] for value in outputs)


def test_full_rebuild_withdrawal_is_atomic_and_preserves_all_note_identities(store):
    import json

    from noodle_agent.next.repository import InvalidKnowledge

    consumer = worker(store)
    outcome = consumer.run_once()
    with store.db.engine.connect() as c:
        result = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )
    outputs = [(n["note_id"], n["revision_id"]) for n in result["notes"]]
    with pytest.raises(Conflict):
        store.jobs.reopen_outputs(
            outcome["job_id"], [outputs[0], (outputs[1][0], "stale")], reason="recheck"
        )
    assert all(store.get_note(n)["revision"]["status"] == "PUBLISHED" for n, _ in outputs)
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "SUCCEEDED"
    store.jobs.reopen_outputs(outcome["job_id"], outputs, reason="Full final-policy validation")
    for note_id, _ in outputs:
        with pytest.raises(InvalidKnowledge):
            store.get_note(note_id)
    store.jobs.retry_review(outcome["job_id"], reason="Full final-policy validation")
    calls_before = consumer.agent.calls.count("next.write")
    assert consumer.run_once()["status"] == "SUCCEEDED"
    assert consumer.agent.calls.count("next.write") - calls_before == len(outputs)
    assert query(store, "SELECT error_json FROM jobs WHERE kind='GENERATE_ATOMICS'") == "{}"
    for note_id, previous in outputs:
        current = store.get_note(note_id)
        assert current["revision"]["ordinal"] == 2
        assert current["revision"]["parent_revision_id"] == previous
    assert query(store, "SELECT count(*) FROM notes") == 3


def test_rebuild_cli_uses_completed_manifest_without_loading_legacy_service(
    store, monkeypatch, capsys
):
    from noodle_agent import cli

    result = worker(store).run_once()
    monkeypatch.setattr(cli, "_service", lambda: pytest.fail("Legacy composition is forbidden"))
    cli.main(
        [
            "next-rebuild",
            "--data-dir",
            str(store.root),
            "--job-id",
            result["job_id"],
            "--reason",
            "Final policy validation",
        ]
    )
    assert '"rebuild_notes": 2' in capsys.readouterr().out
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "PENDING"
    assert query(store, "SELECT count(*) FROM note_revisions WHERE status='WITHDRAWN'") == 2
    cli.main(
        [
            "next-cancel-rebuild",
            "--data-dir",
            str(store.root),
            "--job-id",
            result["job_id"],
            "--reason",
            "Cancel the trial",
        ]
    )
    assert '"restored_notes": 2' in capsys.readouterr().out
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "SUCCEEDED"


def test_recovery_rejects_old_head_if_writing_policy_changed(store, monkeypatch):
    original_save = Checkpoints.save

    def crash_after_first_publication(self, stage, inputs, value):
        if stage == "publish.note-0":
            raise ConnectionError("stopped after first head")
        return original_save(self, stage, inputs, value)

    monkeypatch.setattr(Checkpoints, "save", crash_after_first_publication)
    consumer = worker(store)
    assert consumer.run_once()["status"] == "RETRY"
    monkeypatch.setattr(Checkpoints, "save", original_save)
    original_init = Generation.__init__

    def changed_policy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.writing_policy += "\nA different writing policy."

    monkeypatch.setattr(Generation, "__init__", changed_policy)
    assert consumer.run_once()["status"] == "REVIEW"
    assert query(store, "SELECT count(*) FROM note_revisions") == 1


def test_cancel_partial_rebuild_restores_original_heads_and_fences_late_worker(store, monkeypatch):
    import json

    from noodle_agent.next.jobs import Lease
    from noodle_agent.next.recovery import cancel_rebuild

    consumer = worker(store)
    result = consumer.run_once()
    with store.db.engine.connect() as c:
        baseline = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )
    pairs = [(o["note_id"], o["revision_id"]) for o in baseline["notes"]]
    store.jobs.reopen_outputs(result["job_id"], pairs, reason="Final policy trial", rebuild=True)
    store.jobs.retry_review(result["job_id"], reason="Final policy trial")
    original = Generation.generate_note

    def interrupt_after_publish(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise ConnectionError("Operator interruption after one repaired publication")

    monkeypatch.setattr(Generation, "generate_note", interrupt_after_publish)
    assert consumer.run_once()["status"] == "RETRY"
    late = store.jobs.claim("late", kind=consumer.kind)
    cancellation = cancel_rebuild(
        store, result["job_id"], reason="User cancelled full regeneration"
    )
    assert cancellation["restored_notes"] == 2
    assert len(cancellation["discarded_revisions"]) == 1
    assert all(store.get_note(n)["revision"]["id"] == r for n, r in pairs)
    with pytest.raises(Conflict):
        store.jobs.finish(Lease(late.job_id, late.token, late.input), {})
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "SUCCEEDED"
    assert (
        query(store, "SELECT count(*) FROM events WHERE event_type='GENERATION_REBUILD_CANCELLED'")
        == 1
    )
    # Re-withdrawing a restored head emits a new event instead of losing the transition.
    before_events = query(store, "SELECT count(*) FROM events WHERE event_type='NOTE_INVALIDATED'")
    store.jobs.reopen_outputs(result["job_id"], pairs, reason="Another explicit trial")
    assert (
        query(store, "SELECT count(*) FROM events WHERE event_type='NOTE_INVALIDATED'")
        == before_events + 2
    )


def test_cancel_rebuild_does_not_restore_a_deleted_source(store):
    import json

    from noodle_agent.next.recovery import cancel_rebuild

    outcome = worker(store).run_once()
    with store.db.engine.connect() as c:
        baseline = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )
        source_id = one(c, "SELECT id FROM sources")["id"]
    pairs = [(o["note_id"], o["revision_id"]) for o in baseline["notes"]]
    store.jobs.reopen_outputs(outcome["job_id"], pairs, reason="Trial", rebuild=True)
    store.set_source_deleted(source_id, True)
    with pytest.raises(Conflict, match="source or policy changed"):
        cancel_rebuild(store, outcome["job_id"], reason="Cancel trial")
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "REVIEW"
    assert query(store, "SELECT count(*) FROM note_revisions WHERE status='WITHDRAWN'") == 2
    assert query(store, "SELECT count(*) FROM events WHERE event_type='NOTE_RESTORED'") == 0


def test_cancel_rebuild_cannot_reverse_an_unrelated_quality_withdrawal(store):
    import json

    from noodle_agent.next.recovery import cancel_rebuild

    result = worker(store).run_once()
    with store.db.engine.connect() as c:
        output = json.loads(
            one(c, "SELECT result_json FROM jobs WHERE kind='GENERATE_ATOMICS'")["result_json"]
        )["notes"][0]
    store.jobs.reopen_invalid_output(
        result["job_id"],
        output["note_id"],
        output["revision_id"],
        reason="Known content error, not a rebuild",
    )
    with pytest.raises(Conflict, match="explicit full-rebuild cycle"):
        cancel_rebuild(
            store, result["job_id"], reason="Cancellation must not resurrect known errors"
        )
    assert query(store, "SELECT status FROM jobs WHERE kind='GENERATE_ATOMICS'") == "REVIEW"
