"""Real generated-response normalization, exact routing, and verified visual reuse."""

import copy
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_next_structured_review import cards

from noodle_agent.domain import VisualUnderstanding
from noodle_agent.next.concepts.entity_routing import decide
from noodle_agent.next.concepts.exact_routing import canonical_decision, contract
from noodle_agent.next.contracts import sha
from noodle_agent.next.model_calls import Calls
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.section_contracts import SourceDraft
from noodle_agent.next.store import Store
from noodle_agent.next.visual_replay import VerifiedPageCache, verified_page_manifest


def test_redundant_section_units_are_checked_and_normalized_without_rewriting_text():
    raw = {"title": "Method", "sections": [{"key": "s", "title": "Definition", "markdown": "Original source-grounded text", "unit_ids": ["u"]}], "assignments": {"u": "s"}}
    before = copy.deepcopy(raw)
    value = SourceDraft.model_validate(raw)
    assert value.sections[0].markdown == raw["sections"][0]["markdown"] and value.assignments == {"u": "s"}
    assert raw == before
    bad = copy.deepcopy(raw)
    bad["assignments"] = {"u": "other"}
    with pytest.raises(ValidationError, match="disagrees"):
        SourceDraft.model_validate(bad)


def test_redundant_global_mapping_and_explicit_image_are_compiled_without_new_choices():
    raw = {"title": "Method", "sections": [
        {"key": "a", "title": "Definition", "markdown": "Original", "assignments": {"u": "a", "v": "b"}, "asset_ref": "artifacts/assets/actual.png"},
        {"key": "b", "title": "Example", "markdown": "Example", "assignments": {"u": "a", "v": "b"}}], "assignments": {"u": "a", "v": "b"}}
    before = copy.deepcopy(raw)
    result = SourceDraft.model_validate(raw)
    assert result.assignments == raw["assignments"]
    assert result.sections[0].markdown == "Original\n\n![教学插图](artifacts/assets/actual.png)"
    assert raw == before
    bad = copy.deepcopy(raw)
    bad["sections"][1]["assignments"]["v"] = "a"
    with pytest.raises(ValidationError, match="Conflicting"):
        SourceDraft.model_validate(bad)


def test_entity_subject_metadata_does_not_invalidate_a_comprehensive_note():
    from noodle_agent.next.contracts import EntityHints

    assert len(EntityHints(subjects=["Subject " + str(i) for i in range(12)]).subjects) == 12


def test_actual_failed_concept_review_accusations_remain_auditable_without_becoming_content_gates():
    from noodle_agent.next.structured_quality import QualityFinding, finding_protocol_issues

    root = Path(__file__).resolve().parents[3] / ".local-only/week4-throughput-20261023T172412Z"
    if not root.exists():
        pytest.skip("Optional immutable failed review fixture")
    c = sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
    rows = c.execute("SELECT artifact_ref FROM job_checkpoints WHERE job_id IN (SELECT id FROM jobs WHERE status='REVIEW') AND stage LIKE 'next.entity_review.%'").fetchall()
    c.close()
    assert len(rows) == 4
    for (ref,) in rows:
        raw = json.loads((root / "library" / ref).read_text(encoding="utf-8"))
        findings = [QualityFinding.model_validate(f) for v in raw["chapters"].values() for f in v["findings"]]
        assert finding_protocol_issues(findings, "Actual current candidate", {})
        assert raw["chapters"]  # original receipt is never normalized in place


@pytest.mark.parametrize("different", [False, True])
def test_concurrent_immutable_artifact_publication_never_replaces_an_existing_file(tmp_path, monkeypatch, different):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from noodle_agent.next.repository import InvalidKnowledge

    store = Store(tmp_path / "race")
    barrier, original_link = Barrier(2), os.link

    def simultaneous_link(source, target):
        barrier.wait(timeout=10)
        return original_link(source, target)

    monkeypatch.setattr("noodle_agent.next.repository.os.link", simultaneous_link)

    def write(body):
        try:
            store.put("artifacts/checkpoints/same.json", body)
            return "ok"
        except InvalidKnowledge:
            return "conflict"

    try:
        bodies = [b"original", b"different" if different else b"original"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, bodies))
        assert sorted(results) == (["conflict", "ok"] if different else ["ok", "ok"])
        assert store.path("artifacts/checkpoints/same.json").read_bytes() in bodies
    finally:
        store.close()


def test_critical_reasoning_is_selected_and_bound_to_the_recorded_request(tmp_path, monkeypatch):
    monkeypatch.setenv('NOODLE_NEXT_PRO_USAGE', 'selective')
    from noodle_agent.next.guard_review import GuardCheck
    from noodle_agent.next.structured_quality import critical_thinking

    class Agent:
        pro_model = "deepseek-v4-pro"
        fast_model = "deepseek-v4-flash"

        def structured_json(self, **kwargs):
            assert kwargs["thinking"] is True
            return {"findings": {}}

    agent = Agent()
    assert critical_thinking(agent, "next.assembly_review", True, "essential-v2")
    assert critical_thinking(agent, "next.structure_exclusion_review", True, "essential-v2")
    assert not critical_thinking(agent, "next.structure", True, "essential-v2")
    assert not critical_thinking(agent, "next.section_write", False, "essential-v2")
    assert not critical_thinking(agent, "next.entity_review", True, "essential-v1")
    store = Store(tmp_path / "thinking")
    try:
        Calls(store, agent, review_policy="essential-v2").run("next.local_finding_review", "Check actual mathematics", {}, GuardCheck, use_pro_model=True)
        receipt = next((store.root / "artifacts/model").glob("*.json"))
        assert json.loads(receipt.read_text(encoding="utf-8"))["request"]["thinking"] is True
        assert json.loads(receipt.read_text(encoding="utf-8"))["request"]["reasoning_effort"] == "low"
    finally:
        store.close()


def test_provider_explicitly_limits_reasoning_effort_instead_of_using_high_default():
    from unittest.mock import MagicMock, patch

    from noodle_agent.adapters.llm import OpenAIKnowledgeAgent

    settings = MagicMock(llm_api_key="test", llm_base_url="https://example.invalid", llm_fast_model="deepseek-v4-flash", llm_pro_model="deepseek-v4-pro", note_language="zh-CN")
    with patch("noodle_agent.adapters.llm.OpenAI") as client:
        response = MagicMock()
        response.choices[0].message.content = '{"findings":{}}'
        response.choices[0].finish_reason = "stop"
        client.return_value.chat.completions.create.return_value = response
        agent = OpenAIKnowledgeAgent(settings)
        assert agent.structured_json(system="Verify", user={}, operation="next.local_finding_review", prompt_version="test", use_pro_model=True, thinking=True) == {"findings": {}}
        kwargs = client.return_value.chat.completions.create.call_args.kwargs
        assert kwargs["reasoning_effort"] == "low" and kwargs["max_tokens"] == 16384
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"


def test_real_svm_responses_accept_only_consistent_redundant_source_fields():
    root = Path(__file__).resolve().parents[3] / ".local-only/week4-throughput-20261023T165041Z"
    if not root.exists():
        pytest.skip("Optional immutable failure fixture")
    c = sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True)
    rows = c.execute("SELECT artifact_ref FROM job_checkpoints WHERE stage LIKE 'source.write.source_658f%'").fetchall()
    c.close()
    assert len(rows) == 2
    for (ref,) in rows:
        raw = json.loads((root / "library" / ref).read_text(encoding="utf-8"))
        result = SourceDraft.model_validate(raw)
        assert [s.markdown for s in result.sections] == [s["markdown"] for s in raw["sections"]]
        assert result.assignments == raw["assignments"]


def test_exact_atomic_schema_disallows_second_background_assignment():
    schema = contract(["A0"])
    one = {"atomic": "A0", "action": "CREATE", "target": "N0", "reason": "Primary method"}
    with pytest.raises(ValidationError, match="one decision"):
        schema.model_validate({"assignments": [one, {**one, "action": "REUSE", "target": "C0"}]})
    value = schema.model_validate({"assignments": {"A0": {k: v for k, v in one.items() if k != "atomic"}}})
    assert canonical_decision(value).assignments[0].target == "N0"


def test_primary_routing_protocol_error_is_corrected_before_becoming_a_review(tmp_path):
    class Agent:
        name = "fixture"
        pro_model = "fixture-pro"
        fast_model = "fixture-fast"

        def __init__(self):
            self.calls = 0

        def structured_json(self, **kwargs):
            self.calls += 1
            assignment = {"action": "CREATE", "target": "N0", "reason": "One primary method"}
            result = {"assignments": {"A0": assignment}, "new_entities": [{"ref": "N0", "title": "Method", "domain": "test", "identity_key": "method", "description": "Supported method"}]}
            if self.calls == 1:
                result["assignments"] = [{"atomic": "A0", **assignment}, {"atomic": "A0", **assignment}]
            else:
                assert kwargs["user"]["corrections"]
            return result

    store = Store(tmp_path / "routing")
    try:
        agent = Agent()
        plan, review, _ = decide(Calls(store, agent, policy="structured-check-v2", review_policy="essential-v1"),
            {"atomics": [{"ref": "A0"}]}, cards()[:1], [])
        assert agent.calls == 2 and review.passed and not plan._unresolved_atomics
    finally:
        store.close()


def test_verified_visual_cache_requires_source_prompt_image_and_exact_request_context(tmp_path):
    class Provider:
        model_name = "fixture"

        def __init__(self):
            self.calls = 0

        def analyze(self, *args, **kwargs):
            self.calls += 1
            return "FRESH"

    path = tmp_path / "page.png"
    path.write_bytes(b"image bytes")
    context = {"page": 1, "extraction_mode": "SOURCE_PAGE", "task": "Read this actual page"}
    value = VisualUnderstanding(external_block_id="page-1", image_sha256=sha(path.read_bytes()), model="fixture", prompt_version="source-page-evidence-v1", confidence=1)
    prompt = files("noodle_agent").joinpath("prompts", "source_page_evidence.v1.md").read_text(encoding="utf-8")
    manifest = {"source_sha256": "source", "model": "fixture", "prompt_sha256": sha(prompt), "records": [{
        "page": 1, "image_sha256": value.image_sha256, "input_sha256": VerifiedPageCache.input_hash("page-1", context),
        "value": value.model_dump(mode="json"), "origin_root": "old", "origin_ref": "receipt", "historical_tokens": 10, "historical_ms": 20}]}
    provider = Provider()
    hits = []
    cache = VerifiedPageCache(provider, manifest, source_sha256="source", emit=hits.append)
    assert cache.analyze(path, external_block_id="page-1", context=context).model == "fixture" and len(hits) == 1
    assert cache.analyze(path, external_block_id="page-1", context={**context, "task": "Different instruction"}) == "FRESH"
    assert provider.calls == 1
    with pytest.raises(QualityFailure, match="source/model/prompt"):
        VerifiedPageCache(provider, manifest, source_sha256="different")


def test_actual_successful_page_cache_receipts_use_provider_batch_image_hashes():
    from noodle_agent.config import Settings

    repo = Path(__file__).resolve().parents[3]
    root = repo / ".local-only/week4-throughput-20261023T140703Z"
    if not root.exists():
        pytest.skip("Optional immutable visual cache fixture")
    settings = Settings.from_env(repo)
    manifest = verified_page_manifest([root], "fbd97cb2103a49ad292d5b6534d4940bb209fc2745d9663623697ea224f0b4bd", settings)
    if manifest["rejected_roots"]:
        pytest.skip("Local provider configuration changed")
    assert len(manifest["records"]) == 27
