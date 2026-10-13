from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .domain import (
    Answer,
    DocumentProfile,
    GenerationBundle,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    ParsedDocument,
    SearchHit,
    SourceNoteOutlinePlan,
    SourceNotePlanningBundle,
)


class ParseSubmission(Protocol):
    external_id: str
    state: str


class ParserPort(Protocol):
    @property
    def name(self) -> str: ...

    def submit(self, pdf_path: Path, data_id: str) -> dict: ...

    def poll(self, external_id: str) -> dict: ...

    def download_and_normalize(
        self, external_id: str, result: dict, destination: Path, title: str
    ) -> ParsedDocument: ...


class KnowledgeAgentPort(Protocol):
    @property
    def name(self) -> str: ...

    def build_plan(self, document: ParsedDocument) -> KnowledgePlan: ...

    def profile_document(self, document: ParsedDocument) -> DocumentProfile: ...

    def build_knowledge_map(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> KnowledgeMap: ...

    def knowledge_windows(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> list[KnowledgeWindow]: ...

    def extract_knowledge_window(
        self, document: ParsedDocument, profile: DocumentProfile, window: KnowledgeWindow
    ) -> tuple[list[KnowledgeConceptCandidate], str | None]: ...

    def plan_source_note_bundle(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle: ...

    def plan_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
    ) -> NoteSetPlan: ...

    def plan_source_note_outline(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNoteOutlinePlan: ...

    def review_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> NoteSetReview: ...

    def evaluate_plan(
        self,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap | None = None,
    ) -> list[NoteEvaluation]: ...

    def build_generation(self, document: ParsedDocument) -> GenerationBundle: ...

    def answer(self, question: str, hits: list[SearchHit]) -> Answer: ...


class BlobStorePort(Protocol):
    def put(self, source: Path, media_type: str) -> tuple[str, str, int]: ...

    def path_for(self, storage_key: str) -> Path: ...


class EmbeddingPort(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class NotePublisherPort(Protocol):
    def publish(self, note_id: str, version_id: str) -> str: ...
