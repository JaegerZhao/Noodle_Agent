"""Generation controls and queryable, immutable-call metric projections."""

from alembic import op

revision = "next_0007"
down_revision = "next_0006"
branch_labels = None
depends_on = None


def upgrade():
    for sql in (
        "CREATE TABLE job_controls(job_id TEXT PRIMARY KEY REFERENCES jobs(id),state TEXT NOT NULL CHECK(state IN ('RUN','PAUSE_REQUESTED','PAUSED')),chapter_id TEXT,chapter_title TEXT,requested_at TEXT,updated_at TEXT NOT NULL)",
        "CREATE TABLE job_stage_events(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),stage TEXT NOT NULL,detail_json TEXT NOT NULL,created_at TEXT NOT NULL)",
        "CREATE INDEX ix_stage_job ON job_stage_events(job_id,created_at)",
        "CREATE TABLE provider_calls(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),root_job_id TEXT REFERENCES jobs(id),kind TEXT NOT NULL,operation TEXT NOT NULL,model TEXT,started_at TEXT,elapsed_ms INTEGER,input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,cached_input_tokens INTEGER,status TEXT,artifact_ref TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)",
        "CREATE INDEX ix_calls_root ON provider_calls(root_job_id,created_at)",
    ):
        op.execute(sql)
    op.execute(
        "INSERT INTO job_controls(job_id,state,updated_at) SELECT id,'PAUSED',created_at FROM jobs WHERE kind='GENERATE_ATOMICS' AND status='REVIEW' AND json_extract(error_json,'$.code')='USER_PAUSED'"
    )
    op.execute(
        "UPDATE jobs SET status='PAUSED' WHERE id IN (SELECT job_id FROM job_controls WHERE state='PAUSED')"
    )


def downgrade():
    raise RuntimeError("Restore a consistent snapshot instead of discarding history")
