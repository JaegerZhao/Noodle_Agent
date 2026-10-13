"""Add cached multimodal image understanding.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.db import Base

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["visual_understandings"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["visual_understandings"].drop(op.get_bind(), checkfirst=True)
