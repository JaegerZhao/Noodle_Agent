"""Bind quality verdicts to immutable note content.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.db import Base

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["quality_evaluations"].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["quality_evaluations"].drop(op.get_bind(), checkfirst=True)
