import fitz
import pytest

from noodle_agent.domain import ParsedBlock, ParsedDocument
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation_contracts import Unit
from noodle_agent.next.large_documents import chapter_ranges, planning_windows
from noodle_agent.next.parsing import ParsePending, PdfParsing
from noodle_agent.next.store import Store


class Parser:
    name = "part-test"

    def __init__(self):
        self.parts = {}
        self.ready = False

    def submit(self, path, identity):
        with fitz.open(path) as pdf:
            self.parts[identity] = len(pdf)
        assert self.parts[identity] <= 180
        return {"external_id": identity, "state": "pending"}

    def poll(self, identity):
        return {"state": "done" if self.ready else "running"}

    def download_and_normalize(self, identity, payload, destination, title):
        return ParsedDocument(
            title=title,
            page_count=self.parts[identity],
            markdown="",
            parser_name=self.name,
            parser_version="test-v1",
            blocks=[
                ParsedBlock(block_id=f"b{i}", page_index=i, page_label=str(i + 1), text=f"page {i}")
                for i in range(self.parts[identity])
            ],
        )


def test_partition_receipts_preserve_original_pages_and_single_source(tmp_path):
    with fitz.open() as pdf:
        for i in range(181):
            pdf.new_page().insert_text((20, 30), str(i))
        pdf.set_toc([[1, "1 First", 1], [1, "2 Second", 100]])
        original = pdf.tobytes()
    with pytest.raises(fitz.FileDataError):
        chapter_ranges(b"invalid")
    store = Store(tmp_path)
    source = store.add_source(original, "book")
    dispatch(store)
    lease = store.jobs.claim("test", kind="GENERATE_ATOMICS")
    parser = Parser()
    parsing = PdfParsing(store, parser, Checkpoints(store.repository, lease))
    with pytest.raises(ParsePending):
        parsing.run(source["revision_id"])
    assert len(parser.parts) == 2
    parser.ready = True
    result = parsing.run(source["revision_id"])
    assert len(parser.parts) == 2
    assert [b["page"] for b in result["artifact"]["blocks"]] == list(range(1, 182))
    assert len({b["id"] for b in result["artifact"]["blocks"]}) == 181
    assert result["artifact"]["chapters"][1]["start"] == 100
    units = [
        Unit(unit_id=f"u{i}", block_id=f"b{i}", page=i, start=0, end=1, text="x", kind="TEXT")
        for i in range(1, 182)
    ]
    windows = planning_windows(units, result["artifact"])
    assert sum(map(len, windows)) == 181
    assert all(not (min(u.page for u in w) < 100 <= max(u.page for u in w)) for w in windows)
    assert parsing.run(source["revision_id"]) == result
    store.close()


def test_textbook_chapters_publish_incrementally_and_readback_has_global_units(tmp_path):
    from test_next_generation import Agent

    from noodle_agent.next.verification import verify_generation
    from noodle_agent.next.worker import GenerationWorker

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
                        block_id="first",
                        page_index=0,
                        page_label="1",
                        text="梯度下降沿负梯度方向更新权重。",
                    )
                ],
            )

    with fitz.open() as pdf:
        for i in range(181):
            page = pdf.new_page()
            if i in {0, 180}:
                page.insert_text((20, 30), "Gradient descent")
        pdf.set_toc([[1, "1 First", 1], [1, "2 Second", 181]])
        original = pdf.tobytes()
    store = Store(tmp_path)
    store.add_source(original, "Textbook")
    parser = PointParser()
    parser.ready = True

    def crash(stage, info):
        if stage == "chapter-completed" and info["completed"] == 2:
            raise ConnectionError("simulated worker loss after map commit")

    worker = GenerationWorker(store, parser=parser, agent=Agent(), progress=crash)
    interrupted = worker.run_once()
    assert interrupted["status"] == "RETRY"
    with store.db.engine.connect() as c:
        maps_before = c.exec_driver_sql(
            "SELECT count(*) FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE n.kind='MOC'"
        ).scalar_one()
    worker.progress = lambda *_: None
    result = worker.run_once()
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE n.kind='MOC'"
            ).scalar_one()
            == maps_before
        )
    assert result["status"] == "SUCCEEDED" and result["note_count"] == 2
    report = verify_generation(store, result["job_id"])
    assert report["source_units"] == 2 and report["included_units"] == 2
    moc = store.get_note(result["source_map"]["note_id"])
    assert len(moc["map_entries"]) == 4
    assert "尚未完成全书" not in moc["markdown"]
    with store.db.engine.connect() as c:
        assert c.exec_driver_sql("select count(*) from sources where kind='PDF'").scalar_one() == 1
        assert c.exec_driver_sql("select count(*) from notes where kind='MOC'").scalar_one() == 1
    store.close()


def test_textbook_metadata_only_frontmatter_is_excluded_with_complete_coverage(tmp_path):
    from test_next_generation import Agent

    from noodle_agent.next.verification import verify_generation
    from noodle_agent.next.worker import GenerationWorker

    class FrontParser(Parser):
        def download_and_normalize(self, identity, payload, destination, title):
            first = identity.endswith("part-0")
            blocks = [
                ParsedBlock(
                    block_id="knowledge",
                    page_index=1 if first else 0,
                    page_label="2" if first else "1",
                    text="梯度下降沿负梯度方向更新权重。",
                )
            ]
            if first:
                blocks.insert(
                    0,
                    ParsedBlock(
                        block_id="copyright",
                        page_index=0,
                        page_label="1",
                        text="Copyright 2025. All rights reserved. ISBN 123.",
                    ),
                )
            return ParsedDocument(
                title=title,
                page_count=self.parts[identity],
                markdown="",
                parser_name=self.name,
                parser_version="test-v1",
                blocks=blocks,
            )

    class FrontAgent(Agent):
        def structured_json(self, *, operation, user, **kwargs):
            data = user["content"]
            if operation == "next.plan" and all(
                "copyright" in u["block_id"] for u in data["units"]
            ):
                return {
                    "topics": [],
                    "assignments": {
                        u["unit_id"]: {
                            "mode": "ADMINISTRATIVE",
                            "reason": "纯版权页，不生成知识笔记。",
                        }
                        for u in data["units"]
                    },
                }
            return super().structured_json(operation=operation, user=user, **kwargs)

    with fitz.open() as pdf:
        for i in range(181):
            page = pdf.new_page()
            if i in {0, 1, 180}:
                page.insert_text((20, 30), "Copyright" if i == 0 else "Gradient descent")
        pdf.set_toc([[1, "1 First", 2], [1, "2 Second", 181]])
        original = pdf.tobytes()
    store = Store(tmp_path)
    store.add_source(original, "Textbook")
    parser = FrontParser()
    parser.ready = True
    agent = FrontAgent()
    worker = GenerationWorker(store, parser=parser, agent=agent)
    result = worker.run_once()
    assert result["status"] == "SUCCEEDED" and result["note_count"] == 2
    report = verify_generation(store, result["job_id"])
    assert (
        report["source_units"] == 3
        and report["excluded_units"] == 1
        and report["included_units"] == 2
    )
    moc = store.get_note(result["source_map"]["note_id"])
    assert "Copyright" not in moc["markdown"]
    with store.db.engine.connect() as c:
        assert (
            c.exec_driver_sql(
                "select count(*) from notes where creation_key like '%:chapter0.%'"
            ).scalar_one()
            == 0
        )
    store.close()
