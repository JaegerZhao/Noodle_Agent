"""Add cached multimodal image understanding.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.storage.migrations.schema import create_tables, drop_tables

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_tables(op.get_bind(), ["visual_understandings"])


def downgrade() -> None:
    drop_tables(op.get_bind(), ["visual_understandings"])
