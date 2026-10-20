"""Knowledge identities, reading eligibility and withdrawal."""

from __future__ import annotations

import re

from sqlalchemy import text

from .contracts import canonical
from .repository import Conflict, InvalidKnowledge, Repository, identifier, insert, now, one, rows
from .sources import SourceRepository


class ContentRepository:
    def __init__(self, repository: Repository, sources: SourceRepository):
        self.repository = repository
        self.sources = sources

    def create_note(self, kind: str, title: str, *, domain: str = "general", aliases=()) -> str:
        if kind not in {"ATOMIC", "CONCEPT", "MOC"} or not title.strip():
            raise ValueError("Invalid note")
        note_id = identifier()
        folder = {"ATOMIC": "Notes", "CONCEPT": "Concepts", "MOC": "Maps"}[kind]
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")[:60] or "note"
        with self.repository.transaction() as connection:
            insert(
                connection,
                "notes",
                id=note_id,
                kind=kind,
                title=title,
                domain=domain,
                aliases_json=canonical(list(aliases)),
                current_revision_id=None,
                stable_path=f"{folder}/{stem}-{note_id[:8]}.md",
                visibility="ACTIVE",
                created_at=now(),
            )
        return note_id

    def valid_revision(self, connection, revision_id: str, *, active: bool = True) -> bool:
        active_policy = one(connection, "SELECT active_policy FROM storage_contract WHERE id=1")[
            "active_policy"
        ]
        if active_policy != self.repository.policy_version:
            return False
        revision = one(
            connection,
            """SELECT r.*,n.kind,n.visibility,n.current_revision_id FROM note_revisions r
            JOIN notes n ON n.id=r.note_id WHERE r.id=:id""",
            id=revision_id,
        )
        if (
            not revision
            or revision["status"] != "PUBLISHED"
            or revision["visibility"] != "ACTIVE"
            or revision["policy_version"] != self.repository.policy_version
        ):
            return False
        if active and revision["current_revision_id"] != revision_id:
            return False
        if revision["kind"] == "MOC":
            return True
        evidence = rows(
            connection,
            """SELECT e.* FROM section_evidence se
            JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=:id""",
            id=revision_id,
        )
        if not evidence or any(
            not self.sources.source_valid(connection, e["source_revision_id"]) for e in evidence
        ):
            return False
        if revision["kind"] == "CONCEPT":
            inputs = rows(
                connection,
                "SELECT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:id",
                id=revision_id,
            )
            if not inputs or any(
                not self.valid_revision(connection, i["atomic_revision_id"]) for i in inputs
            ):
                return False
        return True

    def get_note(
        self, note_id: str, *, revision_id: str | None = None, historical: bool = False
    ) -> dict:
        with self.repository.db.engine.connect() as connection:
            note = one(connection, "SELECT * FROM notes WHERE id=:id", id=note_id)
            if not note:
                raise KeyError(note_id)
            selected = revision_id or note["current_revision_id"]
            revision = one(
                connection,
                "SELECT * FROM note_revisions WHERE id=:id AND note_id=:note",
                id=selected,
                note=note_id,
            )
            if not revision:
                raise InvalidKnowledge("Unknown or unpublished revision")
            eligible = self.valid_revision(connection, selected)
            if not historical and not eligible:
                raise InvalidKnowledge("Knowledge is no longer current or supported")
            section_data = [
                dict(s)
                for s in rows(
                    connection,
                    "SELECT * FROM sections WHERE revision_id=:r ORDER BY position",
                    r=selected,
                )
            ]
            entries = [
                dict(e)
                for e in rows(
                    connection,
                    "SELECT * FROM map_entries WHERE map_revision_id=:r ORDER BY position",
                    r=selected,
                )
            ]
        markdown = self.repository.read(
            revision["markdown_ref"], revision["markdown_sha256"]
        ).decode("utf-8")
        return {
            **dict(note),
            "revision": dict(revision),
            "sections": section_data,
            "map_entries": entries,
            "markdown": markdown,
            "eligible": eligible,
        }

    def withdraw_note(self, note_id: str, *, expected_revision: str):
        with self.repository.transaction() as connection:
            note = one(connection, "SELECT * FROM notes WHERE id=:id", id=note_id)
            if not note or note["current_revision_id"] != expected_revision:
                raise Conflict("Note head changed")
            connection.execute(
                text("UPDATE notes SET visibility='WITHDRAWN' WHERE id=:id"), {"id": note_id}
            )
            self.repository.advance_epoch(connection)
            self.repository.event(
                connection,
                "NOTE_WITHDRAWN",
                note_id,
                expected_revision,
                {},
                key=f"withdraw:{expected_revision}",
            )
