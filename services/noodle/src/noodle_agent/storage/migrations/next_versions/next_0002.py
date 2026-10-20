"""Idempotent event fanout for durable next-v1 jobs."""

from alembic import op

revision = "next_0002"
down_revision = "next_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ux_next_event_job_kind", "jobs", ["event_id", "kind"], unique=True)


def downgrade():
    op.drop_index("ux_next_event_job_kind", table_name="jobs")
