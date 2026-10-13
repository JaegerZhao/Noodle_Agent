"""Add structured note block payloads and inline asset anchors.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 0001 creates current metadata for a brand-new installation.
    # Guard additive changes so both a clean ``upgrade head`` and an upgrade of
    # a historical database remain valid.
    inspector = sa.inspect(op.get_bind())
    block_columns = {item["name"] for item in inspector.get_columns("note_blocks")}
    if "content_json" not in block_columns:
        with op.batch_alter_table("note_blocks") as batch:
            batch.add_column(sa.Column("content_json", sa.Text(), nullable=True))

    asset_columns = {item["name"] for item in inspector.get_columns("note_assets")}
    asset_indexes = {item["name"] for item in inspector.get_indexes("note_assets")}
    with op.batch_alter_table("note_assets") as batch:
        if "note_block_id" not in asset_columns:
            batch.add_column(sa.Column("note_block_id", sa.String(36), nullable=True))
        if "placement" not in asset_columns:
            batch.add_column(
                sa.Column("placement", sa.String(20), nullable=False, server_default="AFTER")
            )
        if "ix_note_assets_note_block_id" not in asset_indexes:
            batch.create_index("ix_note_assets_note_block_id", ["note_block_id"])


def downgrade() -> None:
    with op.batch_alter_table("note_assets") as batch:
        batch.drop_index("ix_note_assets_note_block_id")
        batch.drop_column("placement")
        batch.drop_column("note_block_id")
    with op.batch_alter_table("note_blocks") as batch:
        batch.drop_column("content_json")
