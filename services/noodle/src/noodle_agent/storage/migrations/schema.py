"""Frozen SQLite schema resources used only by versioned migrations."""

from __future__ import annotations

import json
import re
from pathlib import Path


def snapshot(version=1):
    return json.loads(
        Path(__file__).with_name(f"schema_v{version}.json").read_text(encoding="utf-8")
    )


def columns(connection, table):
    return {row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def create_tables(connection, names=None, *, version=1):
    data = snapshot(version)
    selected = set(names) if names is not None else None
    for table in data["tables"]:
        if selected is None or table["name"] in selected:
            connection.exec_driver_sql(
                table["sql"].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
            )
    for index in data["indices"]:
        if (selected is None or index["table"] in selected) and set(index["columns"]) <= columns(
            connection, index["table"]
        ):
            sql = re.sub(
                r"^CREATE (UNIQUE )?INDEX ",
                lambda m: "CREATE " + (m[1] or "") + "INDEX IF NOT EXISTS ",
                index["sql"],
            )
            connection.exec_driver_sql(sql)


def drop_tables(connection, names):
    for name in reversed(list(names)):
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{name}"')


def create_fts(connection):
    connection.exec_driver_sql("""CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
        note_version_id UNINDEXED,note_id UNINDEXED,title,thesis,body,tags,tokenize='unicode61')""")
    connection.exec_driver_sql("""CREATE VIRTUAL TABLE IF NOT EXISTS concept_fts USING fts5(
        concept_version_id UNINDEXED,concept_id UNINDEXED,title,summary,body,aliases,tokenize='unicode61')""")


def validate(connection):
    for table in snapshot(2)["tables"]:
        missing = set(table["columns"]) - columns(connection, table["name"])
        if missing:
            raise ValueError(
                f"Unsupported old schema: {table['name']} is missing {sorted(missing)}. Restore the migration backup and review this schema."
            )
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
        raise ValueError("SQLite integrity check failed")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
        raise ValueError("SQLite foreign-key check failed")
