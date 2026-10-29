"""Durable organization corrections survive later Concept rebuilds."""

from alembic import op

revision = 'next_0010'
down_revision = 'next_0009'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE relation_overrides(
        owner_note_id TEXT NOT NULL REFERENCES notes(id), relation_key TEXT NOT NULL,
        operation_json TEXT NOT NULL, source_revision_id TEXT NOT NULL REFERENCES note_revisions(id),
        run_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner_note_id,relation_key))''')


def downgrade():
    raise RuntimeError('Restore a consistent snapshot instead of deleting relation decisions')
