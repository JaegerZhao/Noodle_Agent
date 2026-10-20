"""One transactional, versioned schema-upgrade entry for API, Worker and CLI."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

_ENV_LOCK = threading.RLock()


def config_for(connection=None, *, contract="legacy-v1"):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    config.set_main_option("path_separator", "os")
    config.set_main_option("version_path_separator", "os")
    if contract == "next-v1":
        config.set_main_option(
            "version_locations", str(Path(__file__).with_name("migrations") / "next_versions")
        )
    elif contract != "legacy-v1":
        raise ValueError("Unknown storage contract")
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def status(engine, *, contract="legacy-v1"):
    head = ScriptDirectory.from_config(config_for(contract=contract)).get_current_head()
    with engine.connect() as connection:
        current = (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            if inspect(connection).has_table("alembic_version")
            else None
        )
    return {"current": current, "head": head, "needs_upgrade": current != head}


def upgrade(engine, *, contract="legacy-v1"):
    if engine.dialect.name != "sqlite":
        raise ValueError("Current migrations support SQLite only")
    database = engine.url.database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    # SQLite serializes writers across processes. Alembic's global context also
    # requires serialization across cold-start threads in the same process.
    with _ENV_LOCK, engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            config = config_for(connection, contract=contract)
            head = ScriptDirectory.from_config(config).get_current_head()
            names = set(inspect(connection).get_table_names())
            if "storage_contract" in names:
                stored = connection.exec_driver_sql(
                    "SELECT name FROM storage_contract WHERE id=1"
                ).scalar()
                if stored != contract:
                    raise ValueError(f"Storage contract mismatch: expected {contract}")
            elif contract == "next-v1" and names - {"alembic_version"}:
                raise ValueError(
                    "Legacy or unknown database refused by next-v1; use an empty isolated store"
                )
            elif contract == "next-v1" and "alembic_version" in names:
                raise ValueError("Unmarked versioned database refused by next-v1")
            current = (
                connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                if "alembic_version" in names
                else None
            )
            backup = None
            if current != head:
                if names - {"alembic_version"} and database and database != ":memory:":
                    directory = Path(database).parent / "backups"
                    directory.mkdir(exist_ok=True)
                    backup = (
                        directory
                        / f"schema-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.db"
                    )
                    # A separate read connection snapshots the committed DB
                    # before any DDL is issued by the reserved writer.
                    with sqlite3.connect(database) as src, sqlite3.connect(backup) as dest:
                        src.backup(dest)
                command.upgrade(config, "head")
            connection.commit()
            return {
                "current": head,
                "upgraded": current != head,
                "backup": str(backup) if backup else None,
            }
        except Exception:
            connection.rollback()
            raise
