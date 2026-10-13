from __future__ import annotations

import pytest

from noodle_agent.synthesis.merge_content import combine_blocks, merge_scope, remap_references


def test_identical_blocks_retain_provenance_and_remap_keys():
    target = [
        {
            "block_key": "a",
            "type": "PROSE",
            "markdown": "Same fact",
            "evidence_refs": ["target"],
            "note_block_ids": ["n1"],
        }
    ]
    source = [
        {
            "block_key": "b",
            "type": "PROSE",
            "markdown": "Same fact",
            "evidence_refs": ["old"],
            "note_block_ids": ["n2"],
        },
        {"block_key": "a", "type": "PROSE", "markdown": "Distinct", "evidence_refs": ["old"]},
    ]
    result, mapping, removed = combine_blocks(target, remap_references(source, {"old": "new"}))
    assert removed == 1 and len(result) == 2
    assert result[0]["evidence_refs"] == ["target", "new"]
    assert result[0]["note_block_ids"] == ["n1", "n2"]
    assert mapping["b"] == "a" and mapping["a"] != "a"
    assert source[0]["evidence_refs"] == ["old"]
    assert target[0]["evidence_refs"] == ["target"]


def test_scope_union_and_conflict_rejection():
    result = merge_scope(
        {"includes": ["A"], "prerequisites": ["P"]},
        {"includes": ["A", "B"], "prerequisites": ["Q"], "excludes": ["C"]},
        ["Alias"],
    )
    assert result["includes"] == ["A", "B"]
    assert result["prerequisites"] == ["P", "Q"]
    with pytest.raises(ValueError, match="Scope boundary conflict"):
        merge_scope({"includes": ["A"]}, {"excludes": ["A"]}, [])


def test_formula_whitespace_and_different_claims_not_fuzzy_deduped():
    blocks, _, removed = combine_blocks(
        [{"block_key": "a", "type": "FORMULA", "latex": "x + y"}],
        [{"block_key": "b", "type": "FORMULA", "latex": "x+y"}],
    )
    assert len(blocks) == 2 and removed == 0
