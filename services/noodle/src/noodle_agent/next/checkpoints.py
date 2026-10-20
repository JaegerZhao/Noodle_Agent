"""Immutable stage receipts, with a lease fence on every state transition."""

from __future__ import annotations

import json

from sqlalchemy import text

from .contracts import canonical, sha
from .jobs import Lease, require_lease
from .repository import Conflict, Repository, insert, now, one


class Checkpoints:
    def __init__(self, repository: Repository, lease: Lease):
        self.repository = repository
        self.lease = lease

    def load(self, stage: str, inputs: dict):
        with self.repository.db.engine.connect() as c:
            row = one(
                c,
                "SELECT * FROM job_checkpoints WHERE job_id=:job AND stage=:stage",
                job=self.lease.job_id,
                stage=stage,
            )
        if not row:
            return None
        if row["input_sha256"] != sha(canonical(inputs)):
            raise Conflict(f"Checkpoint input changed: {stage}")
        if row["state"] == "INTENT":
            raise Conflict(
                f"Uncertain external submission: {stage}; inspect provider before retrying"
            )
        return json.loads(self.repository.read(row["artifact_ref"], row["artifact_sha256"]))

    def intent(self, stage: str, inputs: dict):
        with self.repository.transaction() as c:
            require_lease(c, self.lease)
            existing = one(
                c,
                "SELECT state FROM job_checkpoints WHERE job_id=:job AND stage=:stage",
                job=self.lease.job_id,
                stage=stage,
            )
            if existing:
                raise Conflict(f"Stage already started: {stage}")
            insert(
                c,
                "job_checkpoints",
                job_id=self.lease.job_id,
                stage=stage,
                input_sha256=sha(canonical(inputs)),
                state="INTENT",
                created_at=now(),
            )

    def save(self, stage: str, inputs: dict, value: dict):
        data = canonical(value).encode("utf-8")
        digest = sha(data)
        ref = f"artifacts/checkpoints/{digest}.json"
        self.repository.put(ref, data)
        with self.repository.transaction() as c:
            require_lease(c, self.lease)
            row = one(
                c,
                "SELECT * FROM job_checkpoints WHERE job_id=:job AND stage=:stage",
                job=self.lease.job_id,
                stage=stage,
            )
            input_hash = sha(canonical(inputs))
            if row:
                if row["input_sha256"] != input_hash:
                    raise Conflict(f"Checkpoint input changed: {stage}")
                if row["state"] == "DONE":
                    if row["artifact_sha256"] != digest:
                        raise Conflict(f"Immutable checkpoint differs: {stage}")
                    return value
                c.execute(
                    text("""UPDATE job_checkpoints SET state='DONE',artifact_ref=:ref,
                    artifact_sha256=:sha WHERE job_id=:job AND stage=:stage"""),
                    {"ref": ref, "sha": digest, "job": self.lease.job_id, "stage": stage},
                )
            else:
                insert(
                    c,
                    "job_checkpoints",
                    job_id=self.lease.job_id,
                    stage=stage,
                    input_sha256=input_hash,
                    state="DONE",
                    artifact_ref=ref,
                    artifact_sha256=digest,
                    created_at=now(),
                )
        return value

    def compute(self, stage: str, inputs: dict, function):
        cached = self.load(stage, inputs)
        return cached if cached is not None else self.save(stage, inputs, function())
