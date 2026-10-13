"""Add composed source notes and outline placements.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.db import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = (
    "source_notes",
    "source_note_versions",
    "source_note_outline_nodes",
    "source_note_unit_placements",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
