"""New throughput paths retain review gates and independent stage dependencies."""

import json
import threading
from types import SimpleNamespace

from test_next_source_sections import Agent, Parser, prepared  # noqa: F401

from noodle_agent.next.assembly_generation import page_materials
from noodle_agent.next.concepts.entity_patch import write_entity_sections
from noodle_agent.next.generation import Generation
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.maintenance_pool import MaintenancePool
from noodle_agent.next.page_evidence import PageEvidence


def test_real_policy_path_checks_before_publication_and_reuses_receipts(prepared):  # noqa: F811
    store, lease, _source = prepared

    class MetadataAgent(Agent):
        def structured_json(self, **kwargs):
            result = super().structured_json(**kwargs)
            if kwargs["operation"] == "next.section_write":
                result.update(summary="Source supported explanation", entity_hints={"subject": result["title"]})
            return result

    agent = MetadataAgent()
    profile = {"parser": Parser.name, "generation_layout": "source-sections-v1", "concept_layout": "entity-v2",
               "execution_policy": "pipeline-throughput-v1", "checking_policy": "single-check-v1"}
    result = Generation(store, Parser(), agent, lease, profile=profile, concurrency=3).run()
    assert len(result["notes"]) == 2
    assert agent.operations.count("next.section_write") == 2
    assert agent.operations.count("next.assembly_review") == 1
    before = list(agent.operations)
    again = Generation(store, Parser(), agent, lease, profile=profile, concurrency=3).run()
    assert again["notes"] == result["notes"] and agent.operations == before
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM events WHERE event_type='ATOMIC_BATCH_CLOSED'").scalar_one() == 1


def test_page_dependency_does_not_wait_for_unrelated_later_page(monkeypatch):
    later_started, release = threading.Event(), threading.Event()

    def contexts(*args, selected_pages, **kwargs):
        page = selected_pages[0]
        if page == 2:
            later_started.set()
            assert release.wait(5)
        return [{"page": page}]

    monkeypatch.setattr("noodle_agent.next.page_evidence.page_contexts", contexts)
    monkeypatch.setenv("NOODLE_NEXT_VISION_CONCURRENCY", "2")
    units = [Unit(unit_id=f"u{p}", block_id=f"b{p}", page=p, start=0, end=5,
                  text="image", kind="IMAGE", asset_ref=f"assets/{p}.png") for p in (1, 2)]
    generation = SimpleNamespace(store=None, journal=None, parsing=SimpleNamespace(vision=None),
                                 lease=SimpleNamespace(input={"revision_id": "r"}), progress=lambda *_: None)
    with PageEvidence(generation, units) as evidence:
        try:
            evidence.prefetch(units)
            assert later_started.wait(5)
            assert evidence.for_units(units[:1]) == [{"page": 1}]
        finally:
            release.set()


def test_maintenance_lanes_allow_index_and_concept_to_overlap():
    index_started, concept_started = threading.Event(), threading.Event()
    stop = threading.Event()

    class Consumer:
        def __init__(self, kinds):
            self.kinds = kinds

        def run_once(self, _worker):
            if "BUILD_INDEX" in self.kinds:
                index_started.set()
                assert concept_started.wait(5)
            elif "BUILD_CONCEPT" in self.kinds:
                concept_started.set()
                assert index_started.wait(5)
            stop.wait(.01)

    pool = MaintenancePool(None, stop=stop, factory=Consumer).start()
    try:
        assert index_started.wait(5) and concept_started.wait(5)
    finally:
        pool.close()
    assert not pool.errors and not any(t.is_alive() for t in pool.threads)


def test_entity_patch_repairs_only_failed_changed_chapter():
    class Calls:
        def __init__(self):
            self.writes = []

        def begin_unit(self, *_args, **_kwargs):
            pass

        def run(self, operation, instruction, content, contract, check):
            specs = content["chapters"]
            if operation == "next.entity_review":
                raw = {"chapters": {s["key"]: {"passed": len(self.writes) > 1 or s["key"] == "a",
                    "issues": [] if len(self.writes) > 1 or s["key"] == "a" else ["Missing condition"],
                    "supported_fragment_ids": [f["fragment_id"] for f in s["fragments"]],
                    "checked_image_ids": []} for s in specs}}
            else:
                self.writes.append([s["key"] for s in specs])
                raw = {"chapters": {s["key"]: {"markdown": "Source explanation with its condition."} for s in specs}}
            value = contract.model_validate(raw)
            check(value)
            return value

    calls = Calls()
    specs = [{"key": key, "title": key, "question": "Explain", "scope": {"includes": ["Explain"]},
              "fragments": [{"fragment_id": key, "sha256": key, "text": "Source explanation.", "evidence_ids": []}]}
             for key in ("a", "b")]
    result = write_entity_sections(None, calls, specs)
    assert set(result) == {"a", "b"} and calls.writes == [["a", "b"], ["b"]]


def test_compact_page_material_preserves_facts_and_uncertainty():
    material = {"ocr_text": "Full code and formula", "confidence": .7, "factual_observations": ["Observed label"],
                "spatial_relations": ["Arrow points right"], "request_id": "transport-only"}
    result = page_materials([{"page": 3, "asset_ref": "assets/page.png", "text": json.dumps(material)}])
    assert result[0]["material"] == {k: v for k, v in material.items() if k != "request_id"}
