"""Author provenance, public visibility and Vault projection receipts."""

from alembic import op

revision = "next_0006"
down_revision = "next_0005"
branch_labels = None
depends_on = None


def upgrade():
    for sql in (
        "CREATE TABLE job_progress(job_id TEXT PRIMARY KEY REFERENCES jobs(id),stage TEXT NOT NULL,details_json TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE author_origin_sources(revision_id TEXT NOT NULL REFERENCES note_revisions(id),source_revision_id TEXT NOT NULL REFERENCES source_revisions(id),PRIMARY KEY(revision_id,source_revision_id))",
        "CREATE TABLE publication_visibility(note_id TEXT PRIMARY KEY REFERENCES notes(id),is_public INTEGER NOT NULL DEFAULT 0 CHECK(is_public IN (0,1)))",
        "CREATE TABLE vault_receipts(note_id TEXT PRIMARY KEY REFERENCES notes(id),revision_id TEXT NOT NULL REFERENCES note_revisions(id),file_sha256 TEXT NOT NULL,relative_path TEXT NOT NULL,updated_at TEXT NOT NULL)",
    ):
        op.execute(sql)


def downgrade():
    raise RuntimeError("Restore a consistent snapshot instead of discarding publication history")
