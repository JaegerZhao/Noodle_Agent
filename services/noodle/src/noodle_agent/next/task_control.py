"""Durable chapter-boundary pause; the current chapter is allowed to finish."""

import json

from sqlalchemy import text

from .jobs import require_lease
from .repository import Conflict, identifier, insert, now, one


class PauseRequested(Exception):
    pass


def control(store, job_id):
    with store.db.engine.connect() as c:
        row = one(c, "SELECT * FROM job_controls WHERE job_id=:j", j=job_id)
    return dict(row) if row else {"job_id": job_id, "state": "RUN"}


def record(c, job_id, state):
    insert(
        c,
        "job_stage_events",
        id=identifier(),
        job_id=job_id,
        stage="control." + state,
        detail_json="{}",
        created_at=now(),
    )


def request_pause(store, job_id):
    with store.repository.transaction() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:j", j=job_id)
        if not job or job["kind"] != "GENERATE_ATOMICS":
            raise ValueError("Unknown generation task")
        old = one(c, "SELECT * FROM job_controls WHERE job_id=:j", j=job_id)
        if old and old["state"] in {"PAUSED", "PAUSE_REQUESTED"}:
            return dict(old)
        if job["status"] == "SUCCEEDED":
            raise Conflict("Task is already completed")
        state = "PAUSE_REQUESTED" if job["status"] == "RUNNING" else "PAUSED"
        c.execute(
            text(
                "INSERT INTO job_controls(job_id,state,updated_at,requested_at) VALUES(:j,:s,:t,:t) ON CONFLICT(job_id) DO UPDATE SET state=:s,requested_at=:t,updated_at=:t"
            ),
            {"j": job_id, "s": state, "t": now()},
        )
        record(c, job_id, "paused" if state == "PAUSED" else "pause-requested")
        if state == "PAUSED":
            c.execute(
                text(
                    "UPDATE jobs SET status='PAUSED',lease_owner=NULL,lease_until=NULL WHERE id=:j"
                ),
                {"j": job_id},
            )
    return control(store, job_id)


def enter_chapter(store, lease, chapter_id, title):
    with store.repository.transaction() as c:
        require_lease(c, lease)
        old = one(c, "SELECT * FROM job_controls WHERE job_id=:j", j=lease.job_id)
        if old and old["state"] != "RUN":
            raise PauseRequested()
        c.execute(
            text(
                "INSERT INTO job_controls(job_id,state,chapter_id,chapter_title,updated_at) VALUES(:j,'RUN',:id,:title,:t) ON CONFLICT(job_id) DO UPDATE SET chapter_id=:id,chapter_title=:title,updated_at=:t"
            ),
            {"j": lease.job_id, "id": chapter_id, "title": title, "t": now()},
        )


def boundary(store, lease):
    if control(store, lease.job_id)["state"] != "RUN":
        raise PauseRequested()


def park(store, lease):
    with store.repository.transaction() as c:
        require_lease(c, lease)
        record(c, lease.job_id, "paused")
        c.execute(
            text("UPDATE job_controls SET state='PAUSED',updated_at=:t WHERE job_id=:j"),
            {"t": now(), "j": lease.job_id},
        )
        c.execute(
            text(
                "UPDATE jobs SET status='PAUSED',attempts=max(0,attempts-1),error_json='{}',lease_owner=NULL,lease_until=NULL WHERE id=:j"
            ),
            {"j": lease.job_id},
        )


def resume(store, job_id):
    with store.repository.transaction() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:j", j=job_id)
        old = one(c, "SELECT * FROM job_controls WHERE job_id=:j", j=job_id)
        if (
            old
            and old["state"] == "RUN"
            and job
            and job["status"] in {"RUNNING", "PENDING", "RETRY"}
        ):
            return {"state": "RUN"}
        legacy = (
            job
            and job["status"] == "REVIEW"
            and json.loads(job["error_json"]).get("code") == "USER_PAUSED"
        )
        if not job or (job["status"] != "PAUSED" and not legacy):
            raise Conflict("Only a paused generation task can resume")
        c.execute(
            text(
                "INSERT INTO job_controls(job_id,state,updated_at) VALUES(:j,'RUN',:t) ON CONFLICT(job_id) DO UPDATE SET state='RUN',requested_at=NULL,updated_at=:t"
            ),
            {"j": job_id, "t": now()},
        )
        c.execute(
            text("UPDATE jobs SET status='PENDING',attempts=0,error_json='{}' WHERE id=:j"),
            {"j": job_id},
        )
        record(c, job_id, "resumed")
    return {"state": "RUN"}
