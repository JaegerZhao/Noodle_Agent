from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf as fitz
from fastapi.testclient import TestClient
from sqlalchemy import select

from noodle_agent.agent import AgentHarness
from noodle_agent.api import create_app
from noodle_agent.config import Settings
from noodle_agent.db import (
    AtomicNote,
    BlobObject,
    Job,
    NoteCandidate,
    NoteEmbedding,
    NoteVersion,
    Source,
    SourceNoteVersion,
    SourceVersion,
    utcnow,
)
from noodle_agent.domain import (
    ConceptCoverage,
    JobStage,
    KnowledgeConcept,
    KnowledgeMap,
    KnowledgePlan,
    NoteBlockDraft,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedBlock,
    ParsedDocument,
)
from noodle_agent.services import NoodleService, _merged_event_duration_ms
from noodle_agent.worker import JobWorker


def create_fixture(directory: Path) -> Path:
    pdf_path = directory / "course.pdf"
    pdf = fitz.open()
    for index in range(2):
        page = pdf.new_page()
        page.insert_text((72, 72), f"Course page {index + 1}")
    pdf.save(pdf_path)
    pdf.close()
    (directory / "figure.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
    )
    pdf_path.with_suffix(".mineru.json").write_text(
        json.dumps(
            {
                "title": "优化方法课程",
                "pages": [
                    {
                        "blocks": [
                            {"id": "h1", "type": "heading", "level": 1, "text": "梯度下降"},
                            {
                                "id": "p1",
                                "type": "text",
                                "text": "梯度下降沿负梯度方向更新参数，从而减小损失函数。",
                            },
                        ]
                    },
                    {
                        "blocks": [
                            {"id": "h2", "type": "heading", "level": 1, "text": "学习率"},
                            {
                                "id": "p2",
                                "type": "text",
                                "text": "学习率过大可能发散，过小会导致收敛缓慢。",
                            },
                            {
                                "id": "img2",
                                "type": "chart",
                                "text": "不同学习率下的损失曲线",
                                "asset_path": "figure.png",
                            },
                        ]
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pdf_path


@contextmanager
def isolated_settings(directory: Path):
    values = {
        "NOODLE_DATA_DIR": str(directory / "data"),
        "NOODLE_PARSER": "fixture",
        "NOODLE_LLM": "fixture",
        "NOODLE_SKILLS_DIR": str(Path(__file__).resolve().parents[1] / "skills"),
    }
    with patch.dict(os.environ, values, clear=False):
        yield Settings.from_env(directory)


class MvpTest(unittest.TestCase):
    def test_headerless_rectangular_matrix_gets_lossless_markdown_headers(self) -> None:
        plan = KnowledgePlan(
            document_summary="pooling",
            notes=[
                NoteDraft(
                    stable_key="pooling",
                    title="池化",
                    thesis="最大池化示例",
                    blocks=[
                        NoteBlockDraft(
                            block_type="TABLE",
                            table_rows=[["1", "2"], ["3", "4"]],
                            evidence_block_ids=["b1"],
                        )
                    ],
                )
            ],
        )

        normalized, repairs = NoodleService._normalize_table_shapes(plan)

        self.assertEqual(plan.notes[0].blocks[0].table_headers, [])
        self.assertEqual(normalized.notes[0].blocks[0].table_headers, ["", ""])
        self.assertEqual(
            normalized.notes[0].blocks[0].table_rows,
            [["1", "2"], ["3", "4"]],
        )
        self.assertEqual(repairs[0]["type"], "ADD_EMPTY_PRESENTATION_HEADERS")

    def test_model_wall_duration_merges_parallel_calls(self) -> None:
        started_at = utcnow()
        events = [
            SimpleNamespace(started_at=started_at, duration_ms=10_000),
            SimpleNamespace(started_at=started_at + timedelta(seconds=5), duration_ms=10_000),
            SimpleNamespace(started_at=started_at + timedelta(seconds=20), duration_ms=2_000),
        ]
        self.assertEqual(_merged_event_duration_ms(events), 17_000)

    def test_failed_preparse_job_retries_from_preserved_original(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                with service.db.session() as session:
                    failed = session.get(Job, ingest["job_id"])
                    failed.status = "FAILED"
                    failed.stage = "SUBMITTING_PARSE"
                    failed.error_code = "FixtureFailure"

                retried = service.resume_job(ingest["job_id"])
                self.assertEqual(retried["status"], "CREATED")
                self.assertEqual(retried["retry_mode"], "from_original")
                self.assertEqual(retried["replaces_job_id"], ingest["job_id"])
                self.assertNotEqual(retried["id"], ingest["job_id"])
                service.close()

    def test_delete_source_retains_history_but_removes_active_reads(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                completed = service.run_job_until_terminal(
                    ingest["job_id"], timeout_seconds=10
                )
                self.assertEqual(completed["status"], "COMPLETED")
                with service.db.session() as session:
                    version = session.get(SourceVersion, ingest["source_version_id"])
                    original_blob = session.get(BlobObject, version.original_blob_id)
                    original_path = service.blobs.path_for(original_blob.storage_key)
                    artifact_path = settings.artifact_dir / completed["parse_run_id"]
                export_paths = list(
                    settings.export_dir.glob(f"*--{ingest['source_version_id'][:8]}--*")
                )
                log_path = settings.data_dir / "logs" / "noodle.jsonl"
                self.assertTrue(original_path.is_file())
                self.assertTrue(artifact_path.is_dir())
                self.assertTrue(export_paths)
                self.assertIn(ingest["job_id"], log_path.read_text(encoding="utf-8"))
                self.assertTrue(service.list_notes())
                self.assertTrue(service.list_source_notes())

                deleted = service.delete_source_local_data(ingest["source_id"])

                self.assertEqual(deleted["deletion_mode"], "LOGICAL")
                self.assertTrue(deleted["sync_event_id"])
                self.assertTrue(pdf_path.is_file())
                self.assertTrue(original_path.is_file())
                self.assertTrue(artifact_path.is_dir())
                self.assertEqual(service.list_notes(), [])
                self.assertEqual(service.list_source_notes(), [])
                with service.db.session() as session:
                    self.assertEqual(session.get(Source, ingest["source_id"]).visibility, "DELETED")
                    self.assertIsNotNone(session.get(SourceVersion, ingest["source_version_id"]))
                new_upload = service.ingest_pdf(pdf_path)
                self.assertNotEqual(new_upload["source_id"], ingest["source_id"])
                self.assertEqual(new_upload["deduplicated"], "false")
                service.delete_source_local_data(new_upload["source_id"])
                self.assertEqual(service.get_library_overview()["sources"], [])
                service.close()

    def test_delete_source_keeps_blob_until_last_shared_reference_is_removed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                first = service.ingest_pdf(pdf_path, force_new_version=True)
                second = service.ingest_pdf(pdf_path, force_new_version=True)
                with service.db.session() as session:
                    first_version = session.get(SourceVersion, first["source_version_id"])
                    second_version = session.get(SourceVersion, second["source_version_id"])
                    self.assertEqual(first_version.original_blob_id, second_version.original_blob_id)
                    blob = session.get(BlobObject, first_version.original_blob_id)
                    blob_path = service.blobs.path_for(blob.storage_key)
                    session.get(Job, first["job_id"]).status = "FAILED"
                    session.get(Job, second["job_id"]).status = "FAILED"

                service.delete_source_local_data(first["source_id"])
                self.assertTrue(blob_path.is_file())
                self.assertIsNotNone(
                    service.get_library_overview()["sources"][0]["source_id"]
                )

                service.delete_source_local_data(second["source_id"])
                self.assertTrue(blob_path.exists())
                self.assertEqual(service.get_library_overview()["sources"], [])
                self.assertTrue(pdf_path.is_file())
                service.close()

    def test_block_edit_is_versioned_indexed_and_conflict_safe(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                source_note_id = service.get_job_result(ingest["job_id"])[
                    "source_note_id"
                ]
                before_release = service.get_source_note(source_note_id)

                notes: list[dict] = []

                def collect(nodes: list[dict]) -> None:
                    for node in nodes:
                        notes.extend(node["notes"])
                        collect(node["children"])

                collect(before_release["outline"])
                target = next(
                    note
                    for note in notes
                    if any(block["type"] != "IMAGE" and block["text"] for block in note["blocks"])
                )
                block = next(
                    block
                    for block in target["blocks"]
                    if block["type"] != "IMAGE" and block["text"]
                )
                replacement = "**人工修订：** 梯度下降沿负梯度方向更新参数。"
                result = service.edit_note_block(
                    target["note_id"],
                    expected_version_id=target["version_id"],
                    block_key=block["block_key"],
                    new_text=replacement,
                )

                self.assertEqual(result["status"], "UPDATED")
                self.assertNotEqual(result["version_id"], target["version_id"])
                edited = service.get_note(target["note_id"])
                edited_block = next(
                    item for item in edited["blocks"] if item["block_key"] == block["block_key"]
                )
                self.assertEqual(edited_block["text"], replacement)
                self.assertEqual(edited_block["origin"], "HUMAN_REVISED")
                self.assertEqual(
                    service.get_source_note(source_note_id)["version_no"],
                    before_release["version_no"] + 1,
                )
                unchanged = service.edit_note_block(
                    target["note_id"],
                    expected_version_id=result["version_id"],
                    block_key=block["block_key"],
                    new_text=replacement,
                )
                self.assertEqual(unchanged["status"], "UNCHANGED")
                self.assertEqual(unchanged["version_id"], result["version_id"])
                with self.assertRaisesRegex(ValueError, "version conflict"):
                    service.edit_note_block(
                        target["note_id"],
                        expected_version_id=target["version_id"],
                        block_key=block["block_key"],
                        new_text="stale write",
                    )
                self.assertTrue(service.search("人工修订"))
                service.close()

    def test_replacing_captioned_images_retires_old_caption_blocks(self):
        from noodle_agent.db import NoteBlock
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                source = next(item for item in service.list_source_notes() if item["source_version_id"] == ingest["source_version_id"])
                with service.db.session() as session:
                    image_blocks = session.scalars(select(NoteBlock).where(NoteBlock.block_type == "IMAGE")).all()
                    for block in image_blocks:
                        block.content_text = "Obsolete crop description"
                attachment = service.create_agent_attachment(root / "figure.png", original_name="reviewed.png", media_type="image/png", analyze=False)
                result = service.replace_source_note_images(source_note_id=source["source_note_id"], attachment_id=attachment["attachment_id"], page_start=2, page_end=2, expected_source_note_version_id=source["version_id"], caption="Reviewed full-page labels", purpose="Preserve context")
                updated = service.get_note(result["target_note_id"])
                assert "Obsolete crop description" not in "\n".join(block["text"] for block in updated["blocks"])
                assert all(block["assets"] for block in updated["blocks"] if block["type"] == "IMAGE")
                second_attachment = service.create_agent_attachment(root / "figure.png", original_name="reviewed-again.png", media_type="image/png", analyze=False)
                second = service.replace_source_note_images(source_note_id=source["source_note_id"], attachment_id=second_attachment["attachment_id"], page_start=2, page_end=2, target_note_title=updated["title"], expected_source_note_version_id=result["source_note_version_id"], caption="Revised labels", purpose="Second revision")
                assert second["status"] == "UPDATED"
                assert len([b for b in service.get_note(second["target_note_id"])["blocks"] if b["type"] == "IMAGE"]) == 1
                service.close()

    def test_search_keeps_fts_results_when_embedding_model_changes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                with service.db.session() as session:
                    for embedding in session.scalars(select(NoteEmbedding)).all():
                        embedding.model_name = "previous-embedding-model"

                hits = service.search("梯度下降")

                self.assertTrue(hits)
                self.assertIn("梯度下降", hits[0].title)
                service.close()

    def test_agent_replaces_source_image_with_versioned_attachment(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                source_note_id = service.get_job_result(ingest["job_id"])[
                    "source_note_id"
                ]
                original = service.get_source_note(source_note_id)
                replacement = service.create_agent_attachment(
                    root / "figure.png",
                    original_name="complete-figure.png",
                    media_type="image/png",
                    analyze=False,
                )
                harness = AgentHarness(service, settings)
                result = harness.chat(
                    "将 course.pdf 的 P2-P2 图片碎片替换为这张完整图片",
                    attachment_ids=[replacement["attachment_id"]],
                    active_source_note_id=source_note_id,
                )
                self.assertEqual(result.skill, "note-editing")
                self.assertEqual(
                    result.tool_calls[0]["tool_name"], "note.replace_source_images"
                )
                self.assertEqual(result.tool_calls[0]["status"], "SUCCEEDED")
                edited = service.get_source_note(source_note_id)
                self.assertEqual(edited["version_no"], original["version_no"] + 1)
                with self.assertRaisesRegex(ValueError, "version conflict"):
                    service.replace_source_note_images(
                        source_note_id=source_note_id,
                        attachment_id=replacement["attachment_id"],
                        page_start=2,
                        page_end=2,
                        expected_source_note_version_id=original["version_id"],
                    )

                notes: list[dict] = []

                def collect(nodes: list[dict]) -> None:
                    for node in nodes:
                        notes.extend(node["notes"])
                        collect(node["children"])

                collect(edited["outline"])
                assets = [
                    asset
                    for note in notes
                    for block in note["blocks"]
                    for asset in block.get("assets", [])
                ]
                user_assets = [asset for asset in assets if asset["origin"] == "USER_UPLOAD"]
                self.assertEqual(len(user_assets), 1)
                self.assertEqual(user_assets[0]["source_pages"], ["2"])
                with service.db.session() as session:
                    source_versions = session.scalars(
                        select(SourceNoteVersion).where(
                            SourceNoteVersion.source_note_id == source_note_id
                        )
                    ).all()
                    self.assertEqual(len(source_versions), 2)
                harness.close()
                service.close()

    def test_worker_claims_job_once_and_exposes_source_note_result(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                worker = JobWorker(service, worker_id="worker-a")
                result = worker.run_once()
                self.assertIsNotNone(result)
                self.assertEqual(result["id"], ingest["job_id"])
                self.assertEqual(result["status"], "COMPLETED")
                job_result = service.get_job_result(ingest["job_id"])
                self.assertTrue(job_result["ready"])
                self.assertEqual(job_result["note_count"], 2)
                self.assertTrue(job_result["source_note_id"])
                with service.db.session() as session:
                    persisted = session.get(Job, ingest["job_id"])
                    self.assertIsNone(persisted.lease_owner)
                    self.assertIsNone(persisted.lease_expires_at)
                service.close()

    def test_worker_lease_prevents_duplicate_claim_and_can_expire(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                first = JobWorker(service, worker_id="worker-a")
                second = JobWorker(service, worker_id="worker-b")
                self.assertEqual(first.claim_next_job(), ingest["job_id"])
                self.assertIsNone(second.claim_next_job())
                with service.db.session() as session:
                    job = session.get(Job, ingest["job_id"])
                    job.lease_expires_at = utcnow() - timedelta(seconds=1)
                self.assertEqual(second.claim_next_job(), ingest["job_id"])
                second.release(ingest["job_id"])
                service.close()

    def test_parallel_stage_callbacks_do_not_regress_public_progress(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                service._update_generation_stage(
                    ingest["job_id"], JobStage.ANALYZING_VISUALS, 0.77
                )
                service._update_generation_stage(
                    ingest["job_id"], JobStage.DRAFTING_NOTES, 0.66
                )
                job = service.get_job(ingest["job_id"])
                self.assertEqual(job["progress"], 0.77)
                self.assertEqual(job["stage"], JobStage.ANALYZING_VISUALS.value)
                timeline = service.get_job_timeline(ingest["job_id"])
                progresses = [item["progress"] for item in timeline["stages"]]
                self.assertEqual(progresses, sorted(progresses))
                service.close()

    def test_end_to_end_pdf_notes_search_and_qa(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                result = service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                self.assertEqual(result["status"], "COMPLETED")
                timeline = service.get_job_timeline(ingest["job_id"])
                self.assertTrue(timeline["stages"])
                self.assertEqual(timeline["stages"][-1]["status"], "COMPLETED")
                self.assertTrue(timeline["job"]["created_at"].endswith("Z"))
                self.assertTrue(timeline["job"]["updated_at"].endswith("Z"))
                self.assertGreaterEqual(timeline["job"]["duration_ms"], 0)
                self.assertLessEqual(
                    timeline["summary"]["model_wall_duration_ms"],
                    timeline["summary"]["model_cumulative_duration_ms"],
                )
                library = service.get_library_overview()
                self.assertTrue(library["sources"][0]["updated_at"].endswith("Z"))
                self.assertTrue(library["sources"][0]["job"]["created_at"].endswith("Z"))
                self.assertTrue((settings.data_dir / "logs" / "noodle.jsonl").is_file())
                notes = service.list_notes()
                self.assertEqual(len(notes), 2)
                source_notes = service.list_source_notes()
                self.assertEqual(len(source_notes), 1)
                source_note = service.get_source_note(source_notes[0]["source_note_id"])
                self.assertEqual(source_note["unit_count"], 2)
                self.assertTrue(source_note["outline"])
                anchors = source_note["navigation_anchors"]
                self.assertTrue(anchors)
                self.assertTrue(all(item["page_index"] >= 0 for item in anchors))
                self.assertTrue(any(item["block_key"] for item in anchors))
                self.assertTrue(
                    all(
                        item["confidence"] in {"EXACT", "NOTE"}
                        for item in anchors
                    )
                )
                pending_nodes = list(source_note["outline"])
                projected_notes = []
                while pending_nodes:
                    node = pending_nodes.pop()
                    projected_notes.extend(node["notes"])
                    pending_nodes.extend(node["children"])
                self.assertTrue(
                    any(
                        block.get("evidence")
                        for note in projected_notes
                        for block in note["blocks"]
                    )
                )
                with service.db.session() as session:
                    hidden = session.get(AtomicNote, notes[0]["note_id"])
                    hidden.visibility = "HIDDEN"
                reconciled = service.reconcile_source_note_release(
                    source_notes[0]["source_note_id"]
                )
                self.assertEqual(reconciled["restored_note_count"], 1)
                self.assertEqual(len(service.list_notes()), 2)
                exports = service.export_markdown(ingest["source_version_id"])
                self.assertEqual(exports[0]["note_count"], 2)
                index_path = Path(exports[0]["index"])
                self.assertTrue(index_path.exists())
                self.assertTrue(Path(exports[0]["source_note"]).exists())
                source_note_markdown = Path(exports[0]["source_note"]).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn(f"\n## {source_note['title']}\n", source_note_markdown)
                self.assertNotIn("学习目标", source_note_markdown)
                self.assertNotIn("本节目标", source_note_markdown)
                self.assertNotIn("*溯源页：", source_note_markdown)
                self.assertRegex(source_note_markdown, r"\n## 1\.")
                exported_notes = [Path(path) for path in exports[0]["files"]]
                exported_texts = [path.read_text(encoding="utf-8") for path in exported_notes]
                self.assertTrue(all("## 来源与证据" in text for text in exported_texts))
                self.assertTrue(any("assets/" in text for text in exported_texts))
                assets_dir = exported_notes[0].parent / "assets"
                self.assertTrue(assets_dir.exists())
                self.assertTrue(any(assets_dir.iterdir()))
                hits = service.search("学习率", limit=3)
                self.assertTrue(hits)
                self.assertEqual(hits[0].title, "学习率")
                self.assertEqual(hits[0].evidence[0]["page_label"], "2")
                self.assertEqual(hits[0].assets[0]["page_label"], "2")
                answer = service.ask("学习率过大会怎样？")
                self.assertIn("发散", answer.answer)
                no_answer = service.ask("量子纠缠是否能实现超光速通信？")
                self.assertEqual(no_answer.hits, [])
                harness = AgentHarness(service, settings)
                agent_answer = harness.chat("学习率过大会怎样？")
                self.assertIn("发散", agent_answer.answer)
                self.assertEqual(agent_answer.skill, "evidence-grounded-qa")
                self.assertEqual(agent_answer.tool_calls[0]["tool_name"], "knowledge.answer")
                refused = harness.chat("量子纠缠是否能实现超光速通信？")
                self.assertEqual(refused.citations, [])
                persisted = harness.get_run(agent_answer.run_id)
                self.assertEqual(persisted["status"], "COMPLETED")
                self.assertEqual(persisted["context"]["skill"], "evidence-grounded-qa")
                follow_up = harness.chat(
                    "有哪些笔记？",
                    conversation_id=agent_answer.conversation_id,
                )
                self.assertEqual(follow_up.conversation_id, agent_answer.conversation_id)
                self.assertEqual(follow_up.tool_calls[0]["tool_name"], "note.list")
                conversation = harness.get_conversation(agent_answer.conversation_id)
                self.assertEqual(
                    [item["role"] for item in conversation["messages"]],
                    ["user", "assistant", "user", "assistant"],
                )
                harness.close()
                service.close()

    def test_structured_blocks_and_inline_image_are_persisted_and_exported(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()

                class StructuredAgent:
                    name = "structured-test"

                    def build_plan(self, _document):
                        return KnowledgePlan(
                            document_summary="学习率选择",
                            notes=[
                                NoteDraft(
                                    stable_key="learning-rate-v4",
                                    title="如何判断学习率是否合适",
                                    thesis="学习率需要在收敛速度与稳定性之间权衡。",
                                    blocks=[
                                        NoteBlockDraft(
                                            block_type="PARAGRAPH",
                                            text="学习率控制每次参数更新的步幅。",
                                            evidence_block_ids=["p2"],
                                        ),
                                        NoteBlockDraft(
                                            block_type="BULLETED_LIST",
                                            items=["过大：可能发散", "过小：收敛缓慢"],
                                            evidence_block_ids=["p2"],
                                        ),
                                        NoteBlockDraft(
                                            block_type="TABLE",
                                            table_headers=["现象", "判断"],
                                            table_rows=[
                                                ["损失震荡", "步幅可能过大"],
                                                [r"范数 $\|w\|^2$", "公式竖线不应破坏表格"],
                                            ],
                                            evidence_block_ids=["p2"],
                                        ),
                                        NoteBlockDraft(
                                            block_type="IMAGE",
                                            caption="比较不同学习率对应的损失曲线。",
                                            purpose="在判断规则之后给出视觉对照。",
                                            evidence_block_ids=["p2"],
                                            asset_block_ids=["img2"],
                                        ),
                                    ],
                                    tags=["学习率"],
                                    source_sections=["学习率"],
                                )
                            ],
                        )

                    def evaluate(self, _document, plan):
                        return [NoteEvaluation(stable_key=plan.notes[0].stable_key, passed=True)]

                service.agent = StructuredAgent()
                ingest = service.ingest_pdf(pdf_path)
                result = service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                self.assertEqual(result["status"], "COMPLETED")
                note = service.get_note(service.list_notes()[0]["note_id"])
                self.assertEqual(
                    [block["type"] for block in note["blocks"]],
                    ["PARAGRAPH", "BULLETED_LIST", "TABLE", "IMAGE"],
                )
                self.assertEqual(note["blocks"][-1]["assets"][0]["page_label"], "2")
                self.assertFalse(note["assets"])
                exports = service.export_markdown(ingest["source_version_id"])
                markdown = Path(exports[0]["files"][0]).read_text(encoding="utf-8")
                self.assertIn("- 过大：可能发散", markdown)
                self.assertIn("| 现象 | 判断 |", markdown)
                self.assertIn(r"$\Vert{}w\Vert{}^2$", markdown)
                self.assertIn("比较不同学习率对应的损失曲线", markdown)
                self.assertNotIn("核心结论", markdown)
                service.close()

    def test_partial_success_activates_valid_note(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)

                class PartialAgent:
                    name = "partial-test"

                    def build_plan(self, _document):
                        return KnowledgePlan(
                            document_summary="partial",
                            notes=[
                                NoteDraft(
                                    stable_key="valid",
                                    title="有效笔记",
                                    thesis="有证据",
                                    blocks=[
                                        NoteBlockDraft(
                                            text="梯度下降沿负梯度更新参数。",
                                            evidence_block_ids=["p1"],
                                        )
                                    ],
                                ),
                                NoteDraft(
                                    stable_key="invalid",
                                    title="无效笔记",
                                    thesis="缺少证据",
                                    blocks=[NoteBlockDraft(text="没有证据", evidence_block_ids=[])],
                                ),
                            ],
                        )

                    def answer(self, question, hits):
                        del question
                        return service.agent.answer("", hits)

                original = service.agent
                partial = PartialAgent()
                partial.answer = original.answer
                service.agent = partial
                result = service.run_job_until_terminal(ingest["job_id"], timeout_seconds=10)
                self.assertEqual(result["status"], "COMPLETED_WITH_WARNINGS")
                self.assertEqual(len(service.list_notes()), 1)
                with service.db.session() as session:
                    statuses = {
                        item.stable_key: item.validation_status
                        for item in session.scalars(select(NoteCandidate)).all()
                    }
                self.assertEqual(statuses, {"valid": "PASSED", "invalid": "FAILED"})
                service.close()

    def test_full_regeneration_creates_version_and_hides_old_notes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                first = service.ingest_pdf(pdf_path)
                service.run_job_until_terminal(first["job_id"], timeout_seconds=10)

                repeated = service.ingest_pdf(pdf_path, source_id=first["source_id"])
                self.assertEqual(repeated["deduplicated"], "true")
                self.assertEqual(repeated["source_version_id"], first["source_version_id"])

                regenerated = service.ingest_pdf(
                    pdf_path,
                    source_id=first["source_id"],
                    force_new_version=True,
                )
                self.assertEqual(regenerated["deduplicated"], "false")
                self.assertNotEqual(regenerated["source_version_id"], first["source_version_id"])
                service.run_job_until_terminal(regenerated["job_id"], timeout_seconds=10)

                with service.db.session() as session:
                    old_notes = session.scalars(
                        select(AtomicNote).where(
                            AtomicNote.source_version_id == first["source_version_id"]
                        )
                    ).all()
                    new_notes = session.scalars(
                        select(AtomicNote).where(
                            AtomicNote.source_version_id == regenerated["source_version_id"]
                        )
                    ).all()
                    source = session.get(Source, first["source_id"])
                    self.assertTrue(all(note.visibility == "HIDDEN" for note in old_notes))
                    self.assertTrue(all(note.visibility == "ACTIVE" for note in new_notes))
                    self.assertEqual(source.current_version_id, regenerated["source_version_id"])
                self.assertEqual(len(service.list_notes()), 2)
                service.close()

    def test_completed_job_can_restart_v3_generation_without_reparsing(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings:
                service = NoodleService(settings)
                service.init()
                ingest = service.ingest_pdf(pdf_path)
                completed = service.run_job_until_terminal(
                    ingest["job_id"], timeout_seconds=10
                )
                self.assertEqual(completed["status"], "COMPLETED")
                original_parse_run_id = completed["parse_run_id"]
                with service.db.session() as session:
                    job = session.get(Job, completed["id"])
                    normalized_path = Path(json.loads(job.state_json)["normalized_path"])
                (normalized_path.parent / "generation-v4.json").write_text(
                    json.dumps({"schema_version": "source-note-generation-v4"}),
                    encoding="utf-8",
                )

                restarted = service.restart_generation(completed["id"], "outline")
                self.assertEqual(restarted["status"], "RUNNING")
                self.assertEqual(restarted["stage"], "PLANNING_SOURCE_NOTE_OUTLINE")
                self.assertEqual(restarted["parse_run_id"], original_parse_run_id)

                rerun = service.run_job_until_terminal(completed["id"], timeout_seconds=10)
                self.assertEqual(rerun["status"], "COMPLETED")
                self.assertEqual(rerun["parse_run_id"], original_parse_run_id)
                source_note = service.get_source_note(
                    service.list_source_notes()[0]["source_note_id"]
                )
                roots = source_note["outline"]
                self.assertTrue(roots)
                self.assertTrue(roots[0]["children"])
                self.assertEqual(
                    sum(len(child["notes"]) for child in roots[0]["children"]),
                    source_note["unit_count"],
                )

                replacement = NoteDraft(
                    stable_key="replacement-note",
                    title="Replacement note",
                    thesis="Only the current release set remains active.",
                    blocks=[
                        NoteBlockDraft(
                            text="Gradient descent updates parameters.",
                            evidence_block_ids=["p1"],
                        )
                    ],
                )
                service._persist_notes(
                    completed["id"], ingest["source_id"], ingest["source_version_id"],
                    [replacement],
                )
                with service.db.session() as session:
                    rows = session.scalars(
                        select(AtomicNote).where(
                            AtomicNote.source_version_id == ingest["source_version_id"]
                        )
                    ).all()
                self.assertEqual(
                    [note.stable_key for note in rows if note.visibility == "ACTIVE"],
                    ["replacement-note"],
                )
                self.assertTrue(
                    all(
                        note.visibility == "HIDDEN"
                        for note in rows
                        if note.stable_key != "replacement-note"
                    )
                )
                replacement_v2 = replacement.model_copy(
                    update={
                        "thesis": "Changed content creates a new note version.",
                        "blocks": [
                            NoteBlockDraft(
                                text="The replacement body has changed.",
                                evidence_block_ids=["p1"],
                            )
                        ],
                    }
                )
                service._persist_notes(
                    completed["id"], ingest["source_id"], ingest["source_version_id"],
                    [replacement_v2],
                )
                with service.db.session() as session:
                    replacement_row = session.scalar(
                        select(AtomicNote).where(
                            AtomicNote.source_version_id == ingest["source_version_id"],
                            AtomicNote.stable_key == "replacement-note",
                        )
                    )
                    active_version = session.get(
                        NoteVersion, replacement_row.active_version_id
                    )
                    version_count = len(
                        session.scalars(
                            select(NoteVersion).where(
                                NoteVersion.note_id == replacement_row.id
                            )
                        ).all()
                    )
                self.assertEqual(active_version.thesis, replacement_v2.thesis)
                self.assertEqual(version_count, 2)
                service.close()

    def test_http_health_and_notes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            with isolated_settings(root) as settings, TestClient(create_app(settings)) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json()["database"])
                self.assertEqual(client.get("/notes").json(), [])
                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn("学习批注工作台", home.text)
                agent = client.post("/agent/chat", json={"message": "有哪些笔记"})
                self.assertEqual(agent.status_code, 200)
                payload = agent.json()
                self.assertEqual(payload["skill"], "evidence-grounded-qa")
                self.assertEqual(payload["tool_calls"][0]["tool_name"], "note.list")
                run = client.get(f"/agent/runs/{payload['run_id']}")
                self.assertEqual(run.status_code, 200)
                self.assertEqual(run.json()["status"], "COMPLETED")

    def test_http_upload_returns_background_job_links(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = create_fixture(root)
            with isolated_settings(root) as settings, TestClient(create_app(settings)) as client:
                with pdf_path.open("rb") as pdf_file:
                    response = client.post(
                        "/sources/pdf?title=Web%20Upload",
                        files={"file": ("course.pdf", pdf_file, "application/pdf")},
                    )
                self.assertEqual(response.status_code, 202)
                payload = response.json()
                self.assertEqual(payload["job_url"], f"/jobs/{payload['job_id']}")
                self.assertEqual(
                    payload["timeline_url"], f"/jobs/{payload['job_id']}/timeline"
                )
                job_result = client.get(payload["result_url"])
                self.assertEqual(job_result.status_code, 200)
                self.assertFalse(job_result.json()["ready"])
                with pdf_path.open("rb") as pdf_file:
                    duplicate = client.post(
                        "/sources/pdf",
                        files={"file": ("duplicate.pdf", pdf_file, "application/pdf")},
                    )
                self.assertEqual(duplicate.status_code, 202)
                self.assertEqual(duplicate.json()["job_id"], payload["job_id"])
                self.assertEqual(duplicate.json()["deduplicated"], "true")
                self.assertEqual(len(client.get("/jobs").json()), 1)

    def test_generation_validators_reject_missing_core_and_invalid_text(self) -> None:
        document = ParsedDocument(
            title="课程",
            page_count=1,
            markdown="知识",
            parser_name="test",
            blocks=[ParsedBlock(block_id="b1", page_index=0, page_label="1", text="证据")],
        )
        knowledge_map = KnowledgeMap(
            document_summary="课程",
            concepts=[
                KnowledgeConcept(
                    stable_key="core",
                    title="核心",
                    question_answered="核心是什么？",
                    core_claim="核心结论",
                    importance="CORE",
                    evidence_block_ids=["b1"],
                ),
                KnowledgeConcept(
                    stable_key="supporting",
                    title="扩展",
                    question_answered="扩展是什么？",
                    core_claim="扩展结论",
                    evidence_block_ids=["b1"],
                ),
            ],
        )
        note_set = NoteSetPlan(
            document_summary="课程",
            notes=[
                NoteSpec(
                    stable_key="supporting-note",
                    title="扩展",
                    question_answered="扩展是什么？",
                    target_thesis="扩展结论",
                    concept_keys=["supporting"],
                    evidence_block_ids=["b1"],
                )
            ],
            skipped_concepts=[{"concept_key": "core", "reason": "跳过"}],
        )
        review = NoteSetReview(
            passed=True,
            coverage=[
                ConceptCoverage(concept_key="core", status="SKIPPED", reason="跳过"),
                ConceptCoverage(
                    concept_key="supporting",
                    status="COVERED",
                    note_keys=["supporting-note"],
                ),
            ],
        )
        errors = NoodleService._validate_note_set(
            document, knowledge_map, note_set, review
        )
        self.assertIn("UNCOVERED_CORE_CONCEPT:core", errors)

        plan = KnowledgePlan(
            document_summary="课程",
            notes=[
                NoteDraft(
                    stable_key="bad-character",
                    title="异常字符",
                    thesis="公式含有对象占位符\ufffc",
                    blocks=[NoteBlockDraft(text="正文", evidence_block_ids=["b1"])],
                )
            ],
        )
        passed, failures = NoodleService._validate_plan(document, plan)
        self.assertEqual(passed, [])
        self.assertIn("INVALID_TEXT_CHARACTER", failures[0]["detail"])


if __name__ == "__main__":
    unittest.main()
