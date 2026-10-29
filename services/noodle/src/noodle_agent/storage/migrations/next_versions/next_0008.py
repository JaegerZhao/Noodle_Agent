"""Frozen entity plans and per-Atomic route receipts; bodies use existing revisions."""

from alembic import op

revision = "next_0008"
down_revision = "next_0007"
branch_labels = None
depends_on = None


def upgrade():
    for sql in (
        "CREATE TABLE concept_entity_plans(snapshot_hash TEXT PRIMARY KEY,source_revision_id TEXT NOT NULL REFERENCES source_revisions(id),state TEXT NOT NULL CHECK(state IN ('PLANNING','READY')),job_id TEXT NOT NULL REFERENCES jobs(id),lease_token TEXT NOT NULL,artifact_ref TEXT,artifact_sha256 TEXT,created_at TEXT NOT NULL)",
        "CREATE TABLE atomic_entity_routes(atomic_revision_id TEXT PRIMARY KEY REFERENCES note_revisions(id),plan_hash TEXT NOT NULL REFERENCES concept_entity_plans(snapshot_hash),concept_id TEXT REFERENCES notes(id),status TEXT NOT NULL CHECK(status IN ('ACCEPTED','SKIPPED','REVIEW')),route_json TEXT NOT NULL,policy TEXT NOT NULL)",
        "CREATE INDEX ix_entity_route_concept ON atomic_entity_routes(concept_id,atomic_revision_id)",
    ):
        op.execute(sql)


def downgrade():
    raise RuntimeError("Restore a consistent snapshot instead of discarding history")
