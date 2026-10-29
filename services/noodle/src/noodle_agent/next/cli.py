"""Current generation, recovery and inspection commands for next-v1 stores."""

import json
import time
from pathlib import Path

from ..adapters.mineru import validate_pdf
from ..config import Settings
from .repository import Conflict, one, rows
from .store import Store
from .worker import GenerationWorker


def configure(sub):
    from .app_cli import configure as configure_application

    configure_application(sub)
    ingest = sub.add_parser("next-ingest", help="Register a PDF in an isolated next-v1 store")
    ingest.add_argument("--data-dir", type=Path, required=True)
    ingest.add_argument("--pdf", type=Path, required=True)
    ingest.add_argument("--title")
    worker = sub.add_parser(
        "next-worker", help="Run next-v1 PDF generation with configured real providers"
    )
    worker.add_argument("--data-dir", type=Path, required=True)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--worker-id", default="next-cli")
    retry = sub.add_parser(
        "next-retry", help="Explicitly resume an inspected REVIEW job without removing receipts"
    )
    retry.add_argument("--data-dir", type=Path, required=True)
    retry.add_argument("--job-id", required=True)
    rebuild = sub.add_parser(
        "next-rebuild",
        help="Rebuild all atomics of a completed job, retaining parse and plan receipts",
    )
    rebuild.add_argument("--data-dir", type=Path, required=True)
    rebuild.add_argument("--job-id", required=True)
    rebuild.add_argument("--reason", required=True)
    cancel = sub.add_parser(
        "next-cancel-rebuild", help="Restore the completed result before an unfinished rebuild"
    )
    cancel.add_argument("--data-dir", type=Path, required=True)
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--reason", required=True)
    verify = sub.add_parser("next-verify", help="Independently verify a completed generation job")
    verify.add_argument("--data-dir", type=Path, required=True)
    verify.add_argument("--job-id", required=True)
    status = sub.add_parser("next-status", help="Read next-v1 jobs and stage receipts")
    status.add_argument("--data-dir", type=Path, required=True)


def run(args, emit):
    store = Store(args.data_dir)
    try:
        from .app_cli import COMMANDS
        from .app_cli import run as run_application

        if args.command in COMMANDS:
            return run_application(args, store, emit)
        if args.command == "next-cancel-rebuild":
            from .recovery import cancel_rebuild

            emit(cancel_rebuild(store, args.job_id, reason=args.reason))
            return
        if args.command == "next-rebuild":
            with store.db.engine.connect() as c:
                job = one(c, "SELECT * FROM jobs WHERE id=:id", id=args.job_id)
            if not job or job["kind"] != GenerationWorker.kind or job["status"] != "SUCCEEDED":
                raise Conflict("Rebuild requires a completed atomic-generation job")
            outputs = json.loads(job["result_json"])["notes"]
            store.jobs.reopen_outputs(
                args.job_id,
                [(o["note_id"], o["revision_id"]) for o in outputs],
                reason=args.reason,
                rebuild=True,
            )
            # If interrupted between these transactions, next-retry safely resumes REVIEW.
            store.jobs.retry_review(args.job_id)
            emit(
                {
                    "job_id": args.job_id,
                    "status": "PENDING",
                    "rebuild_notes": len(outputs),
                    "reuse": "verified source, parse and plan; no prior note body",
                }
            )
            return
        if args.command == "next-verify":
            from .verification import verify_generation

            emit(verify_generation(store, args.job_id))
            return
        if args.command == "next-retry":
            store.jobs.retry_review(args.job_id)
            emit({"job_id": args.job_id, "status": "PENDING", "checkpoints": "preserved"})
            return
        if args.command == "next-ingest":
            validate_pdf(args.pdf)
            result = store.add_source(
                args.pdf.read_bytes(), args.title or args.pdf.stem, original_name=args.pdf.name
            )
            emit(result)
            return
        if args.command == "next-status":
            with store.db.engine.connect() as c:
                jobs = [
                    dict(r)
                    for r in rows(
                        c,
                        "SELECT id,kind,status,attempts,result_json,error_json FROM jobs ORDER BY created_at",
                    )
                ]
                stages = [
                    dict(r)
                    for r in rows(
                        c, "SELECT job_id,stage,state FROM job_checkpoints ORDER BY created_at"
                    )
                ]
                notes = [
                    dict(r)
                    for r in rows(
                        c,
                        "SELECT id,kind,title,current_revision_id,stable_path FROM notes ORDER BY created_at",
                    )
                ]
            emit({"jobs": jobs, "checkpoints": stages, "notes": notes})
            return
        settings = Settings.from_env()
        worker = GenerationWorker(
            store,
            settings=settings,
            progress=lambda stage, info: print(
                json.dumps({"stage": stage, **info}, ensure_ascii=False), flush=True
            ),
        )
        while True:
            outcome = worker.run_once(args.worker_id)
            if outcome:
                emit(outcome)
            if args.once:
                if outcome and outcome["status"] in {"REVIEW", "RETRY"}:
                    raise SystemExit(1)
                return
            time.sleep(
                max(1, settings.mineru_poll_seconds)
                if outcome and outcome["status"] == "PENDING"
                else 1
            )
    finally:
        store.close()
