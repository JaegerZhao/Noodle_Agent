"""Source revisions, parse records and evidence registration."""

from __future__ import annotations

import json

from sqlalchemy import text

from .contracts import canonical, sha
from .repository import Conflict, InvalidKnowledge, Repository, identifier, insert, now, one


class SourceRepository:
    def __init__(self, repository: Repository):
        self.repository = repository

    def add_source(
        self, content: bytes, title: str, *, kind: str = "PDF", original_name: str = ""
    ) -> dict:
        if kind not in {"PDF", "AUTHOR"} or not content or not title.strip():
            raise ValueError("Invalid source")
        if len(content) > 100 * 1024 * 1024:
            raise ValueError("Source exceeds 100 MiB")
        if kind == "PDF" and not content.startswith(b"%PDF-"):
            raise ValueError("Not a PDF")
        if kind == "AUTHOR":
            content.decode("utf-8")
        digest = sha(content)
        ref = f"blobs/{digest}.{'pdf' if kind == 'PDF' else 'md'}"
        self.repository.put(ref, content)
        with self.repository.transaction() as connection:
            existing = one(
                connection,
                """SELECT s.id,s.current_revision_id,s.deleted_at,r.id AS matched_revision FROM sources s
                JOIN source_revisions r ON r.source_id=s.id
                WHERE r.sha256=:hash AND s.kind=:kind
                ORDER BY CASE WHEN s.current_revision_id=r.id THEN 0 ELSE 1 END,r.ordinal DESC LIMIT 1""",
                hash=digest,
                kind=kind,
            )
            if existing:
                return {
                    "source_id": existing["id"],
                    "revision_id": existing["current_revision_id"],
                    "duplicate": True,
                    "deleted": existing["deleted_at"] is not None,
                    "matched_revision_id": existing["matched_revision"],
                }
            source_id, revision_id = identifier(), identifier()
            insert(
                connection,
                "sources",
                id=source_id,
                kind=kind,
                title=title,
                current_revision_id=None,
                created_at=now(),
            )
            insert(
                connection,
                "source_revisions",
                id=revision_id,
                source_id=source_id,
                ordinal=1,
                sha256=digest,
                blob_ref=ref,
                byte_size=len(content),
                original_name=original_name or title,
                created_at=now(),
            )
            connection.execute(
                text("UPDATE sources SET current_revision_id=:revision WHERE id=:id"),
                {"revision": revision_id, "id": source_id},
            )
            self.repository.event(
                connection, "SOURCE_ADDED", source_id, revision_id, {}, key=f"source:{revision_id}"
            )
            return {
                "source_id": source_id,
                "revision_id": revision_id,
                "duplicate": False,
                "deleted": False,
            }

    def replace_source(
        self, source_id: str, content: bytes, *, expected_revision: str, original_name: str = ""
    ) -> dict:
        with self.repository.db.engine.connect() as connection:
            source = one(connection, "SELECT * FROM sources WHERE id=:id", id=source_id)
        if not source or source["deleted_at"] is not None:
            raise InvalidKnowledge("Unknown or deleted source")
        if not content or len(content) > 100 * 1024 * 1024:
            raise ValueError("Invalid source size")
        if source["kind"] == "PDF" and not content.startswith(b"%PDF-"):
            raise ValueError("Not a PDF")
        if source["kind"] == "AUTHOR":
            content.decode("utf-8")
        digest = sha(content)
        ref = f"blobs/{digest}.{'pdf' if source['kind'] == 'PDF' else 'md'}"
        self.repository.put(ref, content)
        with self.repository.transaction() as connection:
            current = one(connection, "SELECT * FROM sources WHERE id=:id", id=source_id)
            if (
                current["current_revision_id"] != expected_revision
                or current["deleted_at"] is not None
            ):
                raise Conflict("Source head changed")
            previous = one(
                connection, "SELECT * FROM source_revisions WHERE id=:id", id=expected_revision
            )
            if previous["sha256"] == digest:
                return {"source_id": source_id, "revision_id": expected_revision, "duplicate": True}
            if one(
                connection,
                "SELECT id FROM source_revisions WHERE sha256=:hash AND source_id!=:id LIMIT 1",
                hash=digest,
                id=source_id,
            ):
                raise InvalidKnowledge("Identical content already belongs to another source")
            revision_id = identifier()
            insert(
                connection,
                "source_revisions",
                id=revision_id,
                source_id=source_id,
                ordinal=previous["ordinal"] + 1,
                sha256=digest,
                blob_ref=ref,
                byte_size=len(content),
                original_name=original_name or previous["original_name"],
                created_at=now(),
            )
            connection.execute(
                text("UPDATE sources SET current_revision_id=:revision WHERE id=:id"),
                {"revision": revision_id, "id": source_id},
            )
            self.repository.advance_epoch(connection)
            self.repository.event(
                connection,
                "SOURCE_REPLACED",
                source_id,
                revision_id,
                {"previous_revision_id": expected_revision},
                key=f"source:{revision_id}",
            )
            return {"source_id": source_id, "revision_id": revision_id, "duplicate": False}

    def source_valid(self, connection, revision_id: str) -> bool:
        return (
            one(
                connection,
                """SELECT r.id FROM source_revisions r JOIN sources s ON s.id=r.source_id
            WHERE r.id=:id AND s.current_revision_id=r.id AND s.deleted_at IS NULL""",
                id=revision_id,
            )
            is not None
        )

    def record_parse(
        self, revision_id: str, parser_version: str, config: dict, artifact: dict
    ) -> str:
        content = canonical(artifact).encode("utf-8")
        digest = sha(content)
        ref = f"artifacts/parses/{digest}.json"
        self.repository.put(ref, content)
        parse_id = identifier()
        with self.repository.transaction() as connection:
            if not self.source_valid(connection, revision_id):
                raise Conflict("Source is not current")
            insert(
                connection,
                "parse_runs",
                id=parse_id,
                source_revision_id=revision_id,
                parser_version=parser_version,
                config_json=canonical(config),
                input_sha256=sha(canonical([revision_id, parser_version, config])),
                artifact_ref=ref,
                artifact_sha256=digest,
                created_at=now(),
            )
        return parse_id

    def add_evidence(
        self,
        revision_id: str,
        quote: str,
        *,
        page: int | None = None,
        block_id: str | None = None,
        parse_id: str | None = None,
    ) -> str:
        evidence_id = identifier()
        if not quote.strip():
            raise InvalidKnowledge("Empty evidence")
        with self.repository.transaction() as connection:
            source = one(
                connection,
                """SELECT r.*,s.kind FROM source_revisions r
                JOIN sources s ON s.id=r.source_id WHERE r.id=:id""",
                id=revision_id,
            )
            if source is None or not self.source_valid(connection, revision_id):
                raise InvalidKnowledge("Evidence source is not current")
            if source["kind"] == "PDF":
                parse = one(connection, "SELECT * FROM parse_runs WHERE id=:id", id=parse_id)
                if not parse or parse["source_revision_id"] != revision_id or not page or page < 1:
                    raise InvalidKnowledge("PDF evidence requires its parse and page")
                artifact = json.loads(
                    self.repository.read(parse["artifact_ref"], parse["artifact_sha256"])
                )
                block = next(
                    (b for b in artifact.get("blocks", []) if b.get("id") == block_id), None
                )
                if not block or block.get("page") != page or quote not in block.get("text", ""):
                    raise InvalidKnowledge("Evidence does not match the parsed block")
            else:
                original = self.repository.read(source["blob_ref"], source["sha256"]).decode(
                    "utf-8"
                )
                if quote not in original or page is not None or parse_id is not None:
                    raise InvalidKnowledge("Author evidence does not match its snapshot")
            insert(
                connection,
                "evidence_anchors",
                id=evidence_id,
                source_revision_id=revision_id,
                parse_run_id=parse_id,
                page_number=page,
                locator_json=canonical({"block_id": block_id}),
                quoted_text=quote,
                sha256=sha(quote),
            )
        return evidence_id

    def set_source_deleted(self, source_id: str, deleted: bool):
        with self.repository.transaction() as connection:
            source = one(connection, "SELECT * FROM sources WHERE id=:id", id=source_id)
            if not source:
                raise KeyError(source_id)
            if bool(source["deleted_at"]) == deleted:
                return
            connection.execute(
                text("UPDATE sources SET deleted_at=:value WHERE id=:id"),
                {"value": now() if deleted else None, "id": source_id},
            )
            self.repository.advance_epoch(connection)
            token = identifier()
            self.repository.event(
                connection,
                "SOURCE_DELETED" if deleted else "SOURCE_RESTORED",
                source_id,
                source["current_revision_id"],
                {},
                key=f"source-state:{token}",
            )
