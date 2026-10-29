"""Generic stage/repair invariants use injected fixtures and no provider network."""

import pytest

from noodle_agent.next.contracts import sha
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repair_protocol import check_patch_scope, check_progress, repair_packet
from noodle_agent.next.section_contracts import DraftPatch
from noodle_agent.next.visual_replay import ReplayVision


def test_repair_packet_limits_prose_and_source_pages():
    record = {
        "draft": {
            "title": "t",
            "summary": "s",
            "sections": [
                {"key": "a", "title": "A", "markdown": "wrong"},
                {"key": "b", "title": "B", "markdown": "keep"},
            ],
            "assignments": {"u1": "a", "u2": "b"},
            "omissions": {},
        },
        "inputs": {"units": [{"unit_id": "u1", "page": 1}, {"unit_id": "u2", "page": 2}]},
        "contexts": [{"page": 1, "text": "one"}, {"page": 2, "text": "two"}],
    }
    packet = repair_packet(record, [{"section_key": "a", "issue": "incorrect"}])
    assert [s["key"] for s in packet["draft"]["sections"]] == ["a"]
    assert [s["unit_id"] for s in packet["source_units"]] == ["u1"]
    assert [s["page"] for s in packet["page_contexts"]] == [1]
    with pytest.raises(QualityFailure, match="unrelated"):
        check_patch_scope(packet, DraftPatch(sections=[{"key": "b", "markdown": "changed"}]))
    with pytest.raises(QualityFailure, match="NO_PROGRESS"):
        check_progress(record["draft"], dict(record["draft"]))


def test_frozen_vision_refuses_misses_and_wrong_purpose(tmp_path):
    p = tmp_path / "page.png"
    p.write_bytes(b"page")
    model = "frozen-model"
    digest = sha(b"page")
    value = {"external_block_id": "page-1", "image_sha256": digest, "model": model}
    replay = ReplayVision(
        {
            "source_sha256": "source",
            "model": model,
            "records": [{"kind": "SOURCE_PAGE", "page": 1, "image_sha256": digest, "value": value}],
        }
    )
    assert (
        replay.analyze(
            p, external_block_id="page-1", context={"page": 1, "extraction_mode": "SOURCE_PAGE"}
        ).image_sha256
        == digest
    )
    with pytest.raises(QualityFailure, match="CACHE_MISS"):
        replay.analyze(
            p, external_block_id="page-1", context={"page": 1, "extraction_mode": "RELATIONS"}
        )
    assert replay.network_calls == 0 and replay.hits == 1
