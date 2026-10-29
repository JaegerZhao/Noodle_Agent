"""Generation and maintenance run independently; one long PDF never starves updates."""

import threading
import time

from ..config import Settings
from .maintenance_pool import MaintenancePool
from .vault import import_vault, write_vault
from .worker import GenerationWorker


def record_acceptance(store, result):
    """A recovered job has a new report; never overwrite its earlier evidence."""
    from .contracts import canonical, sha
    from .verification import verify_generation

    try:
        report = verify_generation(store, result['job_id'])
    except Exception as exc:  # noqa: BLE001 - isolate read-back diagnostics from other sources
        report = {'job_id':result['job_id'],'status':'ENGINEERING_READBACK_FAILED','notes':[],
            'error_code':type(exc).__name__,'error':str(exc)[:2000]}
    data = canonical(report).encode()
    ref = f"artifacts/acceptance/{result['job_id']}/{sha(data)}.json"
    store.put(ref,data)
    return {**report,'report_ref':ref}


def run_workspace(store, *, watch_vault=False, emit=print):
    settings = Settings.from_env()
    stop = threading.Event()
    errors = []

    def generation():
        worker = GenerationWorker(store, settings=settings)
        try:
            while not stop.is_set():
                try:
                    result = worker.run_once("next-workspace-generation")
                except Exception as exc:
                    from .repository import storage_busy
                    if not storage_busy(exc):
                        raise
                    emit({'status':'WORKER_WAITING_STORAGE','detail':str(exc)[:1000]})
                    stop.wait(1)
                    continue
                if result:
                    emit(result)
                    if result["status"] == "SUCCEEDED":
                        report = record_acceptance(store,result)
                        emit(
                            {
                                "job_id": result["job_id"],
                                "status": report["status"],
                                "notes": len(report["notes"]),
                                "report_ref": report['report_ref'],
                                "human_acceptance": "PENDING",
                            }
                        )
                stop.wait(
                    max(1, settings.mineru_poll_seconds)
                    if result and result["status"] == "PENDING"
                    else 1
                )
        except Exception as exc:  # noqa: BLE001 - supervise the worker thread and surface its error
            errors.append(exc)
            stop.set()

    thread = threading.Thread(target=generation, name="next-generation", daemon=True)
    thread.start()
    maintenance = MaintenancePool(store, settings=settings, stop=stop, emit=emit).start()
    last_signature = None
    stable_since = 0
    last_error = None
    last_synced_signature = None
    last_projection = None
    try:
        while not stop.is_set():
            result = None
            if watch_vault:
                root = store.path("Vault")
                signature = (
                    tuple(
                        (str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                        for p in sorted(root.rglob("*.md"))
                        if p.is_file()
                        and not any(x.startswith(".") for x in p.relative_to(root).parts)
                    )
                    if root.exists()
                    else ()
                )
                if signature != last_signature:
                    stable_since = time.monotonic()
                    last_signature = signature
                with store.db.engine.connect() as c:
                    epoch = c.exec_driver_sql(
                        "SELECT validity_epoch FROM storage_contract WHERE id=1"
                    ).scalar_one()
                    revision_row = c.exec_driver_sql(
                        "SELECT count(*),max(created_at) FROM note_revisions"
                    ).one()
                    projection = (epoch, tuple(revision_row))
                if time.monotonic() - stable_since >= 2 and (
                    signature != last_synced_signature or projection != last_projection
                ):
                    try:
                        import_vault(store, apply=True)
                        write_vault(store)
                        last_synced_signature = tuple(
                            (str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                            for p in sorted(root.rglob("*.md"))
                            if p.is_file()
                            and not any(x.startswith(".") for x in p.relative_to(root).parts)
                        )
                        last_signature = last_synced_signature
                        last_projection = projection
                        last_error = None
                    except (ValueError, OSError) as exc:
                        message = str(exc)
                        if message != last_error:
                            emit({"status": "VAULT_REVIEW", "detail": message})
                        last_error = message
                    stable_since = time.monotonic()
            if not result:
                stop.wait(1)
        if errors:
            raise errors[0]
    except KeyboardInterrupt:
        stop.set()
        maintenance.close()
    finally:
        stop.set()
        # Running provider calls retain their publication lease fences. On process shutdown,
        # an unfinished job becomes reclaimable after its lease expires.
        thread.join(timeout=2)
