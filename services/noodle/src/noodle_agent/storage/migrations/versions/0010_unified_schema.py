"""Adopt supported local databases and retire unused runtime structures.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from noodle_agent.storage.migrations.schema import columns, create_fts, create_tables, validate

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

ADDITIONS = {
    "atomic_notes": {"source_version_id": "VARCHAR(36)"},
    "note_blocks": {"content_json": "TEXT DEFAULT '{}'"},
    "note_assets": {"note_block_id": "VARCHAR(36)", "placement": "VARCHAR(20) DEFAULT 'AFTER'"},
    "concept_module_assets": {"content_block_key": "VARCHAR(220) DEFAULT ''"},
    "jobs": {
        "lease_owner": "VARCHAR(120)",
        "lease_expires_at": "DATETIME",
        "heartbeat_at": "DATETIME",
    },
    "backbone_figures": {"caption_zh": "TEXT DEFAULT ''"},
    "concept_build_records": {
        "result_status": "VARCHAR(30) DEFAULT 'LEGACY_UNVERIFIED'",
        "fallback_used": "BOOLEAN DEFAULT 0",
        "fallback_detail": "TEXT DEFAULT ''",
        "output_sha256": "VARCHAR(64) DEFAULT ''",
    },
    "concept_update_jobs": {
        "result_json": "TEXT DEFAULT '{}'",
        "attempt_count": "INTEGER DEFAULT 0",
        "next_attempt_at": "DATETIME",
        "lease_owner": "VARCHAR(120)",
        "lease_expires_at": "DATETIME",
        "heartbeat_at": "DATETIME",
        "operation": "VARCHAR(30) DEFAULT 'ADD'",
        "supersedes_contribution_id": "VARCHAR(36)",
    },
    "concept_contributions": {
        "superseded_by_id": "VARCHAR(36)",
        "updated_at": "DATETIME",
        "decided_at": "DATETIME",
    },
}


def upgrade():
    bind = op.get_bind()
    create_tables(bind, version=2)
    for table, items in ADDITIONS.items():
        existing = columns(bind, table)
        for name, ddl in items.items():
            if name not in existing:
                bind.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
                if table == "atomic_notes" and name == "source_version_id":
                    bind.exec_driver_sql("""UPDATE atomic_notes SET source_version_id=(
                        SELECT source_version_id FROM note_versions WHERE note_versions.note_id=atomic_notes.id
                        ORDER BY version_no LIMIT 1) WHERE source_version_id IS NULL""")
                if table == "concept_contributions" and name == "updated_at":
                    bind.exec_driver_sql(
                        "UPDATE concept_contributions SET updated_at=created_at WHERE updated_at IS NULL"
                    )
    # Harness identifiers no longer reference Python conversation rows. Keep
    # identifiers and attachment data while removing obsolete FK constraints.
    foreign_keys = sa.inspect(bind).get_foreign_keys("agent_attachments")
    obsolete = [
        f for f in foreign_keys if f["referred_table"] in {"conversations", "conversation_messages"}
    ]
    if obsolete:
        convention = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
        with op.batch_alter_table(
            "agent_attachments", naming_convention=convention, recreate="always"
        ) as batch:
            for fk in obsolete:
                name = (
                    fk["name"]
                    or f"fk_agent_attachments_{fk['constrained_columns'][0]}_{fk['referred_table']}"
                )
                batch.drop_constraint(name, type_="foreignkey")
    legacy = [
        "approval_requests",
        "agent_tool_calls",
        "agent_runs",
        "conversation_messages",
        "conversations",
    ]
    tables = set(sa.inspect(bind).get_table_names())
    # Populated historical storage remains intact. Only an entirely empty
    # retired runtime is removed from fresh or unused installations.
    if all(
        bind.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}"').scalar() == 0
        for t in legacy
        if t in tables
    ):
        for table in legacy:
            if table in tables:
                op.drop_table(table)
    if (
        "notion_page_maps" in tables
        and bind.exec_driver_sql("SELECT COUNT(*) FROM notion_page_maps").scalar() == 0
    ):
        op.drop_table("notion_page_maps")
    create_tables(bind, version=2)
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_atomic_note_source_version_stable ON atomic_notes(source_version_id,stable_key)"
    )
    create_fts(bind)
    validate(bind)


def downgrade():
    raise RuntimeError(
        "0010 cannot automatically restore retired runtime structures; restore the pre-upgrade backup"
    )
