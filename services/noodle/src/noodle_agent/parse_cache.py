"""Content-addressed reuse of real normalized parses, never of knowledge releases."""

import json
import shutil
from pathlib import Path

from sqlalchemy import select

from .db import Job, ParseRun, Source, SourceVersion
from .domain import ParsedDocument


def parser_contract(service):
    return {
        "provider": service.parser.name,
        "model": service.settings.mineru_model_version,
        "mode": service.settings.mineru_mode,
        "language": service.settings.mineru_language,
        "normalizer": "parsed-document-v1",
    }


def reuse_parse(service, job_id):
    if not service.settings.parse_cache_enabled or service.parser.name != "mineru":
        return False
    contract = parser_contract(service)
    with service.db.session() as session:
        job = session.get(Job, job_id)
        if json.loads(job.state_json or "{}").get("parse_cache_hit"):
            return True
        version = session.get(SourceVersion, job.source_version_id)
        source = session.get(Source, version.source_id)
        if not source or source.visibility != "ACTIVE":
            return False
        current_parse = job.parse_run_id
        title = source.title
        rows = session.execute(
            select(ParseRun, Job)
            .join(Job, Job.parse_run_id == ParseRun.id)
            .join(SourceVersion, SourceVersion.id == ParseRun.source_version_id)
            .where(
                SourceVersion.content_sha256 == version.content_sha256,
                ParseRun.status == "SUCCEEDED",
                ParseRun.provider == service.parser.name,
                ParseRun.parser_version == service.settings.mineru_model_version,
                ParseRun.id != current_parse,
            )
            .order_by(ParseRun.completed_at.desc())
        ).all()
        snapshots = [(p.id, json.loads(j.state_json or "{}")) for p, j in rows]
    for parse_id, state in snapshots:
        previous = state.get("parse_cache_contract")
        # Legacy precision artifacts are eligible only under the original default locale.
        if previous != contract and not (
            previous is None and contract["mode"] == "precision" and contract["language"] == "ch"
        ):
            continue
        normalized = Path(state.get("normalized_path", "")).resolve()
        expected_root = (service.settings.artifact_dir / parse_id).resolve()
        if not normalized.is_file() or normalized.parent != expected_root:
            continue
        document = ParsedDocument.model_validate_json(normalized.read_text(encoding="utf-8"))
        if document.parser_name != "mineru" or document.parser_version != contract["model"]:
            continue
        files = [Path(b.asset_path).resolve() for b in document.blocks if b.asset_path]
        if document.raw_artifact_path:
            files.append(Path(document.raw_artifact_path).resolve())
        if any(not f.is_file() or expected_root not in f.parents for f in files):
            continue
        destination = service.settings.artifact_dir / current_parse
        destination.mkdir(parents=True, exist_ok=True)
        for path in set(files):
            target = destination / path.relative_to(expected_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        for block in document.blocks:
            if block.asset_path:
                block.asset_path = str(
                    destination / Path(block.asset_path).resolve().relative_to(expected_root)
                )
        if document.raw_artifact_path:
            document.raw_artifact_path = str(
                destination / Path(document.raw_artifact_path).resolve().relative_to(expected_root)
            )
        document.title = title
        target = destination / "normalized.json"
        target.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        service._persist_parsed_document(job_id, document, target)
        with service.db.session() as session:
            job = session.get(Job, job_id)
            data = json.loads(job.state_json or "{}")
            data.update(
                parse_cache_hit=True, parse_cache_source_id=parse_id, parse_cache_contract=contract
            )
            job.state_json = json.dumps(data, ensure_ascii=False)
        return True
    return False
