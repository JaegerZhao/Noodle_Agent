"""Allow one frozen fragment to have separately explained target contributions."""

from alembic import op

revision = 'next_0009'
down_revision = 'next_0008'
branch_labels = None
depends_on = None


def upgrade():
    # No table references decision IDs by FK. Preserve every existing row and
    # applied proof while replacing the old Atomic/fragment-only uniqueness.
    op.execute('''CREATE TABLE contribution_decisions_v9(
        id TEXT PRIMARY KEY, atomic_revision_id TEXT NOT NULL REFERENCES note_revisions(id),
        fragment_id TEXT NOT NULL, policy TEXT NOT NULL, concept_id TEXT REFERENCES notes(id),
        section_id TEXT, action TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ACCEPTED','REVIEW','SKIPPED')),
        fragment_json TEXT NOT NULL, decision_json TEXT NOT NULL,
        applied_revision_id TEXT REFERENCES note_revisions(id), created_at TEXT NOT NULL)''')
    op.execute('INSERT INTO contribution_decisions_v9 SELECT * FROM contribution_decisions')
    op.execute('DROP TABLE contribution_decisions')
    op.execute('ALTER TABLE contribution_decisions_v9 RENAME TO contribution_decisions')
    op.execute('CREATE INDEX ix_decision_target ON contribution_decisions(concept_id,atomic_revision_id)')
    op.execute("CREATE UNIQUE INDEX uq_decision_target_fragment ON contribution_decisions(atomic_revision_id,fragment_id,policy,COALESCE(concept_id,''))")


def downgrade():
    raise RuntimeError('Restore a consistent snapshot instead of discarding multi-target contributions')
