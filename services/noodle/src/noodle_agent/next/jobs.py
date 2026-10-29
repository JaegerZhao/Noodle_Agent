"""Durable outbox dispatch and fenced job leases shared by next-v1 workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from .contracts import canonical, sha
from .repository import Conflict, Repository, identifier, insert, now, one, rows


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    input: dict


def require_lease(connection, lease: Lease):
    job = one(
        connection,
        """SELECT * FROM jobs WHERE id=:id AND lease_owner=:token
        AND lease_until>:now AND status='RUNNING'""",
        id=lease.job_id,
        token=lease.token,
        now=now(),
    )
    if not job:
        raise Conflict("Job lease expired or was replaced")
    return job


class JobQueue:
    def __init__(self, repository: Repository):
        self.repository = repository

    def enqueue(self, event_id: str, kind: str, payload: dict) -> str:
        with self.repository.transaction() as connection:
            event = one(connection, "SELECT * FROM events WHERE id=:id", id=event_id)
            if not event:
                raise ValueError("Unknown event")
            existing = one(
                connection,
                "SELECT id FROM jobs WHERE event_id=:id AND kind=:kind",
                id=event_id,
                kind=kind,
            )
            if existing:
                return existing["id"]
            if event["status"] == "COMPLETED":
                raise ValueError("Completed event cannot accept new job kinds")
            job_id = identifier()
            connection.execute(
                text("""INSERT INTO jobs(id,event_id,kind,status,input_json,created_at)
                VALUES(:id,:event,:kind,'PENDING',:payload,:created)"""),
                {
                    "id": job_id,
                    "event": event_id,
                    "kind": kind,
                    "payload": canonical(payload),
                    "created": now(),
                },
            )
            return job_id

    def dispatch(self, routes: dict[str, list[str]], *, limit: int = 100) -> int:
        if limit < 1:
            raise ValueError("Dispatch limit must be positive")
        dispatched = 0
        with self.repository.transaction() as connection:
            supported = [name for name, kinds in routes.items() if kinds]
            if not supported:
                return 0
            params = {f"type_{i}": name for i, name in enumerate(supported)}
            placeholders = ",".join(f":{name}" for name in params)
            events = rows(
                connection,
                f"SELECT * FROM events WHERE status='QUEUED' AND event_type IN ({placeholders}) ORDER BY created_at LIMIT :limit",
                **params,
                limit=limit,
            )
            for event in events:
                kinds = routes.get(event["event_type"], [])
                if callable(kinds):
                    kinds = kinds(connection, event)
                if not kinds:
                    connection.execute(
                        text("UPDATE events SET status='COMPLETED' WHERE id=:id"),
                        {"id": event["id"]},
                    )
                    continue
                payload = {
                    "event_id": event["id"],
                    "entity_id": event["entity_id"],
                    "revision_id": event["revision_id"],
                    "payload": json.loads(event["payload_json"]),
                }
                for kind in set(kinds):
                    connection.execute(
                        text("""INSERT OR IGNORE INTO jobs(id,event_id,kind,status,input_json,created_at)
                        VALUES(:id,:event,:kind,'PENDING',:payload,:created)"""),
                        {
                            "id": identifier(),
                            "event": event["id"],
                            "kind": kind,
                            "payload": canonical(payload),
                            "created": now(),
                        },
                    )
                connection.execute(
                    text("UPDATE events SET status='DISPATCHED' WHERE id=:id"), {"id": event["id"]}
                )
                dispatched += 1
        return dispatched

    def claim(
        self, worker: str, *, kind: str, lease_seconds: int = 180, max_attempts: int = 3,
        ready=None,
    ) -> Lease | None:
        if not worker or not kind or lease_seconds < 1 or max_attempts < 1:
            raise ValueError("Worker, kind and positive lease are required")
        token = f"{worker}:{identifier()}"
        timestamp = now()
        expires = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self.repository.transaction() as connection:
            connection.execute(
                text("""UPDATE jobs SET status='REVIEW',lease_owner=NULL,lease_until=NULL,
                error_json=:error WHERE kind=:kind AND attempts>=:maximum AND
                (status IN ('PENDING','RETRY') OR (status='RUNNING' AND lease_until<=:now))"""),
                {
                    "error": canonical({"code": "ATTEMPTS_EXHAUSTED"}),
                    "kind": kind,
                    "maximum": max_attempts,
                    "now": timestamp,
                },
            )
            candidates = rows(
                connection,
                """SELECT * FROM jobs WHERE kind=:kind AND attempts<:maximum AND
                (status IN ('PENDING','RETRY') OR (status='RUNNING' AND lease_until<=:now))
                ORDER BY created_at""",
                kind=kind,
                maximum=max_attempts,
                now=timestamp,
            )
            job = None
            from .metrics import root_for

            for candidate in candidates:
                if kind in {"BUILD_CONCEPT", "BUILD_INDEX", "REVIEW_ORGANIZATION"}:
                    busy = one(connection, """SELECT id FROM jobs WHERE kind=:kind AND id!=:id
                        AND status='RUNNING' AND lease_until>:n AND
                        (:kind IN ('BUILD_INDEX','REVIEW_ORGANIZATION') OR json_extract(input_json,'$.entity_id')=:entity) LIMIT 1""",
                        kind=kind, id=candidate["id"], n=timestamp,
                        entity=json.loads(candidate["input_json"])["entity_id"])
                    if busy:
                        continue
                if ready is not None and not ready(connection, candidate):
                    continue
                root = root_for(connection, candidate["id"])
                control = (
                    one(connection, "SELECT state FROM job_controls WHERE job_id=:j", j=root)
                    if root
                    else None
                )
                if (
                    control
                    and control["state"] != "RUN"
                    and kind
                    in {"ANALYZE_ATOMIC", "BUILD_CONCEPT", "BUILD_INDEX", "GENERATE_ATOMICS"}
                ):
                    continue
                job = candidate
                break
            if not job:
                return None
            connection.execute(
                text("""UPDATE jobs SET status='RUNNING',lease_owner=:token,
                lease_until=:expires,attempts=attempts+1 WHERE id=:id"""),
                {"id": job["id"], "token": token, "expires": expires},
            )
            insert(connection, 'job_stage_events', id=identifier(), job_id=job['id'], stage='execution.attempt-start',
                   detail_json=canonical({'attempt_id':token,'kind':kind,'attempt':job['attempts']+1}), created_at=timestamp)
            return Lease(job["id"], token, json.loads(job["input_json"]))

    def heartbeat(self, lease: Lease, *, lease_seconds: int = 180):
        if lease_seconds < 1:
            raise ValueError("Lease must be positive")
        with self.repository.transaction() as connection:
            require_lease(connection, lease)
            expires = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                text("UPDATE jobs SET lease_until=:expires WHERE id=:id"),
                {"id": lease.job_id, "expires": expires},
            )

    def finish(self, lease: Lease, result: dict):
        with self.repository.transaction() as connection:
            job = require_lease(connection, lease)
            insert(connection, 'job_stage_events', id=identifier(), job_id=lease.job_id, stage='execution.attempt-end',
                   detail_json=canonical({'attempt_id':lease.token,'status':'SUCCEEDED'}), created_at=now())
            connection.execute(
                text("""UPDATE jobs SET status='SUCCEEDED',result_json=:result,error_json='{}',
                lease_owner=NULL,lease_until=NULL WHERE id=:id"""),
                {"id": lease.job_id, "result": canonical(result)},
            )
            pending = one(
                connection,
                "SELECT id FROM jobs WHERE event_id=:event AND status!='SUCCEEDED' LIMIT 1",
                event=job["event_id"],
            )
            if not pending:
                connection.execute(
                    text("UPDATE events SET status='COMPLETED' WHERE id=:id"),
                    {"id": job["event_id"]},
                )

    def fail(self, lease: Lease, error: dict, *, retry: bool, max_attempts: int = 3):
        from .provider_errors import provider_issue
        issue = provider_issue(error)
        if issue:
            error = {**error, 'provider_issue': issue}
            retry = False
        if max_attempts < 1:
            raise ValueError("Attempt limit must be positive")
        with self.repository.transaction() as connection:
            job = require_lease(connection, lease)
            status = "RETRY" if retry and job["attempts"] < max_attempts else "REVIEW"
            insert(connection, 'job_stage_events', id=identifier(), job_id=lease.job_id, stage='execution.attempt-end',
                   detail_json=canonical({'attempt_id':lease.token,'status':status}), created_at=now())
            control = one(
                connection, "SELECT state FROM job_controls WHERE job_id=:j", j=lease.job_id
            )
            if (
                job["kind"] == "GENERATE_ATOMICS"
                and control
                and control["state"] == "PAUSE_REQUESTED"
            ):
                connection.execute(
                    text("UPDATE job_controls SET state='PAUSED',updated_at=:t WHERE job_id=:j"),
                    {"j": lease.job_id, "t": now()},
                )
                if retry:
                    status = "PAUSED"
            connection.execute(
                text("""UPDATE jobs SET status=:status,error_json=:error,
                lease_owner=NULL,lease_until=NULL WHERE id=:id"""),
                {
                    "id": lease.job_id,
                    "status": status,
                    "error": canonical(error),
                },
            )
            if status == 'REVIEW' and job['kind'] == 'GENERATE_ATOMICS':
                insert(connection, 'job_stage_events', id=identifier(), job_id=lease.job_id,
                    stage='execution.blocked', detail_json=canonical({'category': issue['category'] if issue else 'REVIEW',
                        'code':error.get('code')}), created_at=now())
            return status

    def defer(self, lease: Lease, progress: dict):
        """Polling is progress, not a failed attempt; retain the same durable job."""
        with self.repository.transaction() as c:
            require_lease(c, lease)
            insert(c, 'job_stage_events', id=identifier(), job_id=lease.job_id, stage='execution.attempt-end',
                   detail_json=canonical({'attempt_id':lease.token,'status':'DEFERRED','deferred_stage':progress.get('stage')}), created_at=now())
            c.execute(
                text("""UPDATE jobs SET status='PENDING',result_json=:progress,
                attempts=attempts-1,lease_owner=NULL,lease_until=NULL WHERE id=:id"""),
                {"progress": canonical(progress), "id": lease.job_id},
            )

    def retry_review(self, job_id: str):
        """Explicit operator retry preserving checkpoints and audit history; never auto-loop."""
        with self.repository.transaction() as c:
            job = one(c, "SELECT * FROM jobs WHERE id=:id", id=job_id)
            partial = job and job['kind'] == 'GENERATE_ATOMICS' and job['status'] == 'SUCCEEDED' and json.loads(job['result_json']).get('coverage', {}).get('failed_plans')
            paused = job and job['kind'] == 'GENERATE_ATOMICS' and job['status'] == 'PAUSED'
            if not job or job["status"] != "REVIEW" and not partial and not paused:
                raise Conflict("Only a REVIEW, paused or partially completed generation job can be explicitly resumed")
            self.repository.event(
                c,
                "JOB_RETRY_REQUESTED",
                job_id,
                None,
                {
                    "previous_error": json.loads(job["error_json"]),
                    "previous_attempts": job["attempts"],
                    "checkpoint_watermark": c.execute(
                        text("SELECT coalesce(max(rowid),0) FROM job_checkpoints WHERE job_id=:j"),
                        {"j": job_id},
                    ).scalar_one(),
                },
                key=f"job-retry:{job_id}:{identifier()}",
            )
            c.execute(
                text(
                    "UPDATE jobs SET status='PENDING',attempts=0,error_json='{}',lease_owner=NULL,lease_until=NULL WHERE id=:id"
                ),
                {"id": job_id},
            )
            if job["kind"] == "GENERATE_ATOMICS":
                if job['status'] == 'REVIEW':
                    insert(c, 'job_stage_events', id=identifier(), job_id=job_id,
                        stage='execution.resumed', detail_json=canonical({}), created_at=now())
                old_control = one(c,'SELECT state FROM job_controls WHERE job_id=:j',j=job_id)
                if old_control and old_control['state'] != 'RUN':
                    from .task_control import record
                    record(c,job_id,'resumed')
                c.execute(
                    text(
                        "UPDATE job_controls SET state='RUN',requested_at=NULL,updated_at=:t WHERE job_id=:j"
                    ),
                    {"j": job_id, "t": now()},
                )

    def reopen_invalid_output(self, job_id, note_id, revision_id, *, reason):
        self.reopen_outputs(job_id, [(note_id, revision_id)], reason=reason)

    def reopen_outputs(self, job_id, outputs, *, reason, rebuild=False):
        """Withdraw a checked output set and reopen its job in one transaction."""
        outputs = list(outputs)
        if not outputs or len({n for n, _ in outputs}) != len(outputs) or not reason.strip():
            raise ValueError("A distinct nonempty output set and reason are required")
        with self.repository.transaction() as c:
            job = one(c, "SELECT * FROM jobs WHERE id=:id", id=job_id)
            if not job or job["status"] != "SUCCEEDED":
                raise Conflict("Only a completed job can reopen an output set")
            baseline = json.loads(job["result_json"])
            rebuild_id = None
            if rebuild:
                expected = {(o["note_id"], o["revision_id"]) for o in baseline.get("notes", [])}
                if job["kind"] != "GENERATE_ATOMICS" or set(outputs) != expected:
                    raise Conflict("Full rebuild must match the completed output manifest")
                rebuild_id = identifier()
            for note_id, revision_id in outputs:
                note = one(c, "SELECT * FROM notes WHERE id=:id", id=note_id)
                proof = one(
                    c,
                    "SELECT id FROM build_artifacts WHERE job_id=:job AND storage_ref=:ref",
                    job=job_id,
                    ref=f"artifacts/builds/{revision_id}.json",
                )
                revision = one(c, "SELECT status FROM note_revisions WHERE id=:id", id=revision_id)
                if (
                    not note
                    or note["current_revision_id"] != revision_id
                    or note["visibility"] != "ACTIVE"
                    or not proof
                    or not revision
                    or revision["status"] != "PUBLISHED"
                ):
                    raise Conflict("Output changed before quality withdrawal")
            for note_id, revision_id in outputs:
                c.execute(
                    text("UPDATE note_revisions SET status='WITHDRAWN' WHERE id=:id"),
                    {"id": revision_id},
                )
                self.repository.event(
                    c,
                    "NOTE_INVALIDATED",
                    note_id,
                    revision_id,
                    {"reason": reason, "job_id": job_id, "rebuild_id": rebuild_id},
                    key=f"quality-withdraw:{revision_id}:{identifier()}",
                )
            c.execute(
                text("UPDATE jobs SET status='REVIEW',error_json=:error WHERE id=:id"),
                {"id": job_id, "error": canonical({"code": "QUALITY_RECHECK", "detail": reason})},
            )
            c.execute(
                text("UPDATE events SET status='DISPATCHED' WHERE id=:id"), {"id": job["event_id"]}
            )
            if rebuild:
                pending = {"request_id": rebuild_id, "baseline_sha256": sha(canonical(baseline))}
                c.execute(
                    text("UPDATE jobs SET result_json=:result WHERE id=:id"),
                    {"id": job_id, "result": canonical({**baseline, "pending_rebuild": pending})},
                )
            self.repository.advance_epoch(c)
