"""Fence mixed-version writers with a database-owned publication policy."""

import sqlalchemy as sa
from alembic import op

revision = "next_0003"
down_revision = "next_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "storage_contract",
        sa.Column("active_policy", sa.Text(), nullable=False, server_default="writing-next-v1"),
    )


def downgrade():
    raise RuntimeError("Restore a consistent snapshot; policy fencing must not be silently removed")
