"""Add durable attachments for agent-mediated note editing.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_attachments" in inspector.get_table_names():
        return
    op.create_table(
        "agent_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("blob_id", sa.String(length=36), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("attachment_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("vision_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["blob_id"], ["blob_objects.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["conversation_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_attachments_conversation_id",
        "agent_attachments",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_attachments_message_id",
        "agent_attachments",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_attachments_blob_id",
        "agent_attachments",
        ["blob_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_attachments_status",
        "agent_attachments",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_attachments_status", table_name="agent_attachments")
    op.drop_index("ix_agent_attachments_blob_id", table_name="agent_attachments")
    op.drop_index("ix_agent_attachments_message_id", table_name="agent_attachments")
    op.drop_index("ix_agent_attachments_conversation_id", table_name="agent_attachments")
    op.drop_table("agent_attachments")
