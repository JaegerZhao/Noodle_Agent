"""Add persisted stage and model-call telemetry.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05
"""

from alembic import op

from noodle_agent.db import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = ("stage_events", "llm_call_events", "vision_call_events")


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
