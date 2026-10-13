"""Frozen initial local schema; existing tables are adopted without data removal."""

from alembic import op

from noodle_agent.storage.migrations.schema import create_fts, create_tables, drop_tables, snapshot

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    create_tables(op.get_bind())
    create_fts(op.get_bind())


def downgrade():
    op.execute("DROP TABLE IF EXISTS note_fts")
    op.execute("DROP TABLE IF EXISTS concept_fts")
    drop_tables(op.get_bind(), [t["name"] for t in snapshot()["tables"]])
