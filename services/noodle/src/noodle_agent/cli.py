from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .services import NoodleService
from .worker import JobWorker


def _print(value: Any) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _service() -> NoodleService:
    service = NoodleService(Settings.from_env())
    service.init()
    return service


def _create_demo(directory: Path) -> dict[str, str]:
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise RuntimeError(
            "create-demo requires PyMuPDF; install project dependencies first"
        ) from exc
    directory.mkdir(parents=True, exist_ok=True)
    pdf_path = directory / "demo-course.pdf"
    document = fitz.open()
    for index, title in enumerate(
        ["Gradient Descent", "Learning Rate", "Convergence and Failure Modes"]
    ):
        page = document.new_page()
        page.insert_text((72, 72), f"Chapter {index + 1}: {title}", fontsize=16)
        page.insert_text(
            (72, 110),
            (
                "This PDF shell is paired with a fixture that simulates MinerU output. "
                f"Demo schema revision 2, page {index + 1}."
            ),
            fontsize=10,
        )
    document.save(pdf_path)
    document.close()
    (directory / "learning-rate-chart.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
    )
    sidecar = pdf_path.with_suffix(".mineru.json")
    sidecar.write_text(
        json.dumps(
            {
                "title": "梯度下降课程",
                "parser_version": "fixture-v1",
                "pages": [
                    {
                        "page_label": "1",
                        "blocks": [
                            {"id": "h1", "type": "heading", "level": 1, "text": "梯度下降是什么"},
                            {
                                "id": "p1",
                                "type": "text",
                                "text": "梯度下降通过沿损失函数负梯度方向迭代更新参数，以逐步减小目标函数。",
                            },
                        ],
                    },
                    {
                        "page_label": "2",
                        "blocks": [
                            {"id": "h2", "type": "heading", "level": 1, "text": "学习率的作用"},
                            {
                                "id": "p2",
                                "type": "text",
                                "text": "学习率决定每次参数更新的步长；过大会导致震荡或发散，过小会使收敛缓慢。",
                            },
                            {
                                "id": "img2",
                                "type": "chart",
                                "text": "不同学习率下的损失曲线",
                                "asset_path": "learning-rate-chart.png",
                            },
                        ],
                    },
                    {
                        "page_label": "3",
                        "blocks": [
                            {"id": "h3", "type": "heading", "level": 1, "text": "收敛与失败模式"},
                            {
                                "id": "p3",
                                "type": "text",
                                "text": "判断训练是否收敛需要同时观察损失曲线、梯度规模和验证集表现。",
                            },
                        ],
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"pdf": str(pdf_path), "sidecar": str(sidecar)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="noodle", description="Noodle Agent local MVP")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize local directories and SQLite")
    sub.add_parser("doctor", help="Show active adapters and storage health")
    demo = sub.add_parser("create-demo", help="Create a PDF plus MinerU fixture sidecar")
    demo.add_argument("--dir", type=Path, default=Path("./data/demo"))
    process = sub.add_parser("process", help="Ingest and process one PDF")
    process.add_argument("pdf", type=Path)
    process.add_argument("--title")
    process.add_argument("--source-id")
    process.add_argument(
        "--new-version",
        action="store_true",
        help="Run the full pipeline again as a new version of the same source",
    )
    process.add_argument("--no-wait", action="store_true")
    run = sub.add_parser("run", help="Run or resume a persisted job")
    run.add_argument("job_id")
    run.add_argument("--wait", action="store_true")
    run.add_argument("--resume", action="store_true", help="Resume a failed post-parse job")
    run.add_argument(
        "--restart-from",
        choices=[
            "profile",
            "knowledge-map",
            "outline",
            "note-set",
            "vision",
            "drafts",
            "evaluation",
            "validation",
        ],
        help="Invalidate generation artifacts from this step, then resume",
    )
    run.add_argument("--timeout", type=float, default=1800)
    sub.add_parser("notes", help="List active notes")
    sub.add_parser("source-notes", help="List complete Source Notes")
    review_release = sub.add_parser(
        "review-release", help="Re-run only Source-level editorial review"
    )
    review_release.add_argument("job_id")
    show_source = sub.add_parser("show-source-note", help="Show a composed Source Note")
    show_source.add_argument("source_note_id")
    show = sub.add_parser("show", help="Show an active note and evidence")
    show.add_argument("note_id")
    search = sub.add_parser("search", help="Search active notes")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    ask = sub.add_parser("ask", help="Answer from active notes with evidence")
    ask.add_argument("question")
    ask.add_argument("--limit", type=int, default=5)
    sub.add_parser("rebuild-index", help="Rebuild FTS from active note versions")
    sub.add_parser(
        "backfill-source-notes",
        help="Compose Source Notes for active legacy atomic notes",
    )
    backbone = sub.add_parser(
        "backbone-register",
        help="Register an authoritative textbook and seed its complete Synthesis outline",
    )
    backbone.add_argument("pdf", type=Path)
    figures = sub.add_parser(
        "backbone-extract-figures",
        help="Extract source-faithful textbook figures and map them to Concepts",
    )
    figures.add_argument("corpus_id")
    translate_figures = sub.add_parser(
        "backbone-translate-figure-captions",
        help="Translate textbook Figure captions to Chinese and sync active notes",
    )
    translate_figures.add_argument("corpus_id")
    translate_figures.add_argument("--batch-size", type=int, default=16)
    translate_figures.add_argument("--concurrency", type=int, default=4)
    translate_figures.add_argument("--force", action="store_true")
    d2l_import = sub.add_parser(
        "d2l-import-source",
        help="Import the official D2L Markdown tree into SourceNote and AtomicNote storage",
    )
    d2l_import.add_argument("repository", type=Path)
    migrate_backbone = sub.add_parser(
        "backbone-migrate-source",
        help="Migrate one book-aligned Backbone into SourceNote and AtomicNote storage",
    )
    migrate_backbone.add_argument("corpus_id")
    semantic = sub.add_parser(
        "semantic-synthesis-build",
        help="Build the reusable Concept V3 catalog and semantic Synthesis",
    )
    semantic.add_argument("corpus_id")
    semantic.add_argument("source_version_id")
    semantic.add_argument("--concurrency", type=int, default=4)
    sub.add_parser(
        "semantic-synthesis-refresh",
        help="Repin active Syntheses to shared active Concept versions without an LLM call",
    )
    enrich = sub.add_parser(
        "backbone-enrich",
        help="Enrich seeded Concepts with the configured LLM API",
    )
    enrich.add_argument("corpus_id")
    enrich.add_argument("--limit", type=int)
    enrich.add_argument("--concurrency", type=int)
    enrich.add_argument(
        "--include-published",
        action="store_true",
        help="Allow targeted regeneration of already published Concepts",
    )
    enrich.add_argument(
        "--long-leaves-only",
        action="store_true",
        help="Only process leaf Concepts whose source section spans more than three pages",
    )
    sub.add_parser("syntheses", help="List long-term Synthesis knowledge systems")
    show_synthesis = sub.add_parser("show-synthesis", help="Show one Synthesis outline")
    show_synthesis.add_argument("synthesis_id")
    show_concept = sub.add_parser("show-concept", help="Show one active Concept version")
    show_concept.add_argument("concept_id")
    add_contribution = sub.add_parser(
        "concept-add-note",
        help="Explicitly accept one AtomicNote version into an existing Concept module",
    )
    add_contribution.add_argument("concept_id")
    add_contribution.add_argument("module_key")
    add_contribution.add_argument("note_version_id")
    add_contribution.add_argument("expected_concept_version_id")
    add_contribution.add_argument("--rationale", required=True)
    show_concept_update = sub.add_parser(
        "show-concept-update", help="Show one durable Concept update job"
    )
    show_concept_update.add_argument("job_id")
    export = sub.add_parser("export", help="Export active notes to local Markdown")
    export.add_argument("source_version_id", nargs="?")
    serve_internal = sub.add_parser(
        "serve-internal",
        help="Run the domain-only API for DeepSeek Harness",
    )
    serve_internal.add_argument("--host", default="127.0.0.1")
    serve_internal.add_argument("--port", type=int, default=8000)
    worker = sub.add_parser("worker", help="Run the persisted background job worker")
    worker.add_argument("--worker-id")
    worker.add_argument("--poll-interval", type=float)
    worker.add_argument("--lease-seconds", type=int)
    worker.add_argument("--heartbeat-seconds", type=int)
    worker.add_argument("--once", action="store_true")
    sub.add_parser("db-upgrade", help="Upgrade the SQLite schema with backup")
    sub.add_parser("db-status", help="Show database and migration versions")
    next_init = sub.add_parser(
        "next-init", help="Initialize an isolated next-v1 store; refuses legacy data"
    )
    next_init.add_argument("--data-dir", type=Path, required=True)
    sub.add_parser("next-schema", help="Print runtime next-v1 candidate and review JSON Schemas")
    from .next.cli import configure

    configure(sub)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command in {"next-ingest", "next-worker", "next-status", "next-retry", "next-verify"}:
        from .next.cli import run

        run(args, _print)
        return
    if args.command == "next-schema":
        from .next.contracts import Candidate, Review

        _print(
            {
                "contract": "next-v1",
                "candidate": Candidate.model_json_schema(),
                "review": Review.model_json_schema(),
            }
        )
        return
    if args.command == "next-init":
        from .next.store import Store

        store = Store(args.data_dir)
        try:
            _print(
                {"contract": store.contract, "data_dir": str(store.root), **store.db.last_migration}
            )
        finally:
            store.close()
        return
    if args.command in {"db-upgrade", "db-status"}:
        from .db import Database
        from .storage.runner import status

        settings = Settings.from_env()
        settings.ensure_directories()
        database = Database(settings.database_url)
        try:
            if args.command == "db-upgrade":
                database.init()
                _print(database.last_migration)
            else:
                _print(status(database.engine))
        finally:
            database.close()
        return
    if args.command == "create-demo":
        _print(_create_demo(args.dir.resolve()))
        return
    if args.command == "serve-internal":
        import uvicorn

        app_path = "noodle_agent.internal_api:create_app"
        uvicorn.run(app_path, factory=True, host=args.host, port=args.port)
        return
    service = _service()
    if args.command == "init":
        _print({"status": "initialized", "data_dir": str(service.settings.data_dir)})
    elif args.command == "doctor":
        _print(
            {
                "database": service.db.healthcheck(),
                "data_dir": str(service.settings.data_dir),
                "parser": service.parser.name,
                "mineru_mode": service.settings.mineru_mode,
                "knowledge_agent": service.agent.name,
                "llm_fast_model": service.settings.llm_fast_model,
                "llm_pro_model": service.settings.llm_pro_model,
                "embedding": service.embedding.model_name,
                "embedding_dimensions": getattr(service.embedding, "dimensions", None),
                "embedding_key_configured": bool(service.settings.embedding_api_key),
                "llm_key_configured": bool(service.settings.llm_api_key),
                "mineru_token_configured": bool(service.settings.mineru_api_token),
            }
        )
    elif args.command == "process":
        result = service.ingest_pdf(
            args.pdf,
            title=args.title,
            source_id=args.source_id,
            force_new_version=args.new_version,
        )
        if not args.no_wait:
            result["job"] = service.run_job_until_terminal(result["job_id"])
        _print(result)
    elif args.command == "run":
        if args.restart_from:
            service.restart_generation(args.job_id, args.restart_from)
        elif args.resume:
            service.resume_job(args.job_id)
        result = (
            service.run_job_until_terminal(args.job_id, timeout_seconds=args.timeout)
            if args.wait
            else service.run_job_once(args.job_id, force_poll=True)
        )
        _print(result)
    elif args.command == "notes":
        _print(service.list_notes())
    elif args.command == "source-notes":
        _print(service.list_source_notes())
    elif args.command == "review-release":
        _print(service.review_existing_release(args.job_id))
    elif args.command == "show-source-note":
        _print(service.get_source_note(args.source_note_id))
    elif args.command == "show":
        _print(service.get_note(args.note_id))
    elif args.command == "search":
        _print([hit.model_dump() for hit in service.search(args.query, limit=args.limit)])
    elif args.command == "ask":
        _print(service.ask(args.question, limit=args.limit).model_dump())
    elif args.command == "rebuild-index":
        _print({"indexed_notes": service.rebuild_index()})
    elif args.command == "backfill-source-notes":
        _print(service.backfill_source_notes())
    elif args.command == "backbone-register":
        _print(service.register_backbone(args.pdf))
    elif args.command == "backbone-extract-figures":
        _print(service.extract_backbone_figures(args.corpus_id))
    elif args.command == "backbone-translate-figure-captions":
        _print(
            service.translate_backbone_figure_captions(
                args.corpus_id,
                batch_size=args.batch_size,
                concurrency=args.concurrency,
                force=args.force,
            )
        )
    elif args.command == "d2l-import-source":
        _print(service.import_d2l_source(args.repository))
    elif args.command == "backbone-migrate-source":
        _print(service.migrate_backbone_source(args.corpus_id))
    elif args.command == "semantic-synthesis-build":
        _print(
            service.build_semantic_synthesis(
                args.corpus_id,
                args.source_version_id,
                concurrency=args.concurrency,
            )
        )
    elif args.command == "semantic-synthesis-refresh":
        _print(service.refresh_shared_concept_versions())
    elif args.command == "backbone-enrich":
        _print(
            service.enrich_backbone(
                args.corpus_id,
                limit=args.limit,
                concurrency=args.concurrency,
                include_published=args.include_published,
                long_leaves_only=args.long_leaves_only,
            )
        )
    elif args.command == "syntheses":
        _print(service.list_syntheses())
    elif args.command == "show-synthesis":
        _print(service.get_synthesis(args.synthesis_id))
    elif args.command == "show-concept":
        _print(service.get_concept(args.concept_id))
    elif args.command == "concept-add-note":
        _print(
            service.accept_atomic_note_contribution(
                concept_id=args.concept_id,
                module_key=args.module_key,
                note_version_id=args.note_version_id,
                expected_concept_version_id=args.expected_concept_version_id,
                rationale=args.rationale,
                actor="cli-user",
            )
        )
    elif args.command == "show-concept-update":
        _print(service.get_concept_update_job(args.job_id))
    elif args.command == "export":
        _print(service.export_markdown(args.source_version_id))
    elif args.command == "worker":
        worker = JobWorker(
            service,
            worker_id=args.worker_id,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
        )
        if args.once:
            _print(worker.run_once() or {"status": "IDLE", "worker_id": worker.worker_id})
        else:
            _print({"status": "STARTED", "worker_id": worker.worker_id})
            try:
                worker.run_forever(poll_seconds=args.poll_interval)
            except KeyboardInterrupt:
                _print({"status": "STOPPED", "worker_id": worker.worker_id})


if __name__ == "__main__":
    main()
