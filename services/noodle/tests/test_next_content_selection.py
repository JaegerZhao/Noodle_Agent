"""Selection gates cover metadata leakage, substantive prefaces and durable exclusions."""

import fitz
from test_next_generation import Agent, Parser

from noodle_agent.domain import ParsedBlock, ParsedDocument
from noodle_agent.next.content_selection import filter_layout, section_contexts
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.store import Store
from noodle_agent.next.verification import verify_generation
from noodle_agent.next.worker import GenerationWorker


class MixedParser(Parser):
    def download_and_normalize(self, *args):
        return ParsedDocument(
            title="Mixed preface",
            page_count=2,
            markdown="",
            parser_name=self.name,
            parser_version="test-v1",
            blocks=[
                ParsedBlock(
                    block_id="heading", page_index=0, page_label="1", kind="HEADING", text="Preface"
                ),
                ParsedBlock(
                    block_id="copyright",
                    page_index=0,
                    page_label="1",
                    text="Copyright 2025. ISBN 123. All rights reserved.",
                ),
                ParsedBlock(
                    block_id="thanks",
                    page_index=0,
                    page_label="1",
                    text="Acknowledgements: thank Alice and Bob for proofreading.",
                ),
                ParsedBlock(
                    block_id="knowledge",
                    page_index=0,
                    page_label="1",
                    text="梯度下降沿负梯度方向更新权重，学习率控制步长。",
                ),
                ParsedBlock(
                    block_id="header",
                    page_index=1,
                    page_label="2",
                    kind="HEADER",
                    text="Book title",
                ),
                ParsedBlock(
                    block_id="reference",
                    page_index=1,
                    page_label="2",
                    text="References: Author. Book title. Publisher, 2024.",
                ),
                ParsedBlock(
                    block_id="toc",
                    page_index=1,
                    page_label="2",
                    text="Contents: 1 Introduction 1; 2 Optimization 15",
                ),
                ParsedBlock(
                    block_id="resource",
                    page_index=1,
                    page_label="2",
                    text="Code and notebook links for this edition: example.org/code",
                ),
            ],
        )


class SelectingAgent(Agent):
    def __init__(self, leak_once=False):
        super().__init__()
        self.leak_once = leak_once
        self.allocations = 0
        self.written = []

    def structured_json(self, *, operation, user, **kwargs):
        data = user["content"]
        if operation == "next.plan":
            assert "先判断内容是否值得写入" in kwargs["system"]
            assert "前言" in kwargs["system"]
            self.allocations += 1
            if self.leak_once and self.allocations == 1:
                return super().structured_json(operation=operation, user=user, **kwargs)
            unit = next(u for u in data["units"] if u["block_id"] == "knowledge")
            return {
                "topics": [
                    {
                        "key": "gradient",
                        "title": "梯度下降",
                        "question": "如何更新？",
                        "scope": {"includes": ["梯度下降"]},
                    }
                ],
                "assignments": {
                    u["unit_id"]: (
                        {"mode": "TOPIC", "topic_key": "gradient"}
                        if u is unit
                        else {
                            "mode": "NAVIGATION"
                            if u["block_id"] in {"heading", "reference", "toc"}
                            else "ADMINISTRATIVE",
                            "reason": "仅提供资料导航或出版/致谢/资源信息，不含独立教学解释。",
                        }
                    )
                    for u in data["units"]
                },
            }
        if operation == "next.plan_review" and self.leak_once and self.allocations == 1:
            return {
                "passed": False,
                "issues": ["版权和致谢被分配成主题"],
                "supported_units": [],
                "reason": "应排除纯资料信息。",
            }
        if operation == "next.write":
            self.written.extend(u["block_id"] for u in data["units"])
        return super().structured_json(operation=operation, user=user, **kwargs)


def test_metadata_is_screened_before_writing_but_knowledge_in_preface_survives(tmp_path):
    store = Store(tmp_path / "library")
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        store.add_source(pdf.tobytes(), "Mixed preface")
    agent = SelectingAgent(leak_once=True)
    result = GenerationWorker(store, parser=MixedParser(), agent=agent).run_once()
    assert result["status"] == "SUCCEEDED" and result["note_count"] == 1
    assert agent.allocations == 2  # independent gate rejected a permissive planner
    assert agent.written == ["knowledge"]
    report = verify_generation(store, result["job_id"])
    assert report["included_units"] == 1 and report["excluded_units"] == 7
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "select count(*) from job_checkpoints where stage='selection.window.0'"
            ).scalar_one()
            == 1
        )
        assert c.exec_driver_sql("select count(*) from notes where kind='ATOMIC'").scalar_one() == 1
    assert "ISBN" not in store.get_note(result["source_map"]["note_id"])["markdown"]
    store.close()


def test_layout_filter_is_narrow_and_does_not_drop_appendices_footnotes_or_mixed_text():
    kinds = ["HEADER", "FOOTER", "PAGE_NUMBER", "PAGE_FOOTNOTE", "HEADING", "TEXT", "IMAGE"]
    units = [
        Unit(
            unit_id=f"u{i}",
            block_id=f"b{i}",
            page=1,
            start=0,
            end=10,
            text="Appendix: copyright detection algorithm",
            kind=kind,
        )
        for i, kind in enumerate(kinds)
    ]
    kept, excluded = filter_layout(units)
    assert [u.kind for u in kept] == kinds[3:]
    assert len(excluded) == 3 and all(e.category == "NAVIGATION" for e in excluded)
    assert section_contexts(units)["u5"] == units[4].text


def test_redundant_writing_units_require_exact_evidence_agreement():
    import pytest

    from noodle_agent.next.generation_contracts import canonical_writing_response

    raw = {
        "assignments": {"u1": "s"},
        "sections": [{"key": "s", "units": ["u1"], "markdown": "body"}],
    }
    assert "units" not in canonical_writing_response(raw)["sections"][0]
    raw["sections"][0]["units"] = ["u2"]
    with pytest.raises(ValueError, match="conflict"):
        canonical_writing_response(raw)


def test_nested_writing_assignments_cannot_overwrite_required_mapping():
    import pytest

    from noodle_agent.next.generation_contracts import canonical_writing_response

    raw = {"assignments": {"u1": "s"}, "sections": [{"key": "s", "assignments": {}}]}
    assert canonical_writing_response(raw)["sections"] == [{"key": "s"}]
    raw["sections"][0]["assignments"] = {"u1": "wrong"}
    with pytest.raises(ValueError, match="conflict"):
        canonical_writing_response(raw)


def test_literal_nlp_tokens_inside_escaped_code_are_not_raw_html():
    from noodle_agent.next.generation import Generation
    from noodle_agent.next.generation_contracts import Draft

    for body in ["未知词用 `<UNK>` 表示。", "样本文本：`robert <UNK>`。"]:
        draft = Draft(
            title="NLP",
            summary="Tokens",
            sections=[
                {
                    "key": "tokens",
                    "title": "特殊词元",
                    "question": "如何表示未知词？",
                    "scope": {"includes": ["词元"]},
                    "markdown": body,
                    "units": ["u0"],
                }
            ],
        )
        Generation.validate_draft(draft, {"u0"}, set())


def test_malformed_code_delimiters_cannot_hide_raw_html():
    import pytest

    from noodle_agent.next.generation import Generation
    from noodle_agent.next.generation_contracts import Draft
    from noodle_agent.next.planning import QualityFailure

    for body in ["``<script>bad</script>`", "`<script>bad</script>``", "\\`<script>bad</script>`"]:
        draft = Draft(
            title="t",
            summary="s",
            sections=[
                {
                    "key": "s",
                    "title": "t",
                    "question": "q",
                    "scope": {"includes": ["x"]},
                    "markdown": body,
                    "units": ["u0"],
                }
            ],
        )
        with pytest.raises(QualityFailure, match="raw HTML"):
            Generation.validate_draft(draft, {"u0"}, set())
