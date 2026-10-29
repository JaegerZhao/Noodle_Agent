"""Source-aligned drafts cannot enter knowledge before assembly acceptance."""

import fitz
import pytest

from noodle_agent.domain import ParsedBlock, ParsedDocument
from noodle_agent.next.assembly_generation import checked_source_draft
from noodle_agent.next.contracts import Candidate, Review, Scope, Section
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation import Generation
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repository import one
from noodle_agent.next.section_contracts import Boundaries, SourceDraft
from noodle_agent.next.source_sections import units_from_blocks, validate_boundaries
from noodle_agent.next.source_structure import source_structure
from noodle_agent.next.store import Store


class Parser:
    name = "source-test"

    def submit(self, *args):
        return {"external_id": "test", "state": "done"}

    def download_and_normalize(self, *args):
        return ParsedDocument(
            title="Example",
            page_count=2,
            markdown="",
            parser_name=self.name,
            parser_version="test",
            blocks=[
                ParsedBlock(
                    block_id="h1",
                    page_index=0,
                    page_label="1",
                    text="Term frequency",
                    block_type="HEADING",
                ),
                ParsedBlock(
                    block_id="a", page_index=0, page_label="1", text="Term frequency counts a term."
                ),
                ParsedBlock(
                    block_id="h2",
                    page_index=1,
                    page_label="2",
                    text="Inverse document frequency",
                    block_type="HEADING",
                ),
                ParsedBlock(
                    block_id="b",
                    page_index=1,
                    page_label="2",
                    text="IDF depends on document frequency.",
                ),
            ],
        )


class Agent:
    def __init__(self, fail=False, omit=False):
        self.operations = []
        self.fail = fail
        self.omit = omit

    def structured_json(self, *, operation, user, **kwargs):
        self.operations.append(operation)
        content = user["content"]
        if operation == "next.section_write":
            units = content["units"]
            return {
                "title": content["plan"]["title"],
                "sections": [
                    {
                        "key": "explain",
                        "title": "解释",
                        "markdown": "\n\n".join(u["text"] for u in units),
                    }
                ],
                "assignments": {u["unit_id"]: "explain" for u in units},
                "omissions": {},
            }
        if operation == "next.assembly_review":
            assembly = content["assembly"]
            return {
                "passed": not self.fail,
                "checked_plans": [a["plan_key"] for a in assembly],
                "checked_omissions": [u for a in assembly for u in a["proposed_omissions"]],
                "issues": [
                    {"plan_key": assembly[0]["plan_key"], "issue": "Important content missing"}
                ]
                if self.fail
                else [],
            }
        if operation == "next.section_repair":
            return {
                "sections": [
                    {"key": s["key"], "markdown": s["markdown"]}
                    for s in content["draft"]["sections"]
                ]
            }
        raise AssertionError(operation)


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path / "next")
    with fitz.open() as pdf:
        for title in ["Term frequency", "Inverse document frequency"]:
            pdf.new_page().insert_text((60, 60), title)
        pdf.set_toc([[1, "Term frequency", 1], [1, "Inverse document frequency", 2]])
        source = store.add_source(pdf.tobytes(), "Example")
    dispatch(store)
    lease = store.jobs.claim("test", kind="GENERATE_ATOMICS")
    yield store, lease, source
    store.close()


def generation(store, lease, agent):
    return Generation(
        store,
        Parser(),
        agent,
        lease,
        profile={"parser": "source-test", "generation_layout": "source-sections-v1"},
        concurrency=2,
    )


def test_source_sections_publish_after_assembly_and_keep_outline(prepared):
    store, lease, source = prepared
    agent = Agent()
    result = generation(store, lease, agent).run()
    assert len(result["notes"]) == 2
    assert agent.operations.count("next.section_write") == 2
    assert "next.plan" not in agent.operations and "next.judge" not in agent.operations
    structure = source_structure(store, source["revision_id"])
    assert all(n["primary_node_id"] for n in structure["notes"])
    assert structure["planning_status"] == "COMPLETE_SOURCE_SECTIONS"
    for output in result["notes"]:
        note = store.get_note(output["note_id"])
        assert note["revision"]["author"] == "next.assembly_review"
    # An interrupted caller can reuse every expensive stage and the batch receipt.
    before = len(agent.operations)
    again = generation(store, lease, agent).run()
    assert len(agent.operations) == before and again["notes"] == result["notes"]


def test_failed_assembly_does_not_publish_or_fan_out(prepared):
    store, lease, _ = prepared
    with pytest.raises(QualityFailure, match="Assembly content review failed|REPAIR_NO_PROGRESS"):
        generation(store, lease, Agent(fail=True)).run()
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "select count(*) from notes where current_revision_id is not null"
            ).scalar_one()
            == 0
        )
        assert (
            c.exec_driver_sql(
                "select count(*) from events where event_type='NOTE_PUBLISHED'"
            ).scalar_one()
            == 0
        )


def test_batch_publication_rolls_back_all_heads(prepared):
    store, lease, source = prepared
    parse = store.record_parse(
        source["revision_id"], "test", {}, {"blocks": [{"id": "b", "page": 1, "text": "Fact"}]}
    )
    evidence = store.add_evidence(
        source["revision_id"], "Fact", page=1, block_id="b", parse_id=parse
    )
    notes = [store.create_note("ATOMIC", str(i)) for i in range(2)]
    candidates = [
        Candidate(
            note_id=n,
            title=str(i),
            scope=Scope(includes=["Fact"]),
            sections=[
                Section(
                    section_id="s",
                    title="Fact",
                    question="Fact",
                    scope=Scope(includes=["Fact"]),
                    markdown="Fact",
                    evidence_ids=[evidence if i == 0 else "unknown"],
                )
            ],
        )
        for i, n in enumerate(notes)
    ]
    checked = [
        (
            c,
            Review(
                candidate_sha256=c.fingerprint,
                policy_version=c.policy_version,
                reviewer="test",
                passed=True,
            ),
        )
        for c in candidates
    ]
    with pytest.raises(ValueError):
        store.publisher.publish_batch(checked, lease=lease)
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "select count(*) from notes where current_revision_id is not null"
            ).scalar_one()
            == 0
        )


def test_draft_omission_is_explicit_not_missing():
    d = SourceDraft(
        title="TF",
        sections=[{"key": "s", "title": "TF", "markdown": "Term frequency."}],
        assignments={"u1": "s"},
        omissions={"u2": "Only a repeated page title"},
    )
    checked_source_draft(d, ["u1", "u2"], [])
    with pytest.raises(QualityFailure):
        checked_source_draft(d, ["u1", "u2", "u3"], [])
    with pytest.raises(QualityFailure):
        checked_source_draft(
            d.model_copy(update={"omissions": {"u1": "duplicate"}}), ["u1", "u2"], []
        )


def test_image_only_unit_and_math_block_are_not_dropped():
    units = units_from_blocks(
        {
            "blocks": [
                {"id": "a", "page": 1, "kind": "IMAGE", "text": "", "asset_ref": "assets/a.png"},
                {
                    "id": "b",
                    "page": 1,
                    "kind": "TEXT",
                    "text": "Formula:\n\n$$x^2$$\n\nHere x is a real number.",
                },
            ]
        }
    )
    assert len(units) == 2 and units[0].asset_ref
    assert "Here x" in units[1].text and "$$x^2$$" in units[1].text


def test_boundaries_preserve_same_page_order():
    units = units_from_blocks(
        {
            "blocks": [
                {"id": "a", "page": 1, "kind": "HEADING", "text": "TF"},
                {"id": "b", "page": 1, "kind": "HEADING", "text": "IDF"},
            ]
        }
    )
    good = Boundaries(sections=[{"start_unit": u.unit_id, "title": u.text} for u in units])
    validate_boundaries(good, units)
    with pytest.raises(QualityFailure):
        validate_boundaries(Boundaries(sections=list(reversed(good.sections))), units)


def test_concept_batch_shares_cards_and_checks_all_decisions():
    from noodle_agent.next.concepts.batching import batch_decisions

    class Calls:
        def __init__(self):
            self.operations = []

        def run(self, operation, system, content, contract, check=None):
            self.operations.append(operation)
            if operation == "next.concept_batch_decide":
                value = contract.model_validate(
                    {
                        "decisions": {
                            f["id"]: {
                                "action": "INSUFFICIENT",
                                "reason": "Only navigation, no teaching explanation",
                            }
                            for f in content["fragments"]
                        }
                    }
                )
                if check:
                    check(value)
                return value
            return contract.model_validate(
                {"checks": {f["id"]: {"passed": True, "issues": []} for f in content["fragments"]}}
            )

    fragments = [
        {
            "fragment_id": f"fragment_{i}",
            "title": "TF-IDF",
            "section_title": "Navigation",
            "text": "Chapter title only",
            "scope": {"includes": ["Navigation"]},
        }
        for i in range(4)
    ]
    calls = Calls()
    checked = batch_decisions(None, fragments, [], calls)
    assert set(checked) == {f["fragment_id"] for f in fragments}
    assert calls.operations == ["next.concept_batch_decide", "next.concept_batch_review"]


def test_concept_batch_rejects_missing_fragment_decision():
    from noodle_agent.next.concepts.batching import batch_decisions

    class Calls:
        def run(self, operation, system, content, contract, check=None):
            value = contract.model_validate({"decisions": {}})
            if check:
                check(value)
            return value

    f = {
        "fragment_id": "fragment_a",
        "title": "TF-IDF",
        "section_title": "TF",
        "text": "Term frequency",
        "scope": {"includes": ["TF"]},
    }
    with pytest.raises(QualityFailure, match="every short fragment"):
        batch_decisions(None, [f], [], Calls())


def test_reversed_assignments_are_normalized_without_inventing_coverage():
    d = SourceDraft.model_validate(
        {
            "title": "TF",
            "sections": [{"key": "s", "title": "TF", "markdown": "Term frequency."}],
            "assignments": {"s": ["u1", "u2"]},
        }
    )
    assert d.assignments == {"u1": "s", "u2": "s"}
    checked_source_draft(d, ["u1", "u2"], [])
    with pytest.raises(QualityFailure):
        checked_source_draft(d, ["u1", "u2", "u3"], [])


def test_cold_visual_derivation_does_not_overwrite_raw_parse(prepared):
    import json

    from noodle_agent.domain import VisualUnderstanding
    from noodle_agent.next.contracts import sha

    store, lease, _ = prepared

    class ImageParser(Parser):
        def download_and_normalize(self, *args):
            doc = super().download_and_normalize(*args)
            destination = args[2]
            destination.mkdir(parents=True, exist_ok=True)
            image = destination / "diagram.png"
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
            pixmap.clear_with(255)
            pixmap.save(image)
            return doc.model_copy(
                update={
                    "blocks": [
                        b.model_copy(update={"asset_path": str(image)}) if b.block_id == "a" else b
                        for b in doc.blocks
                    ]
                }
            )

    class Vision:
        enabled = True
        model_name = "test-visual"

        def analyze_batch(self, items):
            return {
                i["external_block_id"]: VisualUnderstanding(
                    external_block_id=i["external_block_id"],
                    image_sha256=sha(i["image_path"].read_bytes()),
                    model=self.model_name,
                    caption="A source diagram",
                    confidence=0.9,
                )
                for i in items
            }

    g = Generation(
        store,
        ImageParser(),
        Agent(),
        lease,
        vision=Vision(),
        profile={"parser": "source-test", "generation_layout": "source-sections-v1"},
        concurrency=2,
    )
    result = g.run()
    assert len(result["notes"]) == 2
    with store.db.engine.connect() as c:
        raw = one(c, "select * from job_checkpoints where stage='parse.normalized'")
        derived = one(c, "select * from job_checkpoints where stage='source.visual.normalized'")
    original = json.loads(store.read(raw["artifact_ref"], raw["artifact_sha256"]))
    visual = json.loads(store.read(derived["artifact_ref"], derived["artifact_sha256"]))
    assert "视觉识别" not in original["artifact"]["blocks"][1]["text"]
    assert "视觉识别" in visual["artifact"]["blocks"][1]["text"]
    assert original["parse_id"] != visual["parse_id"]


def test_local_code_checks_catch_syntax_and_lost_source_initialization():
    import json

    from noodle_agent.next.source_quality import code_findings

    record = {
        "key": "source_a",
        "draft": {
            "sections": [{"key": "s", "markdown": "```python\nmodel = fit(data_words)\n```"}]
        },
        "contexts": [{"text": json.dumps({"ocr_text": ["data_words = list(tokens)"]})}],
    }
    assert "data_words" in code_findings([record])[0].issue
    record["draft"]["sections"][0]["markdown"] = (
        "```python\ndata_words = list(tokens)\nmodel = fit(data_words)\n```"
    )
    assert code_findings([record]) == []
    record["draft"]["sections"][0]["markdown"] = "```python\nvalue = [1, 2]]\n```"
    assert "语法错误" in code_findings([record])[0].issue


def test_nested_assignments_can_be_lifted_but_not_conflicted():
    raw = {
        "title": "TF",
        "sections": [{"key": "s", "title": "TF", "markdown": "Text.", "assignments": {"u1": "s"}}],
    }
    assert SourceDraft.model_validate(raw).assignments == {"u1": "s"}
    with pytest.raises(ValueError):
        SourceDraft.model_validate({**raw, "assignments": {"u1": "other"}})


def test_precise_page_ocr_uses_its_own_system_prompt(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from unittest.mock import MagicMock

    from noodle_agent.adapters.vision import DoubaoVisionProvider
    from noodle_agent.config import Settings

    client = MagicMock()
    response = MagicMock()
    response.id = "test-ocr"
    response.usage = None
    response.choices[0].message.content = json.dumps(
        {
            "images": [
                {
                    "asset_id": "page-1",
                    "type": "CODE",
                    "caption": "Code",
                    "ocr_text": ["data = [1, 2]"],
                    "confidence": 0.9,
                }
            ]
        }
    )
    client.chat.completions.create.return_value = response
    monkeypatch.setattr("noodle_agent.adapters.vision.OpenAI", lambda **kwargs: client)
    settings = replace(
        Settings.from_env(tmp_path), vision_api_key="test-key", vision_max_output_tokens=900
    )
    telemetry = MagicMock()
    provider = DoubaoVisionProvider(settings, telemetry)
    image = tmp_path / "page.png"
    image.write_bytes(b"test-image")
    result = provider.analyze(
        image, external_block_id="page-1", context={"extraction_mode": "SOURCE_PAGE"}
    )
    request = client.chat.completions.create.call_args.kwargs
    assert "json" in request["messages"][0]["content"].lower()
    assert "不要输出 OCR 全文" not in request["messages"][0]["content"]
    assert request["max_tokens"] >= 4800
    assert result.ocr_text == ["data = [1, 2]"]
    assert result.prompt_version == "source-page-evidence-v1"
    assert telemetry.record_vision.call_args.kwargs["prompt_version"] == "source-page-evidence-v1"


def test_two_uncertain_panels_require_the_original_page(prepared):
    from noodle_agent.domain import VisualUnderstanding
    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.generation_contracts import Unit
    from noodle_agent.next.visual_context import page_contexts

    store, lease, source = prepared

    class Vision:
        enabled = True
        model_name = "injected"
        calls = 0

        def analyze(self, path, **kwargs):
            self.calls += 1
            return VisualUnderstanding(
                external_block_id=kwargs["external_block_id"],
                image_sha256=sha(path.read_bytes()),
                model=self.model_name,
                prompt_version="source-page-evidence-v1",
                caption="Full relation",
                ocr_text=["D = U S V_T"],
                confidence=0.95,
            )

    units = [
        Unit(
            unit_id=f"u{i}",
            block_id=f"b{i}",
            page=1,
            start=0,
            end=1,
            text="k",
            kind="IMAGE",
            asset_ref=f"assets/{i}.png",
            visual={"group_type": "MULTI_PANEL", "confidence": 0.7},
        )
        for i in range(2)
    ]
    vision = Vision()
    contexts = page_contexts(
        store,
        Checkpoints(store.repository, lease),
        vision,
        source["revision_id"],
        units,
        source_aligned=True,
    )
    assert vision.calls == 1 and len(contexts) == 1
    assert contexts[0]["input"]["policy"] == "whole-page-source-v4"


def test_concept_batch_review_cannot_skip_a_fragment():
    from noodle_agent.next.concepts.batching import ReviewBatch, validate_review_batch

    with pytest.raises(QualityFailure, match="every fragment"):
        validate_review_batch(ReviewBatch(checks={"f0": {"passed": True}}), {"f0", "f1"})


def test_repair_can_exclude_a_pure_administrative_note_without_losing_coverage():
    from noodle_agent.next.assembly_generation import apply_source_patch
    from noodle_agent.next.section_contracts import DraftPatch, SourceDraft

    old = SourceDraft(
        title="版权",
        sections=[{"key": "notice", "title": "版权", "markdown": "使用限制。"}],
        assignments={"u1": "notice"},
    )
    fixed = apply_source_patch(
        old,
        DraftPatch(sections=[], omitted_assignments={"u1": "纯版权使用限制，无教学内容"}),
        ["u1"],
        [],
    )
    assert fixed.sections == [] and fixed.assignments == {}
    assert set(fixed.omissions) == {"u1"}
    with pytest.raises(QualityFailure, match="Only an assigned"):
        apply_source_patch(
            old, DraftPatch(sections=[], omitted_assignments={"invented": "无内容"}), ["u1"], []
        )


def test_code_guard_recognizes_valid_inline_initialization():
    import json

    from noodle_agent.next.source_quality import code_findings

    record = {
        "key": "p",
        "draft": {
            "sections": [
                {
                    "key": "s",
                    "markdown": "先用 `vect = Vectorizer()` 创建实例。\n\n```python\nresult = vect.transform(data)\n```",
                }
            ]
        },
        "contexts": [{"text": json.dumps({"ocr_text": ["vect = Vectorizer()"]})}],
    }
    assert code_findings([record]) == []
    record["draft"]["sections"][0]["markdown"] = "```python\nresult = vect.transform(data)\n```"
    assert "vect" in code_findings([record])[0].issue


def test_automatic_identity_partial_coverage_does_not_forbid_future_formulas():
    from noodle_agent.next.concepts.contracts import Identity

    identity = Identity(
        title="TF-IDF",
        domain="IR",
        identity_key="tfidf",
        question="TF-IDF如何加权",
        scope={"includes": ["定义"], "excludes": ["TF和IDF公式"], "prerequisites": ["语料"]},
    )
    assert identity.scope.excludes == []
    assert identity.scope.includes == ["定义"] and identity.scope.prerequisites == ["语料"]


def test_concept_heading_normalization_preserves_code_and_hierarchy():
    from noodle_agent.next.markdown_checks import section_headings

    value = "## 步骤\n\n### 示例\n\n```python\n# Actual code comment\nprint(1)\n```\n"
    normalized = section_headings(value)
    assert normalized.startswith("### 步骤\n\n#### 示例")
    assert "# Actual code comment" in normalized
    assert section_headings(normalized) == normalized


def test_new_concept_chapter_metadata_belongs_to_its_creating_decision():
    from noodle_agent.next.concepts.builder import chapter_declaration

    supplement = (
        {"action": "UPDATE_SECTION"},
        {"title": "原理"},
        {"section_question": "原理是什么"},
    )
    creator = (
        {"action": "CREATE_SECTION"},
        {"title": "实现"},
        {"section_question": "如何用gensim实现"},
    )
    assert chapter_declaration([supplement, creator]) is creator


def test_concept_coverage_growth_preserves_preexisting_explicit_boundaries():
    from types import SimpleNamespace

    from noodle_agent.next.concepts.builder import published_scope
    from noodle_agent.next.contracts import Scope

    old = Scope(includes=["定义"], excludes=["其他领域同名对象"], prerequisites=["语料"])
    current = published_scope(old, [SimpleNamespace(question="公式")])
    assert current.includes == ["定义", "公式"]
    assert current.excludes == old.excludes and current.prerequisites == old.prerequisites


def test_recovery_keeps_versioned_plan_when_an_old_plan_receipt_exists(prepared):
    store, lease, _ = prepared
    agent = Agent()
    g = generation(store, lease, agent)
    first = g.run()
    g.journal.save("source.sections.plan", {"fixture": "old-policy"}, {"plans": []})
    calls = len(agent.operations)
    again = generation(store, lease, agent).run()
    assert again["notes"] == first["notes"]
    assert len(agent.operations) == calls
