"""Composition root for the isolated next-v1 content system."""

from pathlib import Path

from .content import ContentRepository
from .contracts import Candidate, Review
from .jobs import JobQueue, Lease
from .publication import Publisher
from .repository import Conflict, InvalidKnowledge, Repository, one
from .sources import SourceRepository

__all__ = ["Conflict", "InvalidKnowledge", "Store", "one"]


class Store:
    contract = "next-v1"

    def __init__(self, root: Path, *, policy_version: str = "writing-next-v1"):
        self.repository = Repository(root, policy_version=policy_version)
        self.sources = SourceRepository(self.repository)
        self.content = ContentRepository(self.repository, self.sources)
        self.publisher = Publisher(self.repository, self.sources, self.content)
        self.jobs = JobQueue(self.repository)
        self.root = self.repository.root
        self.db = self.repository.db

    def close(self):
        self.repository.close()

    def path(self, ref: str):
        return self.repository.path(ref)

    def put(self, ref: str, content: bytes):
        return self.repository.put(ref, content)

    def read(self, ref: str, expected_hash: str):
        return self.repository.read(ref, expected_hash)

    def add_source(self, content: bytes, title: str, *, kind="PDF", original_name="", regenerate_removed=False):
        return self.sources.add_source(content, title, kind=kind, original_name=original_name, regenerate_removed=regenerate_removed)

    def replace_source(
        self, source_id: str, content: bytes, *, expected_revision: str, original_name=""
    ):
        return self.sources.replace_source(
            source_id, content, expected_revision=expected_revision, original_name=original_name
        )

    def record_parse(self, revision_id: str, parser_version: str, config: dict, artifact: dict):
        return self.sources.record_parse(revision_id, parser_version, config, artifact)

    def add_evidence(
        self, revision_id: str, quote: str, *, page=None, block_id=None, parse_id=None
    ):
        return self.sources.add_evidence(
            revision_id, quote, page=page, block_id=block_id, parse_id=parse_id
        )

    def create_note(self, kind: str, title: str, *, domain="general", aliases=()):
        return self.content.create_note(kind, title, domain=domain, aliases=aliases)

    def publish(
        self,
        candidate: Candidate,
        *,
        expected_revision: str | None,
        review: Review,
        allow_protected_edit=False,
        lease: Lease | None = None,
        guard=None,
        accepted=None,
    ):
        return self.publisher.publish(
            candidate,
            expected_revision=expected_revision,
            review=review,
            allow_protected_edit=allow_protected_edit,
            lease=lease,
            guard=guard,
            accepted=accepted,
        )

    def get_note(self, note_id: str, *, revision_id=None, historical=False):
        return self.content.get_note(note_id, revision_id=revision_id, historical=historical)

    def set_source_deleted(self, source_id: str, deleted: bool):
        return self.sources.set_source_deleted(source_id, deleted)

    def withdraw_note(self, note_id: str, *, expected_revision: str):
        return self.content.withdraw_note(note_id, expected_revision=expected_revision)

    def restore_note(self, note_id: str, *, expected_revision: str):
        return self.content.restore_note(note_id, expected_revision=expected_revision)
