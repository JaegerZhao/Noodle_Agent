"""Add recoverable worker leases to persisted jobs.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")
    }
    with op.batch_alter_table("jobs") as batch:
        if "lease_owner" not in existing_columns:
            batch.add_column(sa.Column("lease_owner", sa.String(length=120), nullable=True))
        if "lease_expires_at" not in existing_columns:
            batch.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        if "heartbeat_at" not in existing_columns:
            batch.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    existing_indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("jobs")
    }
    with op.batch_alter_table("jobs") as batch:
        if "ix_jobs_lease_owner" not in existing_indexes:
            batch.create_index("ix_jobs_lease_owner", ["lease_owner"], unique=False)
        if "ix_jobs_lease_expires_at" not in existing_indexes:
            batch.create_index("ix_jobs_lease_expires_at", ["lease_expires_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_lease_expires_at")
        batch.drop_index("ix_jobs_lease_owner")
        batch.drop_column("heartbeat_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
