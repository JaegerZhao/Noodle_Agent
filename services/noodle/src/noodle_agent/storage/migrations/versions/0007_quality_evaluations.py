"""Bind quality verdicts to immutable note content.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.storage.migrations.schema import create_tables, drop_tables

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_tables(op.get_bind(), ["quality_evaluations"])


def downgrade() -> None:
    drop_tables(op.get_bind(), ["quality_evaluations"])
