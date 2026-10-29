"""User source organization is independent of knowledge content revisions."""

from alembic import op

revision = 'next_0011'
down_revision = 'next_0010'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE source_folders(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata_version INTEGER NOT NULL DEFAULT 1)''')
    op.execute('''CREATE TABLE source_library_metadata(
        source_id TEXT PRIMARY KEY REFERENCES sources(id),
        folder_id TEXT REFERENCES source_folders(id) ON DELETE SET NULL,
        display_title TEXT, metadata_version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL)''')
    op.execute('CREATE INDEX source_library_folder ON source_library_metadata(folder_id)')
    op.execute('''CREATE TABLE source_library_operations(
        operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at TEXT NOT NULL)''')


def downgrade():
    raise RuntimeError('Restore a consistent snapshot instead of discarding user organization')
