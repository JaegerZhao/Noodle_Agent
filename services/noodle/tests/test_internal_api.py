from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf as fitz
import websockets
from fastapi.testclient import TestClient

from noodle_agent.config import Settings
from noodle_agent.db import BlobObject, Concept, ConceptVersion, Job, SourceVersion
from noodle_agent.internal_api import create_app


class InternalApiTest(unittest.TestCase):
    def test_original_pdf_can_be_read_inline_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = root / "course source.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Original source page")
            document.save(pdf_path)
            document.close()
            app = create_app(Settings.from_env(root))
            with TestClient(app) as client, pdf_path.open("rb") as stream:
                ingested = client.post(
                    "/internal/v1/sources/pdf",
                    files={"file": (pdf_path.name, stream, "application/pdf")},
                ).json()
                response = client.get(
                    f"/internal/v1/sources/{ingested['source_id']}/pdf"
                )
                opaque = client.get(
                    f"/internal/v1/sources/{ingested['source_id']}/original-content"
                )
                missing = client.get("/internal/v1/sources/missing/pdf")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertIn("inline", response.headers["content-disposition"])
            self.assertTrue(response.content.startswith(b"%PDF"))
            self.assertEqual(opaque.status_code, 200)
            self.assertEqual(opaque.headers["content-type"], "application/octet-stream")
            self.assertEqual(opaque.headers["x-noodle-media-type"], "application/pdf")
            self.assertNotIn("content-disposition", opaque.headers)
            self.assertTrue(opaque.content.startswith(b"%PDF"))
            self.assertEqual(missing.status_code, 404)

    def test_source_management_retries_then_deletes_all_noodle_owned_data(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = root / "managed.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Managed source")
            document.save(pdf_path)
            document.close()
            app = create_app(Settings.from_env(root))
            with TestClient(app) as client, pdf_path.open("rb") as stream:
                ingested = client.post(
                    "/internal/v1/sources/pdf",
                    files={"file": (pdf_path.name, stream, "application/pdf")},
                ).json()
                with app.state.noodle.db.session() as session:
                    failed = session.get(Job, ingested["job_id"])
                    failed.status = "FAILED"
                    failed.stage = "SUBMITTING_PARSE"
                    version = session.get(SourceVersion, ingested["source_version_id"])
                    blob = session.get(BlobObject, version.original_blob_id)
                    original_path = app.state.noodle.blobs.path_for(blob.storage_key)

                retried = client.post(
                    f"/internal/v1/jobs/{ingested['job_id']}/resume"
                )
                self.assertEqual(retried.status_code, 200)
                self.assertEqual(retried.json()["retry_mode"], "from_original")
                with app.state.noodle.db.session() as session:
                    replacement = session.get(Job, retried.json()["id"])
                    replacement.status = "FAILED"
                    replacement.stage = "SUBMITTING_PARSE"

                deleted = client.delete(
                    f"/internal/v1/sources/{ingested['source_id']}"
                )
                self.assertEqual(deleted.status_code, 200)
                self.assertTrue(deleted.json()["source_deleted"])
                self.assertTrue(original_path.exists())
                self.assertTrue(pdf_path.is_file())
                library = client.get("/internal/v1/library").json()
                self.assertEqual(library["sources"], [])

    def test_ingest_links_job_to_harness_session_and_tool_call(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = root / "linked.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Harness linked PDF")
            document.save(pdf_path)
            document.close()
            settings = Settings.from_env(root)
            app = create_app(settings)
            with TestClient(app) as client, pdf_path.open("rb") as stream:
                response = client.post(
                    "/internal/v1/sources/pdf",
                    files={"file": ("linked.pdf", stream, "application/pdf")},
                    headers={
                        "X-Harness-Session-Id": "session-p0",
                        "X-Harness-Tool-Call-Id": "call-p0",
                    },
                )
                self.assertEqual(response.status_code, 202)
                job = client.get(
                    f"/internal/v1/jobs/{response.json()['job_id']}"
                ).json()
            self.assertEqual(job["harness"]["session_id"], "session-p0")
            self.assertEqual(job["harness"]["tool_call_id"], "call-p0")

    def test_image_attachment_round_trip(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            settings = Settings.from_env(Path(raw))
            app = create_app(settings)
            with TestClient(app) as client:
                created = client.post(
                    "/internal/v1/attachments/images?analyze=false",
                    files={"file": ("replacement.png", png, "image/png")},
                    headers={"X-Harness-Session-Id": "session-image"},
                )
                self.assertEqual(created.status_code, 201)
                payload = created.json()
                fetched = client.get(
                    f"/internal/v1/attachments/{payload['attachment_id']}"
                )
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(fetched.json()["status"], "READY")
            self.assertTrue(fetched.json()["blob_url"].startswith("/internal/v1/blobs/"))

    def test_domain_api_excludes_legacy_agent_and_web(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            settings = Settings.from_env(root)
            app = create_app(settings)
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["api_surface"], "internal-v1")
                self.assertEqual(
                    health.json()["agent_runtime"],
                    "external-deepseek-harness",
                )
                self.assertFalse(health.json()["legacy_agent_loaded"])
                self.assertFalse(health.json()["legacy_web_loaded"])
                self.assertTrue(health.json()["knowledge_workbench_loaded"])

                self.assertEqual(client.get("/internal/v1/source-notes").json(), [])
                self.assertEqual(client.get("/internal/v1/notes").json(), [])
                self.assertEqual(client.post("/agent/chat", json={"message": "hi"}).status_code, 404)
                self.assertEqual(client.get("/ui/").status_code, 404)
                self.assertEqual(client.get("/source-notes").status_code, 404)
                workbench = client.get("/app/")
                self.assertEqual(workbench.status_code, 200)
                self.assertIn("Noodle Agent", workbench.text)
                self.assertIn(
                    "/app/workbench.js?v=20261012-knowledge-sync-v2",
                    workbench.text,
                )
                self.assertEqual(
                    workbench.headers["cache-control"],
                    "no-store, no-cache, must-revalidate",
                )
                self.assertIn('id="agent-form"', workbench.text)
                self.assertIn('id="agent-attachment-shelf"', workbench.text)
                self.assertIn('id="agent-image-file"', workbench.text)
                self.assertNotIn('id="harness-frame"', workbench.text)
                workbench_script = client.get("/app/workbench.js")
                self.assertEqual(workbench_script.status_code, 200)
                self.assertIn("仅允许本次", workbench_script.text)
                self.assertNotIn("打开审批面板", workbench_script.text)
                self.assertEqual(
                    workbench_script.headers["cache-control"],
                    "no-store, no-cache, must-revalidate",
                )
                pdf_module = client.get("/app/vendor/pdfjs/pdf.min.mjs")
                self.assertEqual(pdf_module.status_code, 200)
                self.assertTrue(
                    pdf_module.headers["content-type"].startswith("text/javascript")
                )
                with app.state.noodle.db.session() as session:
                    concept = Concept(
                        stable_key="test:pinned-api",
                        domain_key="test",
                        canonical_name="Pinned API",
                    )
                    session.add(concept)
                    session.flush()
                    version = ConceptVersion(
                        concept_id=concept.id,
                        version_no=1,
                        title="Pinned API v1",
                        summary="Version-addressable Concept.",
                        content_sha256="0" * 64,
                        status="PUBLISHED",
                    )
                    session.add(version)
                    session.flush()
                    concept.active_version_id = version.id
                    concept_id = concept.id
                    version_id = version.id
                pinned = client.get(
                    f"/internal/v1/concepts/{concept_id}",
                    params={"version_id": version_id},
                )
                self.assertEqual(pinned.status_code, 200)
                self.assertEqual(pinned.json()["version_id"], version_id)

            self.assertFalse((settings.data_dir / "agent_checkpoints.db").exists())

    def test_job_warning_severity_distinguishes_info_from_material_warning(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            pdf_path = root / "warnings.pdf"
            document = fitz.open()
            document.new_page().insert_text((72, 72), "Warning semantics")
            document.save(pdf_path)
            document.close()
            app = create_app(Settings.from_env(root))
            with TestClient(app) as client, pdf_path.open("rb") as stream:
                ingested = client.post(
                    "/internal/v1/sources/pdf",
                    files={"file": (pdf_path.name, stream, "application/pdf")},
                ).json()
                with app.state.noodle.db.session() as session:
                    job = session.get(Job, ingested["job_id"])
                    job.status = "COMPLETED_WITH_WARNINGS"
                    job.stage = "COMPLETED"
                    job.warning_json = json.dumps(
                        [{"code": "SKIPPED_SECTION", "section": "结语"}],
                        ensure_ascii=False,
                    )

                info_only = client.get(
                    f"/internal/v1/jobs/{ingested['job_id']}"
                ).json()
                self.assertEqual(info_only["status"], "COMPLETED")
                self.assertEqual(info_only["warnings"][0]["severity"], "INFO")

                with app.state.noodle.db.session() as session:
                    job = session.get(Job, ingested["job_id"])
                    job.warning_json = json.dumps(
                        [{"code": "CANDIDATE_QUALITY_FAILED", "detail": "judge failed"}]
                    )
                material = client.get(
                    f"/internal/v1/jobs/{ingested['job_id']}"
                ).json()
                self.assertEqual(material["status"], "COMPLETED_WITH_WARNINGS")
                self.assertEqual(material["warnings"][0]["severity"], "WARNING")

    def test_tool_call_id_is_returned_as_correlation_id(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            settings = Settings.from_env(Path(raw))
            app = create_app(settings)
            with TestClient(app) as client:
                response = client.get(
                    "/health",
                    headers={"X-Harness-Tool-Call-Id": "tool-call-123"},
                )
            self.assertEqual(response.headers["X-Correlation-Id"], "tool-call-123")

    def test_library_folders_move_and_reorder_sources(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            settings = Settings.from_env(root)
            app = create_app(settings)
            pdf_paths: list[Path] = []
            for index in range(2):
                pdf_path = root / f"course-{index + 1}.pdf"
                document = fitz.open()
                document.new_page().insert_text((72, 72), f"Course {index + 1}")
                document.save(pdf_path)
                document.close()
                pdf_paths.append(pdf_path)

            with TestClient(app) as client:
                folder = client.post(
                    "/internal/v1/folders",
                    json={"name": "机器学习", "parent_id": None},
                )
                self.assertEqual(folder.status_code, 201)
                folder_id = folder.json()["id"]

                source_ids: list[str] = []
                for pdf_path in pdf_paths:
                    with pdf_path.open("rb") as stream:
                        ingest = client.post(
                            "/internal/v1/sources/pdf",
                            files={"file": (pdf_path.name, stream, "application/pdf")},
                        )
                    self.assertEqual(ingest.status_code, 202)
                    source_id = ingest.json()["source_id"]
                    source_ids.append(source_id)
                    moved = client.put(
                        f"/internal/v1/sources/{source_id}/folder",
                        json={"folder_id": folder_id},
                    )
                    self.assertEqual(moved.status_code, 200)

                reordered = client.put(
                    f"/internal/v1/folders/{folder_id}/source-order",
                    json={"source_ids": list(reversed(source_ids))},
                )
                self.assertEqual(reordered.status_code, 200)
                library = client.get("/internal/v1/library")

            self.assertEqual(library.status_code, 200)
            payload = library.json()
            self.assertEqual(payload["folders"][0]["source_count"], 2)
            placed = sorted(
                payload["sources"],
                key=lambda item: item["sort_order"],
            )
            self.assertEqual(
                [item["source_id"] for item in placed],
                list(reversed(source_ids)),
            )
            self.assertTrue(all(item["folder_id"] == folder_id for item in placed))

    def test_ui_config_exposes_harness_adapter_url(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            settings = Settings.from_env(Path(raw))
            app = create_app(settings)
            with TestClient(app) as client:
                response = client.get("/internal/v1/ui-config")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["harness_web_url"], "http://127.0.0.1:3080")
            self.assertEqual(response.json()["harness_chat_api"], "/internal/v1/harness")

    def test_curated_synthesis_http_create_publish_and_stale_write(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            app = create_app(Settings.from_env(Path(raw)))
            with TestClient(app) as client:
                with app.state.noodle.db.session() as session:
                    concept = Concept(
                        stable_key="test:http-curation",
                        domain_key="test",
                        canonical_name="HTTP 知识点",
                        original_name="HTTP Concept",
                        status="PUBLISHED",
                    )
                    session.add(concept)
                    session.flush()
                    version = ConceptVersion(
                        concept_id=concept.id,
                        version_no=1,
                        title="HTTP 知识点",
                        summary="供无代码知识体系接口验收使用。",
                        content_sha256="1" * 64,
                        status="PUBLISHED",
                    )
                    session.add(version)
                    session.flush()
                    concept.active_version_id = version.id
                    concept_id = concept.id

                registry = client.get(
                    "/internal/v1/concepts",
                    params={"q": "HTTP", "limit": 10},
                )
                self.assertEqual(registry.status_code, 200)
                self.assertEqual(registry.json()[0]["concept_id"], concept_id)

                outline = [
                    {
                        "stable_key": "foundations",
                        "title": "基础",
                        "node_kind": "GROUP",
                        "children": [
                            {
                                "stable_key": "http-concept",
                                "title": "HTTP 知识点",
                                "node_kind": "CONCEPT",
                                "concept_id": concept_id,
                                "children": [],
                            }
                        ],
                    }
                ]
                created = client.post(
                    "/internal/v1/syntheses",
                    json={
                        "stable_key": "http-curation-system",
                        "domain_key": "test",
                        "title": "HTTP 创建的知识体系",
                        "description": "通过统一接口创建。",
                        "outline": outline,
                    },
                )
                self.assertEqual(created.status_code, 201)
                created_payload = created.json()
                self.assertEqual(created_payload["version_no"], 1)
                self.assertEqual(created_payload["coverage"]["concept_nodes"], 1)

                published = client.put(
                    f"/internal/v1/syntheses/{created_payload['synthesis_id']}",
                    json={
                        "expected_version_id": created_payload["version_id"],
                        "title": "HTTP 创建的知识体系（修订）",
                        "description": "发布不可变新版本。",
                        "outline": outline,
                    },
                )
                self.assertEqual(published.status_code, 200)
                self.assertEqual(published.json()["version_no"], 2)

                stale = client.put(
                    f"/internal/v1/syntheses/{created_payload['synthesis_id']}",
                    json={
                        "expected_version_id": created_payload["version_id"],
                        "title": "陈旧写入",
                        "description": "必须被 CAS 拒绝。",
                        "outline": outline,
                    },
                )
                self.assertEqual(stale.status_code, 409)

    def test_custom_chat_adapter_preserves_harness_rpc_contract(self) -> None:
        calls: list[dict] = []

        async def fake_post(_client, url, *, json):
            calls.append({"url": str(url), "body": json})
            value = (
                {"items": [{"sessionId": "session-demo", "blank": True}]}
                if json["method"] == "session.list"
                else {"accepted": True}
            )
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={
                    "type": "server-response",
                    "rpcId": json["rpcId"],
                    "result": {"ok": True, "value": value},
                },
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            app = create_app(Settings.from_env(Path(raw)))
            with (
                patch.object(httpx.AsyncClient, "post", new=fake_post),
                TestClient(app) as client,
            ):
                sessions = client.get("/internal/v1/harness/sessions")
                renamed = client.patch(
                    "/internal/v1/harness/sessions/session-demo",
                    json={"title": "课程问答"},
                )
                prompted = client.post(
                    "/internal/v1/harness/prompt",
                    json={
                        "session_id": "session-demo",
                        "message": "解释当前笔记",
                        "context": "SourceNote ID：source-1",
                        "mode": "queue",
                    },
                )

        self.assertEqual(sessions.status_code, 200)
        self.assertEqual(sessions.json()["items"][0]["sessionId"], "session-demo")
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(calls[1]["body"]["method"], "session.rename")
        self.assertEqual(
            calls[1]["body"]["payload"],
            {"sessionId": "session-demo", "title": "课程问答"},
        )
        self.assertEqual(prompted.status_code, 202)
        self.assertEqual(calls[2]["body"]["method"], "session.prompt")
        prompt = calls[2]["body"]["payload"]["content"][0]["text"]
        self.assertIn("SourceNote ID：source-1", prompt)
        self.assertIn("用户请求：\n解释当前笔记", prompt)

    def test_custom_chat_adapter_can_decide_pending_approval(self) -> None:
        approval_frame = {
            "rpcId": "approval-rpc-1",
            "payload": {
                "type": "approval/requested",
                "sessionId": "session-approve",
                "approvalId": "approval-1",
                "toolName": "noodle_edit_note_block",
                "callId": "call-1",
                "reason": "create a new note version",
            },
        }
        responses: list[dict] = []

        class FakeMuxSocket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def recv(self):
                return json.dumps(approval_frame)

        def fake_connect(url, **_kwargs):
            self.assertEqual(url, "ws://127.0.0.1:3080/api/events.mux")
            return FakeMuxSocket()

        async def fake_post(_client, url, *, json):
            responses.append({"url": str(url), "body": json})
            return httpx.Response(
                200,
                request=httpx.Request("POST", str(url)),
                json={"accepted": True},
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            app = create_app(Settings.from_env(Path(raw)))
            with (
                patch.object(websockets, "connect", new=fake_connect),
                patch.object(httpx.AsyncClient, "post", new=fake_post),
                TestClient(app) as client,
            ):
                decided = client.post(
                    "/internal/v1/harness/sessions/session-approve/approvals/approval-1",
                    json={"outcome": "allowed-once"},
                )

        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["outcome"], "allowed-once")
        self.assertEqual(responses[0]["url"], "http://127.0.0.1:3080/api/respond")
        self.assertEqual(responses[0]["body"]["type"], "client-response")
        self.assertEqual(responses[0]["body"]["rpcId"], "approval-rpc-1")
        self.assertEqual(
            responses[0]["body"]["result"]["value"],
            {
                "sessionId": "session-approve",
                "approvalId": "approval-1",
                "outcome": "allowed-once",
            },
        )


if __name__ == "__main__":
    unittest.main()
