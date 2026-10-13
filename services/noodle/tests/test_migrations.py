from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config

from noodle_agent.db import Database


class MigrationTest(unittest.TestCase):
    def test_database_init_is_safe_under_concurrent_cold_start(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            database_path = Path(raw) / "cold-start.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            starter_count = 8
            barrier = Barrier(starter_count)

            def initialize() -> bool:
                database = Database(database_url)
                try:
                    barrier.wait(timeout=5)
                    database.init()
                    return database.healthcheck()
                finally:
                    database.close()

            with ThreadPoolExecutor(max_workers=starter_count) as executor:
                results = list(
                    executor.map(lambda _index: initialize(), range(starter_count))
                )

            self.assertEqual(results, [True] * starter_count)

    def test_upgrade_head_creates_schema_and_fts(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            database_path = Path(raw) / "migration.db"
            config = Config(str(project_root / "alembic.ini"))
            config.set_main_option("script_location", str(project_root / "migrations"))
            config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
            command.upgrade(config, "head")
            connection = sqlite3.connect(database_path)
            try:
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                job_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
            finally:
                connection.close()
            self.assertEqual(revision, ("0009",))
            self.assertIn("atomic_notes", tables)
            self.assertIn("note_blocks", tables)
            self.assertIn("note_candidates", tables)
            self.assertIn("visual_assets", tables)
            self.assertIn("note_fts", tables)
            self.assertIn("conversations", tables)
            self.assertIn("conversation_messages", tables)
            self.assertIn("agent_runs", tables)
            self.assertIn("agent_tool_calls", tables)
            self.assertIn("approval_requests", tables)
            self.assertIn("agent_attachments", tables)
            self.assertIn("source_notes", tables)
            self.assertIn("source_note_versions", tables)
            self.assertIn("source_note_outline_nodes", tables)
            self.assertIn("source_note_unit_placements", tables)
            self.assertIn("stage_events", tables)
            self.assertIn("llm_call_events", tables)
            self.assertIn("vision_call_events", tables)
            self.assertIn("visual_understandings", tables)
            self.assertIn("quality_evaluations", tables)
            self.assertTrue(
                {"lease_owner", "lease_expires_at", "heartbeat_at"}.issubset(
                    job_columns
                )
            )


if __name__ == "__main__":
    unittest.main()
