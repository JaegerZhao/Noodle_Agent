from __future__ import annotations

import os
import socket
import threading
import uuid
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from .db import ConceptUpdateJob, Job, utcnow
from .domain import JobStatus
from .services import TERMINAL_STATUSES, NoodleService


class JobWorker:
    """Single-process worker backed by recoverable SQLite job leases."""

    def __init__(
        self,
        service: NoodleService,
        *,
        worker_id: str | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: int | None = None,
    ):
        self.service = service
        self._prefer_sync = False
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.lease_seconds = max(
            30, lease_seconds or service.settings.worker_lease_seconds
        )
        self.heartbeat_seconds = max(
            5, heartbeat_seconds or service.settings.worker_heartbeat_seconds
        )

    def claim_next_job(self) -> str | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        with self.service.db.session() as session:
            candidate_ids = session.scalars(
                select(Job.id)
                .where(
                    Job.status.notin_(TERMINAL_STATUSES),
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                    or_(
                        Job.status.in_([JobStatus.CREATED.value, JobStatus.RUNNING.value]),
                        and_(
                            Job.status == JobStatus.WAITING_EXTERNAL.value,
                            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
                        ),
                    ),
                )
                .order_by(Job.created_at)
                .limit(8)
            ).all()
            for job_id in candidate_ids:
                claimed = session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status.notin_(TERMINAL_STATUSES),
                        or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                    )
                    .values(
                        lease_owner=self.worker_id,
                        lease_expires_at=lease_until,
                        heartbeat_at=now,
                    )
                )
                if claimed.rowcount == 1:
                    return job_id
        return None

    def heartbeat(self, job_id: str) -> bool:
        now = utcnow()
        with self.service.db.session() as session:
            refreshed = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.lease_owner == self.worker_id)
                .values(
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    heartbeat_at=now,
                )
            )
            return refreshed.rowcount == 1

    def release(self, job_id: str) -> None:
        with self.service.db.session() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.lease_owner == self.worker_id)
                .values(lease_owner=None, lease_expires_at=None, heartbeat_at=None)
            )

    def claim_next_concept_update(self) -> str | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.lease_seconds)
        runnable = [
            "QUEUED",
            "RUNNING",
            "GENERATING",
            "JUDGING",
            "REPAIRING",
            "REJUDGING",
            "READY",
            "PUBLISHING",
        ]
        with self.service.db.session() as session:
            candidate_ids = session.scalars(
                select(ConceptUpdateJob.id)
                .where(
                    ConceptUpdateJob.status.in_(runnable),
                    or_(
                        ConceptUpdateJob.lease_expires_at.is_(None),
                        ConceptUpdateJob.lease_expires_at <= now,
                    ),
                    or_(
                        ConceptUpdateJob.next_attempt_at.is_(None),
                        ConceptUpdateJob.next_attempt_at <= now,
                    ),
                )
                .order_by(ConceptUpdateJob.created_at)
                .limit(8)
            ).all()
            for job_id in candidate_ids:
                claimed = session.execute(
                    update(ConceptUpdateJob)
                    .where(
                        ConceptUpdateJob.id == job_id,
                        ConceptUpdateJob.status.in_(runnable),
                        or_(
                            ConceptUpdateJob.lease_expires_at.is_(None),
                            ConceptUpdateJob.lease_expires_at <= now,
                        ),
                    )
                    .values(
                        lease_owner=self.worker_id,
                        lease_expires_at=lease_until,
                        heartbeat_at=now,
                        attempt_count=ConceptUpdateJob.attempt_count + 1,
                    )
                )
                if claimed.rowcount == 1:
                    return job_id
        return None

    def heartbeat_concept_update(self, job_id: str) -> bool:
        now = utcnow()
        with self.service.db.session() as session:
            refreshed = session.execute(
                update(ConceptUpdateJob)
                .where(
                    ConceptUpdateJob.id == job_id,
                    ConceptUpdateJob.lease_owner == self.worker_id,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    heartbeat_at=now,
                )
            )
            return refreshed.rowcount == 1

    def release_concept_update(self, job_id: str) -> None:
        with self.service.db.session() as session:
            session.execute(
                update(ConceptUpdateJob)
                .where(
                    ConceptUpdateJob.id == job_id,
                    ConceptUpdateJob.lease_owner == self.worker_id,
                )
                .values(lease_owner=None, lease_expires_at=None, heartbeat_at=None)
            )

    def _run_concept_update_once(self, job_id: str) -> dict:
        stop_heartbeat = threading.Event()

        def keep_lease_alive() -> None:
            while not stop_heartbeat.wait(self.heartbeat_seconds):
                if not self.heartbeat_concept_update(job_id):
                    return

        heartbeat_thread = threading.Thread(
            target=keep_lease_alive,
            name=f"noodle-concept-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            try:
                return self.service.run_concept_update_job(job_id)
            except ValueError:
                return self.service.get_concept_update_job(job_id)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)
            self.release_concept_update(job_id)

    def _run_sync(self, event_id: str) -> dict:
        sync = self.service.knowledge_sync
        stop = threading.Event()

        def keep_alive() -> None:
            while not stop.wait(self.heartbeat_seconds):
                if not sync.heartbeat(event_id, self.worker_id):
                    return

        thread = threading.Thread(target=keep_alive, daemon=True)
        thread.start()
        try:
            return sync.run(event_id, self.worker_id)
        finally:
            stop.set()
            thread.join(timeout=2)

    def run_once(self) -> dict | None:
        sync = getattr(self.service, "knowledge_sync", None)
        if sync and self._prefer_sync:
            event_id = sync.claim(self.worker_id)
            if event_id:
                self._prefer_sync = False
                return self._run_sync(event_id)
        self._prefer_sync = True
        job_id = self.claim_next_job()
        if job_id is None:
            event_id = sync.claim(self.worker_id) if sync else None
            if event_id:
                return self._run_sync(event_id)
            concept_job_id = self.claim_next_concept_update()
            if concept_job_id is None:
                return None
            return self._run_concept_update_once(concept_job_id)
        stop_heartbeat = threading.Event()

        def keep_lease_alive() -> None:
            while not stop_heartbeat.wait(self.heartbeat_seconds):
                if not self.heartbeat(job_id):
                    return

        heartbeat_thread = threading.Thread(
            target=keep_lease_alive,
            name=f"noodle-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            return self.service.run_job_once(job_id)
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)
            self.release(job_id)

    def run_forever(
        self,
        *,
        poll_seconds: float | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        interval = max(
            0.2,
            poll_seconds
            if poll_seconds is not None
            else self.service.settings.worker_poll_seconds,
        )
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            result = self.run_once()
            if result is None:
                stop_event.wait(interval)
