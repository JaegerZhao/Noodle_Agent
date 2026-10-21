"""Concept decisions and rebuild state; immutable publication inputs gain ranges."""

from alembic import op

revision = "next_0005"
down_revision = "next_0004"
branch_labels = None
depends_on = None


def upgrade():
    statements = [
        "ALTER TABLE concept_inputs ADD COLUMN fragments_json TEXT NOT NULL DEFAULT '[]'",
        "CREATE TABLE concept_identity_claims(domain TEXT NOT NULL,identity_key TEXT NOT NULL,concept_id TEXT NOT NULL REFERENCES notes(id),definition_json TEXT NOT NULL,PRIMARY KEY(domain,identity_key))",
        "CREATE TABLE contribution_decisions(id TEXT PRIMARY KEY,atomic_revision_id TEXT NOT NULL REFERENCES note_revisions(id),fragment_id TEXT NOT NULL,policy TEXT NOT NULL,concept_id TEXT REFERENCES notes(id),section_id TEXT,action TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('ACCEPTED','REVIEW','SKIPPED')),fragment_json TEXT NOT NULL,decision_json TEXT NOT NULL,applied_revision_id TEXT REFERENCES note_revisions(id),created_at TEXT NOT NULL,UNIQUE(atomic_revision_id,fragment_id,policy))",
        "CREATE INDEX ix_decision_target ON contribution_decisions(concept_id,atomic_revision_id)",
        "CREATE TABLE concept_work_state(concept_id TEXT PRIMARY KEY REFERENCES notes(id),desired_generation INTEGER NOT NULL,completed_generation INTEGER NOT NULL DEFAULT 0,desired_input_hash TEXT NOT NULL,status TEXT NOT NULL)",
        "CREATE TABLE index_generations(id TEXT PRIMARY KEY,mode_key TEXT NOT NULL,profile_id TEXT REFERENCES embedding_profiles(id),backend TEXT NOT NULL,manifest_ref TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,matrix_ref TEXT,matrix_sha256 TEXT,status TEXT NOT NULL CHECK(status IN ('BUILDING','READY','FAILED')),created_at TEXT NOT NULL)",
        "CREATE TABLE index_heads(mode_key TEXT NOT NULL,backend TEXT NOT NULL,generation TEXT NOT NULL REFERENCES index_generations(id),PRIMARY KEY(mode_key,backend))",
        "CREATE TABLE index_work_state(id INTEGER PRIMARY KEY CHECK(id=1),desired_generation INTEGER NOT NULL DEFAULT 0,completed_generation INTEGER NOT NULL DEFAULT 0)",
        "INSERT INTO index_work_state(id) VALUES(1)",
        "CREATE TABLE chunk_metadata(chunk_id TEXT PRIMARY KEY REFERENCES chunks(id),position INTEGER NOT NULL,tokens INTEGER NOT NULL,metadata_json TEXT NOT NULL)",
        "CREATE VIRTUAL TABLE knowledge_fts USING fts5(generation UNINDEXED,chunk_id UNINDEXED,title,body,tokenize='unicode61')",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Restore a consistent snapshot instead of discarding maintenance history")
