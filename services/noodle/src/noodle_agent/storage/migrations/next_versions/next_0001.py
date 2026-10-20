"""Fresh next-v1 store; no conversion from generated legacy knowledge."""

import json
from pathlib import Path

from alembic import op

revision = "next_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    schema = json.loads(Path(__file__).with_name("schema_0001.json").read_text(encoding="utf-8"))
    for statement in schema:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Restore a consistent next-v1 snapshot instead of dropping knowledge")
