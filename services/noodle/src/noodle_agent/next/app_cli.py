"""Current application commands shared by the workspace and standalone CLI."""

import os
import time
from pathlib import Path

from ..config import Settings
from .events import bootstrap, request_reconcile
from .retrieval.contracts import Query
from .runtime import Application, backend_from_env, real_agent

COMMANDS = {
    "next-bootstrap",
    "next-maintain",
    "next-index",
    "next-search",
    "next-ask",
    "next-api",
    "next-reconcile",
    "next-vault",
    "next-export",
    "next-backup",
    "next-restore",
    "next-run",
    "next-outline",
    "next-index-retention",
}


def configure(sub):
    for name in sorted(COMMANDS):
        parser = sub.add_parser(name)
        parser.add_argument("--data-dir", type=Path, required=True)
        if name == 'next-index-retention':
            parser.add_argument('--keep', type=int, default=3)
            parser.add_argument('--min-age-days', type=int, default=7)
            parser.add_argument('--protect-generation', action='append', default=[])
            parser.add_argument('--apply', action='store_true')
        if name == "next-outline":
            parser.add_argument("--source-id", required=True)
            parser.add_argument("--output", type=Path)
        if name in {"next-export", "next-backup"}:
            parser.add_argument("--output", type=Path, required=True)
            parser.add_argument("--public", action="store_true")
            parser.add_argument("--standard", action="store_true")
        if name == "next-restore":
            parser.add_argument("--input", type=Path, required=True)
            parser.add_argument("--destination", type=Path, required=True)
        if name == "next-vault":
            parser.add_argument("--import-edits", action="store_true")
            parser.add_argument("--apply", action="store_true")
        if name == "next-run":
            parser.add_argument("--watch-vault", action="store_true")
        if name == "next-api":
            parser.add_argument("--host", default="127.0.0.1")
            parser.add_argument("--port", type=int, default=8002)
        if name == "next-maintain":
            parser.add_argument("--once", action="store_true")
            parser.add_argument("--until-idle", action="store_true")
            parser.add_argument(
                "--max-steps", type=int, help="Optional positive bound; default runs continuously"
            )
        if name == "next-bootstrap":
            parser.add_argument("--note-id", action="append")
        if name in {"next-maintain", "next-index", "next-search", "next-ask"}:
            parser.add_argument(
                "--mode",
                choices=["HYBRID", "KEYWORD_ONLY"],
                default=os.getenv("NOODLE_NEXT_RETRIEVAL_MODE", "HYBRID"),
            )
        if name in {"next-search", "next-ask"}:
            parser.add_argument("--query", required=True)
            parser.add_argument("--rerank", choices=["AUTO", "OFF", "REQUIRED"], default="AUTO")
            parser.add_argument("--source-id")
            parser.add_argument("--note-id", action="append")
            parser.add_argument("--top-k", type=int, default=6)


def run(args, store, emit):
    if args.command == 'next-index-retention':
        from .retrieval.retention import apply, plan
        options = {'keep': args.keep, 'min_age_days': args.min_age_days, 'protected': args.protect_generation}
        preview = plan(store, **options)
        emit(apply(store, preview, **options) if args.apply else preview)
        return
    if args.command == "next-outline":
        import json

        from .repository import one
        from .source_structure import source_structure

        with store.db.engine.connect() as c:
            source = one(
                c, "SELECT current_revision_id FROM sources WHERE id=:id", id=args.source_id
            )
        if not source:
            raise ValueError("Unknown source")
        result = source_structure(store, source["current_revision_id"])
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            emit(
                {
                    "path": str(args.output.resolve()),
                    "planning_status": result["planning_status"],
                    "counts": result["counts"],
                }
            )
        else:
            emit(result)
        return
    if args.command == "next-vault":
        from .vault import import_vault, write_vault

        emit(import_vault(store, apply=args.apply) if args.import_edits else write_vault(store))
        return
    if args.command in {"next-export", "next-backup"}:
        from .backup import recovery_zip
        from .vault import reading_zip

        content = (
            recovery_zip(store)
            if args.command == "next-backup"
            else reading_zip(store, public=args.public, obsidian=not args.standard)
        )
        if args.output.exists():
            raise ValueError("Choose a new output file; existing exports are not overwritten")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as stream:
            stream.write(content)
        emit({"path": str(args.output.resolve()), "bytes": len(content)})
        return
    if args.command == "next-restore":
        from .backup import restore_package

        emit(restore_package(args.input.read_bytes(), args.destination))
        return
    if args.command == "next-run":
        from .workspace_worker import run_workspace

        run_workspace(store, watch_vault=args.watch_vault, emit=emit)
        return
    if args.command == "next-bootstrap":
        emit({"registered": bootstrap(store, args.note_id)})
        return
    if args.command == "next-reconcile":
        request_reconcile(store)
        emit({"status": "QUEUED"})
        return
    if args.command == "next-api":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(store.root), host=args.host, port=args.port)
        return
    settings = Settings.from_env()
    try:
        agent = real_agent(settings)
    except ValueError:
        agent = None
    application = Application(
        store, settings=settings, agent=agent, backend=backend_from_env(), index_mode=args.mode
    )
    if args.command == "next-index":
        emit(application.indexer.build(args.mode))
        return
    if args.command in {"next-search", "next-ask"}:
        if args.source_id and args.note_id:
            raise ValueError("Choose source or note scope, not both")
        scope = (
            {"mode": "SOURCE", "source_ids": [args.source_id]}
            if args.source_id
            else {"mode": "NOTES", "note_ids": args.note_id}
            if args.note_id
            else {"mode": "GLOBAL"}
        )
        query = Query(
            query=args.query,
            scope=scope,
            allowed_kinds=["ATOMIC"] if args.source_id else ["ATOMIC", "CONCEPT"],
            retrieval_mode=args.mode,
            top_k=args.top_k,
            rerank=args.rerank,
        )
        emit(
            application.answerer.answer(query)
            if args.command == "next-ask"
            else application.searcher.search(query)
        )
        return
    failures = 0
    if args.max_steps is not None and args.max_steps < 1:
        raise ValueError("--max-steps must be positive")
    step = 0
    while args.max_steps is None or step < args.max_steps:
        step += 1
        result = application.run_once()
        if result:
            emit(result)
            failures += result["status"] == "REVIEW"
        elif args.until_idle or args.once:
            break
        else:
            time.sleep(1)
        if args.once:
            break
    if failures:
        raise SystemExit(1)
