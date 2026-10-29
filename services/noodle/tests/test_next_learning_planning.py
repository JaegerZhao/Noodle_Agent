"""Reviewed non-teaching exclusions and immutable source-image provenance."""

from collections import Counter
from types import SimpleNamespace

import fitz
import pytest
from pydantic import ValidationError
from test_next_source_sections import Agent, Parser, prepared  # noqa: F401

from noodle_agent.domain import VisualUnderstanding
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.concepts.entity_contracts import RoutingDecision
from noodle_agent.next.concepts.routing_protocol import expand
from noodle_agent.next.contracts import sha
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation import Generation
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repository import InvalidKnowledge
from noodle_agent.next.source_sections import (
    make_plan,
    units_from_blocks,
    validate_assisted_boundaries,
)
from noodle_agent.next.store import Store


def planning_fixture(tmp_path, reject=False):
    store = Store(tmp_path / "planning")
    with fitz.open() as pdf:
        for _ in range(4):
            pdf.new_page()
        store.add_source(pdf.tobytes(), "Methods")
    dispatch(store)
    lease = store.jobs.claim("test", kind="GENERATE_ATOMICS")
    artifact = {"page_count": 4, "blocks": [
        {"id": "b0", "page": 1, "text": "Course XYZ, lecturer, university", "kind": "TEXT"},
        {"id": "b1", "page": 2, "text": "Introduction", "kind": "HEADING"},
        {"id": "b2", "page": 2, "text": "The method minimizes a defined loss under these assumptions.", "kind": "TEXT"},
        {"id": "b3", "page": 3, "text": "Full procedure and worked example.", "kind": "TEXT"},
        {"id": "b4", "page": 4, "text": "All rights reserved. Do not reproduce.", "kind": "TEXT"},
        {"id": "b5", "page": 4, "text": "The method fails when its assumptions are violated.", "kind": "TEXT"},
    ]}
    requests = []

    def call(stage, operation, instruction, payload, contract, validator, **kwargs):
        assert kwargs["use_pro_model"] is True
        requests.append((operation, payload))
        if operation == "next.structure":
            raw = {"sections": [
                {"start_unit": "u00000", "title": "Cover", "disposition": "EXCLUDE", "reason": "Pure course identification"},
                {"start_unit": "u00001", "title": "Complete method", "reason": "Definition, assumptions, procedure and example"},
                {"start_unit": "u00004", "title": "Copyright", "disposition": "EXCLUDE", "reason": "Pure copyright"},
                {"start_unit": "u00005", "title": "Method limitations", "reason": "Independent limitations"},
            ]}
        else:
            assert "All rights reserved" in str(payload)
            raw = {"passed": not reject, "checked_starts": ["u00000", "u00004"], "checked_content_starts": ["u00001", "u00005"], "issues": ["Teaching material was excluded"] if reject else []}
        value = contract.model_validate(raw)
        validator(value)
        return value

    g = SimpleNamespace(store=store, lease=lease, call=call, profile={"planning_mode": "learning-topics-v5", "planning_model": "configured-pro"})
    return store, g, artifact, requests


def test_pro_excludes_administration_and_retains_teaching_introduction(tmp_path):
    store, g, artifact, requests = planning_fixture(tmp_path)
    try:
        units = units_from_blocks(artifact)
        plan = make_plan(g, {"parse_id": "fixture", "artifact": artifact}, units)
        assert [operation for operation, _ in requests] == ["next.structure", "next.structure_exclusion_review"]
        assert plan["planning_model"] == "configured-pro"
        assert plan["plans"][0]["pages"] == [2, 3]
        assert {u for p in plan["plans"] for u in p["unit_ids"]} == {"u00001", "u00002", "u00003", "u00005"}
        counts = Counter(u for p in plan["plans"] for u in p["unit_ids"])
        counts.update(e["unit_id"] for e in plan["layout_exclusions"])
        assert set(counts) == {u.unit_id for u in units} and set(counts.values()) == {1}
    finally:
        store.close()


def test_independent_exclusion_check_blocks_lost_teaching_content(tmp_path):
    store, g, artifact, _requests = planning_fixture(tmp_path, reject=True)
    try:
        with pytest.raises(QualityFailure, match="exclusion check rejected"):
            make_plan(g, {"parse_id": "fixture", "artifact": artifact}, units_from_blocks(artifact))
    finally:
        store.close()


def test_unverified_images_cannot_be_discarded_from_text_hints():
    from noodle_agent.next.section_contracts import Boundaries
    units = units_from_blocks({"blocks": [{"id": "image", "page": 1, "kind": "IMAGE", "text": "", "asset_ref": "assets/image.png"}]})
    value = Boundaries(sections=[{"start_unit": units[0].unit_id, "title": "Cover", "disposition": "EXCLUDE", "reason": "Looks administrative"}])
    with pytest.raises(QualityFailure, match="unverified image"):
        validate_assisted_boundaries(value, units, True, True)


def test_empty_text_image_completes_real_pipeline_without_forged_quote(prepared):  # noqa: F811
    store, lease, source = prepared
    image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False).tobytes("png")
    asset_ref = f"assets/{sha(image)}.png"
    store.put(asset_ref, image)
    artifact = {"title": "Example", "page_count": 2, "blocks": [
        {"id": "h1", "page": 1, "kind": "HEADING", "text": "Term frequency"},
        {"id": "image", "page": 1, "kind": "IMAGE", "text": "", "asset_ref": asset_ref},
        {"id": "h2", "page": 2, "kind": "HEADING", "text": "Inverse document frequency"},
        {"id": "b", "page": 2, "kind": "TEXT", "text": "IDF depends on document frequency."},
    ]}
    profile = {"parser": Parser.name, "generation_layout": "source-sections-v1", "execution_policy": "pipeline-throughput-v1", "checking_policy": "single-check-v1"}
    parse_id = store.sources.record_parse(source["revision_id"], "raw", {}, artifact, lease=lease)
    with store.db.engine.connect() as c:
        source_hash = c.exec_driver_sql("SELECT sha256 FROM source_revisions WHERE id=?", (source["revision_id"],)).scalar_one()
    Checkpoints(store.repository, lease).save("parse.normalized", {"revision": source["revision_id"], "sha256": source_hash, "profile": profile}, {"parse_id": parse_id, "artifact": artifact})

    class Vision:
        enabled = True
        model_name = "fixture"

        def analyze(self, path, *, external_block_id, context):
            return VisualUnderstanding(external_block_id=external_block_id, image_sha256=sha(path.read_bytes()), model="fixture",
                prompt_version="source-page-evidence-v1", ocr_text=["Term frequency counts occurrences."], confidence=1)

    result = Generation(store, Parser(), Agent(), lease, profile=profile, vision=Vision(), concurrency=2).run()
    assert len(result["notes"]) == 2
    with store.db.engine.connect() as c:
        evidence = c.exec_driver_sql("SELECT quoted_text,locator_json FROM evidence_anchors WHERE parse_run_id=? AND locator_json LIKE '%SOURCE_IMAGE_ASSET%'", (parse_id,)).all()
    assert len(evidence) == 1 and "教学图片" not in evidence[0][0]
    assert sha(image) in evidence[0][1] and artifact["blocks"][1]["text"] == ""
    with pytest.raises(InvalidKnowledge, match="parsed block"):
        store.sources.add_evidence(source["revision_id"], "invented meaning", page=1, block_id="image", parse_id=parse_id)
    with pytest.raises(InvalidKnowledge, match="location"):
        store.sources.add_image_evidence(source["revision_id"], page=2, block_id="image", parse_id=parse_id)
    with pytest.raises(InvalidKnowledge, match="current PDF"):
        store.sources.add_image_evidence("another-source", page=1, block_id="image", parse_id=parse_id)


def test_visual_pages_are_requested_only_when_a_topic_needs_them(monkeypatch):
    from noodle_agent.next.page_evidence import PageEvidence
    calls = []
    monkeypatch.setattr("noodle_agent.next.page_evidence.page_contexts", lambda *a, selected_pages, **kw: calls.extend(selected_pages) or [{"page": selected_pages[0]}])
    units = units_from_blocks({"blocks": [{"id": f"b{p}", "page": p, "kind": "IMAGE", "text": "", "asset_ref": f"assets/{p}.png"} for p in (1, 2, 3)]})
    g = SimpleNamespace(store=None, journal=None, parsing=SimpleNamespace(vision=None),
        lease=SimpleNamespace(input={"revision_id": "r"}), progress=lambda *_: None)
    with PageEvidence(g, units) as evidence:
        assert not evidence.futures and not calls
        assert evidence.for_units(units[:1]) == [{"page": 1}]
        assert set(evidence.futures) == {1}
    assert calls == [1]


def test_routing_requires_explicit_create_target_and_deterministic_section_roles():
    with pytest.raises(ValidationError, match="requires target"):
        RoutingDecision(assignments=[{"atomic": "A0", "action": "CREATE", "reason": "A method"}])
    decision = RoutingDecision(assignments=[{"atomic": "A0", "action": "CREATE", "target": "N1", "reason": "A method", "sections": {"S1": "examples"}}],
        new_entities=[{"ref": "N1", "title": "Method", "domain": "math", "identity_key": "method", "description": "Supported method"}])
    cards = [{"id": "a", "sections": [{"id": f"s{i}"} for i in range(12)]}]
    result = expand(decision, cards, [])
    assert result.routes["a"].sections == {f"s{i}": "examples" if i == 1 else "principles" for i in range(12)}
