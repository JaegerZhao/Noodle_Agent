from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine

from noodle_agent.db import Base, Database
from noodle_agent.storage.migrations.schema import create_fts, create_tables, snapshot
from noodle_agent.storage.runner import status


def url(path):
    return "sqlite:///" + path.as_posix()


def test_cold_start_is_serialized_and_idempotent(tmp_path):
    database_url = url(tmp_path / "cold.db")
    barrier = Barrier(8)

    def start(_):
        db = Database(database_url)
        try:
            barrier.wait(timeout=10)
            db.init()
            return db.healthcheck()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(start, range(8))) == [True] * 8
    db = Database(database_url)
    db.init()
    assert db.last_migration == {"current": "0010", "upgraded": False, "backup": None}
    assert status(db.engine)["needs_upgrade"] is False
    db.close()


def test_fresh_schema_matches_active_models_and_has_no_retired_runtime(tmp_path):
    path = tmp_path / "fresh.db"
    db = Database(url(path))
    db.init()
    db.close()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in Base.metadata.sorted_tables:
            assert table.name in tables
            assert {r[1] for r in conn.execute(f'PRAGMA table_info("{table.name}")')} == {
                c.name for c in table.columns
            }
        assert {"note_fts", "concept_fts", "alembic_version"} <= tables
        assert (
            not {
                "conversations",
                "conversation_messages",
                "agent_runs",
                "agent_tool_calls",
                "approval_requests",
                "notion_page_maps",
            }
            & tables
        )
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def legacy(path):
    engine = create_engine(url(path))
    with engine.begin() as conn:
        create_tables(conn)
        create_fts(conn)
    engine.dispose()


def test_unversioned_old_columns_upgrade_and_backup(tmp_path):
    path = tmp_path / "old.db"
    legacy(path)
    with sqlite3.connect(path) as conn:
        indices = [
            r[1]
            for r in conn.execute("PRAGMA index_list(jobs)")
            if r[1].startswith("ix_jobs_lease")
        ]
        for name in indices:
            conn.execute(f'DROP INDEX "{name}"')
        for name in ["lease_owner", "lease_expires_at", "heartbeat_at"]:
            conn.execute(f'ALTER TABLE jobs DROP COLUMN "{name}"')
        conn.execute(
            "INSERT INTO conversations(id,channel,title,summary,created_at,updated_at) VALUES('history','WEB','keep','keep','2026-01-01','2026-01-01')"
        )
    db = Database(url(path))
    db.init()
    backup = Path(db.last_migration["backup"])
    db.close()
    assert backup.is_file()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT title FROM conversations WHERE id='history'").fetchone() == (
            "keep",
        )
        assert {"lease_owner", "lease_expires_at", "heartbeat_at"} <= {
            r[1] for r in conn.execute("PRAGMA table_info(jobs)")
        }
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(backup) as conn:
        assert "lease_owner" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert conn.execute("SELECT title FROM conversations WHERE id='history'").fetchone() == (
            "keep",
        )


def test_stamped_0009_database_upgrades_without_losing_history(tmp_path):
    path = tmp_path / "stamped.db"
    legacy(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES('0009')")
        conn.execute(
            "INSERT INTO conversations(id,channel,title,summary,created_at,updated_at) VALUES('a','WEB','history','','2026-01-01','2026-01-01')"
        )
    db = Database(url(path))
    db.init()
    db.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)
        assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone() == (1,)


def test_unsupported_schema_rolls_back_all_ddl_and_preserves_rows(tmp_path):
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sources(id VARCHAR(36) PRIMARY KEY)")
        conn.execute("INSERT INTO sources VALUES('preserve')")
    db = Database(url(path))
    try:
        with pytest.raises(ValueError, match="Unsupported old schema"):
            db.init()
    finally:
        db.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM sources").fetchall() == [("preserve",)]
        assert {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"sources"}
    assert list((tmp_path / "backups").glob("schema-*.db"))


def test_frozen_schema_resources_match_current_active_model_columns():
    data = snapshot(2)
    assert {t["name"]: set(t["columns"]) for t in data["tables"]} == {
        t.name: {c.name for c in t.columns} for t in Base.metadata.tables.values()
    }
