"""Add conversations, agent runs, tool calls and approval requests.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04
"""

from alembic import op

from noodle_agent.db import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (
    "conversations",
    "conversation_messages",
    "agent_runs",
    "agent_tool_calls",
    "approval_requests",
)


def upgrade() -> None:
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
