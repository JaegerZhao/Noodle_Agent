from __future__ import annotations

from sqlalchemy import (
    text,
)
from sqlalchemy.orm import Session


def delete_fts_for_note(connection: Session, note_id: str) -> None:
    connection.execute(text("DELETE FROM note_fts WHERE note_id = :note_id"), {"note_id": note_id})


def insert_fts(
    connection: Session,
    *,
    note_version_id: str,
    note_id: str,
    title: str,
    thesis: str,
    body: str,
    tags: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO note_fts(note_version_id, note_id, title, thesis, body, tags)
            VALUES (:version_id, :note_id, :title, :thesis, :body, :tags)
            """
        ),
        {
            "version_id": note_version_id,
            "note_id": note_id,
            "title": title,
            "thesis": thesis,
            "body": body,
            "tags": tags,
        },
    )
