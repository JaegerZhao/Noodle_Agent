"""Durable generation checkpoints and deterministic note identities."""

from alembic import op

revision = "next_0004"
down_revision = "next_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE notes ADD COLUMN creation_key TEXT")
    op.execute(
        "CREATE UNIQUE INDEX ux_next_note_creation ON notes(creation_key) WHERE creation_key IS NOT NULL"
    )
    op.execute("""CREATE TABLE job_checkpoints (
        job_id TEXT NOT NULL REFERENCES jobs(id),
        stage TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('INTENT','DONE')),
        artifact_ref TEXT,
        artifact_sha256 TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,stage),
        CHECK ((state='DONE' AND artifact_ref IS NOT NULL AND artifact_sha256 IS NOT NULL)
            OR (state='INTENT' AND artifact_ref IS NULL AND artifact_sha256 IS NULL))
    )""")


def downgrade():
    raise RuntimeError("Restore a consistent snapshot; do not discard generation checkpoints")
