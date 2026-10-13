"""Initial local MVP schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-01
"""

from alembic import op

from noodle_agent.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
            note_version_id UNINDEXED,
            note_id UNINDEXED,
            title,
            thesis,
            body,
            tags,
            tokenize='unicode61'
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP TABLE IF EXISTS note_fts")
    Base.metadata.drop_all(bind)
