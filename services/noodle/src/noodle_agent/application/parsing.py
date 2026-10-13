from __future__ import annotations

import mimetypes
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from ..adapters.mineru import validate_pdf
from ..composition import Component
from ..db import (
    BlobObject,
    DocumentBlock,
    Job,
    OutlineNode,
    ParseRun,
    Source,
    SourcePage,
    SourceVersion,
    VisualAsset,
    utcnow,
)
from ..domain import (
    JobStage,
    JobStatus,
    ParsedDocument,
)
from ..parse_cache import parser_contract, reuse_parse
from .common import (
    _json,
    _normalize_warnings,
    _parse_json,
    _sha256_text,
)


class ParsingModule(Component):
    def _submit_parse(self, job_id: str) -> None:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            blob = session.get(BlobObject, source_version.original_blob_id)
            pdf_path = self.owner.blobs.path_for(blob.storage_key)
            request_hash = _sha256_text(
                _json(
                    {
                        "source_version": source_version.id,
                        "provider": self.owner.parser.name,
                        "content": source_version.content_sha256,
                    }
                )
            )
            parse_run = ParseRun(
                source_version_id=source_version.id,
                provider=self.owner.parser.name,
                request_hash=request_hash,
                status="SUBMITTING",
            )
            session.add(parse_run)
            session.flush()
            job.parse_run_id = parse_run.id
            job.stage = JobStage.SUBMITTING_PARSE.value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.05

        self.owner.telemetry.transition_stage(
            job_id,
            JobStage.SUBMITTING_PARSE.value,
            0.05,
            {"provider": self.owner.parser.name},
        )
        if reuse_parse(self.owner, job_id):
            self.owner.telemetry.transition_stage(
                job_id, JobStage.MAPPING.value, 0.4, {"parse_cache_hit": True}
            )
            return
        submission = self.owner.parser.submit(pdf_path, source_version.id)
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            parse_run = session.get(ParseRun, job.parse_run_id)
            parse_run.provider_task_id = submission["external_id"]
            parse_run.status = "WAITING"
            job.stage = JobStage.WAITING_PARSE.value
            state = {"parser_result": submission.get("result") or {}}
            job.state_json = _json(state)
            if submission.get("state") == "done":
                job.status = JobStatus.RUNNING.value
                job.next_attempt_at = utcnow()
            else:
                job.status = JobStatus.WAITING_EXTERNAL.value
                job.next_attempt_at = utcnow() + timedelta(
                    seconds=self.owner.settings.mineru_poll_seconds
                )
            job.progress = 0.12
        self.owner.telemetry.transition_stage(
            job_id,
            JobStage.WAITING_PARSE.value,
            0.12,
            {
                "provider": self.owner.parser.name,
                "external_state": str(submission.get("state") or "pending"),
            },
        )

    def _poll_or_normalize(self, job_id: str) -> None:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.stage != JobStage.WAITING_PARSE.value:
                return
            parse_run = session.get(ParseRun, job.parse_run_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            source = session.get(Source, source_version.source_id)
            state = _parse_json(job.state_json, {})
            external_id = parse_run.provider_task_id
            result = state.get("parser_result") or {}

        if reuse_parse(self.owner, job_id):
            self.owner.telemetry.transition_stage(
                job_id, JobStage.MAPPING.value, 0.4, {"parse_cache_hit": True}
            )
            return
        if not result or result.get("state") not in {"done", "failed"}:
            result = self.owner.parser.poll(external_id)
        external_state = str(result.get("state", "pending")).lower()
        if external_state in {"waiting-file", "uploading", "pending", "running", "converting"}:
            with self.owner.db.session() as session:
                job = session.get(Job, job_id)
                job.status = JobStatus.WAITING_EXTERNAL.value
                job.next_attempt_at = utcnow() + timedelta(
                    seconds=self.owner.settings.mineru_poll_seconds
                )
                job.state_json = _json({"parser_result": result})
                job.progress = min(0.28, max(job.progress, 0.15))
            self.owner.telemetry.transition_stage(
                job_id,
                JobStage.WAITING_PARSE.value,
                min(0.28, max(job.progress, 0.15)),
                {"provider": self.owner.parser.name, "external_state": external_state},
            )
            return
        if external_state == "failed":
            raise RuntimeError(
                f"MinerU parse failed: {result.get('err_code')} {result.get('err_msg')}"
            )
        if external_state != "done":
            raise RuntimeError(f"Unknown parser state: {external_state}")

        self.owner.telemetry.transition_stage(
            job_id,
            JobStage.NORMALIZING.value,
            0.3,
            {"provider": self.owner.parser.name, "external_state": external_state},
        )
        artifact_dir = self.owner.settings.artifact_dir / parse_run.id
        document = self.owner.parser.download_and_normalize(
            external_id, result, artifact_dir, source.title
        )
        normalized_path = artifact_dir / "normalized.json"
        normalized_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        self.owner._persist_parsed_document(job_id, document, normalized_path)

    def _persist_parsed_document(
        self, job_id: str, document: ParsedDocument, normalized_path: Path
    ) -> None:
        with self.owner.db.session() as session:
            job = session.get(Job, job_id)
            parse_run = session.get(ParseRun, job.parse_run_id)
            source_version = session.get(SourceVersion, job.source_version_id)
            blob = session.get(BlobObject, source_version.original_blob_id)
            actual_pages = validate_pdf(self.owner.blobs.path_for(blob.storage_key))
            warnings = _parse_json(job.warning_json, [])
            markdown_only = (document.parser_version or "").endswith("-markdown")
            if actual_pages and document.page_count != actual_pages:
                if markdown_only:
                    warnings.append(
                        {
                            "code": "PAGE_PRECISION_UNAVAILABLE",
                            "detail": (
                                f"Markdown-only parser result reports {document.page_count} page(s); "
                                f"PDF has {actual_pages}. Use MINERU_MODE=precision for page evidence."
                            ),
                        }
                    )
                    document.page_count = actual_pages
                else:
                    raise RuntimeError(
                        f"Global page mapping mismatch: PDF={actual_pages}, parser={document.page_count}"
                    )
            page_by_index: dict[int, SourcePage] = {}
            page_labels = {block.page_index: block.page_label for block in document.blocks}
            for page_index in range(document.page_count):
                page = SourcePage(
                    parse_run_id=parse_run.id,
                    page_index=page_index,
                    page_label=page_labels.get(page_index, str(page_index + 1)),
                    mapping_status="UNAVAILABLE" if markdown_only else "VERIFIED",
                )
                session.add(page)
                session.flush()
                page_by_index[page_index] = page
            heading_rows: list[tuple[DocumentBlock, int]] = []
            for order, block in enumerate(document.blocks):
                page_index = min(max(block.page_index, 0), document.page_count - 1)
                row = DocumentBlock(
                    parse_run_id=parse_run.id,
                    page_id=page_by_index[page_index].id,
                    external_block_id=block.block_id,
                    block_type=block.block_type,
                    reading_order=order,
                    text_content=block.text,
                    bbox_json=_json(block.bbox) if block.bbox else None,
                    metadata_json=_json(block.metadata),
                )
                session.add(row)
                session.flush()
                if block.asset_path:
                    asset_path = Path(block.asset_path)
                    if asset_path.exists() and asset_path.is_file():
                        media_type = (
                            mimetypes.guess_type(asset_path.name)[0] or "application/octet-stream"
                        )
                        storage_key, sha256, byte_size = self.owner.blobs.put(
                            asset_path, media_type
                        )
                        asset_blob = session.scalar(
                            select(BlobObject).where(
                                BlobObject.content_sha256 == sha256,
                                BlobObject.media_type == media_type,
                            )
                        )
                        if asset_blob is None:
                            asset_blob = BlobObject(
                                storage_key=storage_key,
                                content_sha256=sha256,
                                media_type=media_type,
                                byte_size=byte_size,
                            )
                            session.add(asset_blob)
                            session.flush()
                        session.add(
                            VisualAsset(
                                parse_run_id=parse_run.id,
                                page_id=page_by_index[page_index].id,
                                document_block_id=row.id,
                                blob_id=asset_blob.id,
                                asset_type=block.block_type,
                                caption=block.text,
                                bbox_json=_json(block.bbox) if block.bbox else None,
                            )
                        )
                if block.block_type in {"TITLE", "HEADING"}:
                    heading_rows.append((row, int(block.level or 1)))
            for order, (row, level) in enumerate(heading_rows):
                page = session.get(SourcePage, row.page_id)
                next_page = (
                    session.get(SourcePage, heading_rows[order + 1][0].page_id).page_index
                    if order + 1 < len(heading_rows)
                    else document.page_count - 1
                )
                session.add(
                    OutlineNode(
                        parse_run_id=parse_run.id,
                        title=row.text_content,
                        level=level,
                        start_page_index=page.page_index,
                        end_page_index=max(page.page_index, next_page),
                        order_index=order,
                    )
                )
            parse_run.status = "SUCCEEDED"
            parse_run.parser_version = document.parser_version
            parse_run.completed_at = utcnow()
            source_version.canonical_parse_run_id = parse_run.id
            job.stage = JobStage.MAPPING.value
            job.status = JobStatus.RUNNING.value
            job.progress = 0.4
            warnings = _normalize_warnings(warnings)
            job.warning_json = _json(warnings)
            state = {"normalized_path": str(normalized_path)}
            if document.parser_name == "mineru":
                state["parse_cache_contract"] = parser_contract(self.owner)
            job.state_json = _json(state)
        self.owner.telemetry.transition_stage(
            job_id,
            JobStage.MAPPING.value,
            0.4,
            {
                "page_count": document.page_count,
                "block_count": len(document.blocks),
                "outline_node_count": len(heading_rows),
            },
        )
