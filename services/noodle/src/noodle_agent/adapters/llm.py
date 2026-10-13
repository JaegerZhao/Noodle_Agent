from __future__ import annotations

from typing import Any

import httpx
from openai import OpenAI

from ..config import Settings
from ..domain import (
    Answer,
    DocumentProfile,
    GenerationBundle,
    KnowledgeConcept,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedBlock,
    ParsedDocument,
    SearchHit,
    SourceNoteOutlinePlan,
    SourceNotePlanningBundle,
    SourceReleaseReview,
    TopicSegment,
)
from ..telemetry import TelemetryRecorder
from .knowledge.common import (
    FixtureKnowledgeAgent,
    HashEmbedding,
    OpenAIEmbedding,
    _loads_structured_json,
    _recover_structured_mojibake,
    compact_reference_ids,
)
from .knowledge.extraction import ExtractionModule
from .knowledge.planning import PlanningModule
from .knowledge.transport import TransportModule
from .knowledge.writing import WritingModule


class OpenAIKnowledgeAgent:
    """Public service boundary; implementation is composed by responsibility."""

    name = "openai-compatible"

    def __init__(self, settings: Settings, telemetry: TelemetryRecorder | None = None):
        if not settings.llm_api_key or not settings.llm_fast_model:
            raise ValueError("An LLM API key and fast model are required when using a real LLM")
        timeout_seconds = float(getattr(settings, "llm_timeout_seconds", 180.0) or 180.0)
        kwargs: dict[str, Any] = {
            "api_key": settings.llm_api_key,
            "timeout": httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            "max_retries": int(getattr(settings, "llm_max_retries", 0) or 0),
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        self.client = OpenAI(**kwargs)
        self.fast_model = settings.llm_fast_model
        self.pro_model = settings.llm_pro_model or settings.llm_fast_model
        self.note_language = str(getattr(settings, "note_language", "zh-CN") or "zh-CN")
        self.telemetry = telemetry
        self.planning_single_call_concept_limit = self._positive_int_setting(
            settings, "planning_single_call_concept_limit", 18
        )
        self.planning_shard_size = self._positive_int_setting(settings, "planning_shard_size", 8)
        self.planning_concurrency = self._positive_int_setting(settings, "planning_concurrency", 3)
        self.planning_task_retries = max(
            0,
            self._nonnegative_int_setting(settings, "generation_task_retries", 1),
        )
        self._transport_module = TransportModule(self)
        self._extraction_module = ExtractionModule(self)
        self._planning_module = PlanningModule(self)
        self._writing_module = WritingModule(self)

    @staticmethod
    def _positive_int_setting(settings: Settings, name: str, default: int) -> int:
        return TransportModule._positive_int_setting(settings, name, default)

    @staticmethod
    def _nonnegative_int_setting(settings: Settings, name: str, default: int) -> int:
        return TransportModule._nonnegative_int_setting(settings, name, default)

    @property
    def _language_instruction(self) -> str:
        return self._transport_module._language_instruction

    def _json_call(
        self,
        *,
        system: str,
        user: dict[str, Any],
        model: str | None = None,
        operation: str | None = None,
        prompt_version: str | None = None,
        thinking: bool = False,
    ) -> dict:
        return self._transport_module._json_call(
            system=system,
            user=user,
            model=model,
            operation=operation,
            prompt_version=prompt_version,
            thinking=thinking,
        )

    def structured_json(
        self,
        *,
        system: str,
        user: dict[str, Any],
        operation: str,
        prompt_version: str,
        use_pro_model: bool = False,
        thinking: bool = False,
    ) -> dict[str, Any]:
        return self._transport_module.structured_json(
            system=system,
            user=user,
            operation=operation,
            prompt_version=prompt_version,
            use_pro_model=use_pro_model,
            thinking=thinking,
        )

    def build_plan(self, document: ParsedDocument) -> KnowledgePlan:
        return self._extraction_module.build_plan(document)

    def profile_document(self, document: ParsedDocument) -> DocumentProfile:
        return self._extraction_module.profile_document(document)

    @staticmethod
    def _blocks_for_segment(document: ParsedDocument, segment: TopicSegment) -> list[ParsedBlock]:
        return ExtractionModule._blocks_for_segment(document, segment)

    @staticmethod
    def _extraction_windows(
        document: ParsedDocument, segment: TopicSegment, *, character_budget: int = 24000
    ) -> list[tuple[str, list[ParsedBlock]]]:
        return ExtractionModule._extraction_windows(
            document, segment, character_budget=character_budget
        )

    def knowledge_windows(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> list[KnowledgeWindow]:
        return self._extraction_module.knowledge_windows(document, profile)

    def extract_knowledge_window(
        self, document: ParsedDocument, profile: DocumentProfile, window: KnowledgeWindow
    ) -> tuple[list[KnowledgeConceptCandidate], str | None]:
        return self._extraction_module.extract_knowledge_window(document, profile, window)

    def merge_knowledge_map(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        local_concepts: list[KnowledgeConceptCandidate],
        skipped: list[dict[str, str]],
    ) -> KnowledgeMap:
        return self._extraction_module.merge_knowledge_map(
            document, profile, local_concepts, skipped
        )

    def build_knowledge_map(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> KnowledgeMap:
        return self._extraction_module.build_knowledge_map(document, profile)

    def plan_source_note_bundle(
        self, document: ParsedDocument, profile: DocumentProfile, knowledge_map: KnowledgeMap
    ) -> SourceNotePlanningBundle:
        return self._planning_module.plan_source_note_bundle(document, profile, knowledge_map)

    def _plan_source_note_bundle_single(
        self, document: ParsedDocument, profile: DocumentProfile, knowledge_map: KnowledgeMap
    ) -> SourceNotePlanningBundle:
        return self._planning_module._plan_source_note_bundle_single(
            document, profile, knowledge_map
        )

    def _plan_source_note_bundle_sharded(
        self, document: ParsedDocument, profile: DocumentProfile, knowledge_map: KnowledgeMap
    ) -> SourceNotePlanningBundle:
        return self._planning_module._plan_source_note_bundle_sharded(
            document, profile, knowledge_map
        )

    def _planning_concept_shards(
        self, concepts: list[KnowledgeConcept]
    ) -> list[list[KnowledgeConcept]]:
        return self._planning_module._planning_concept_shards(concepts)

    @staticmethod
    def _outline_from_result(
        outline_raw: dict[str, Any], document_title: str, default_summary: str
    ) -> SourceNoteOutlinePlan:
        return PlanningModule._outline_from_result(outline_raw, document_title, default_summary)

    @staticmethod
    def _hydrate_note_set_from_concepts(
        note_set: NoteSetPlan, knowledge_map: KnowledgeMap
    ) -> NoteSetPlan:
        return PlanningModule._hydrate_note_set_from_concepts(note_set, knowledge_map)

    @staticmethod
    def _bind_notes_to_outline_leaves(
        note_set: NoteSetPlan, outline: SourceNoteOutlinePlan
    ) -> NoteSetPlan:
        return PlanningModule._bind_notes_to_outline_leaves(note_set, outline)

    def plan_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
    ) -> NoteSetPlan:
        return self._planning_module.plan_note_set(
            document, profile, knowledge_map, source_note_outline
        )

    def plan_source_note_outline(
        self, document: ParsedDocument, profile: DocumentProfile, knowledge_map: KnowledgeMap
    ) -> SourceNoteOutlinePlan:
        return self._planning_module.plan_source_note_outline(document, profile, knowledge_map)

    @staticmethod
    def _note_set_from_result(result: dict[str, Any], default_summary: str) -> NoteSetPlan:
        return PlanningModule._note_set_from_result(result, default_summary)

    def review_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> NoteSetReview:
        return self._planning_module.review_note_set(
            document, profile, knowledge_map, source_note_outline, note_set
        )

    def repair_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        review: NoteSetReview,
    ) -> NoteSetPlan:
        return self._planning_module.repair_note_set(
            document, profile, knowledge_map, source_note_outline, note_set, review
        )

    @staticmethod
    def _evidence_for_spec(document: ParsedDocument, spec: NoteSpec) -> list[dict[str, Any]]:
        return WritingModule._evidence_for_spec(document, spec)

    @staticmethod
    def _nearby_note_boundaries(note_set: NoteSetPlan, spec: NoteSpec) -> list[dict[str, str]]:
        return WritingModule._nearby_note_boundaries(note_set, spec)

    def draft_notes(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
    ) -> KnowledgePlan:
        return self._writing_module.draft_notes(document, profile, knowledge_map, note_set)

    def draft_note(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        spec: NoteSpec,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> NoteDraft:
        return self._writing_module.draft_note(
            document, profile, knowledge_map, note_set, spec, vision_context
        )

    def evaluate_plan(
        self,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        return self._writing_module.evaluate_plan(
            document, note_set, plan, knowledge_map, vision_context
        )

    def build_generation(self, document: ParsedDocument) -> GenerationBundle:
        return self._writing_module.build_generation(document)

    def repair_plan(
        self,
        document: ParsedDocument,
        original: KnowledgePlan,
        failures: list[dict[str, Any]],
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> KnowledgePlan:
        return self._writing_module.repair_plan(
            document, original, failures, note_set, vision_context
        )

    def review_source_release(
        self,
        document: ParsedDocument,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        notes: list[NoteDraft],
    ) -> SourceReleaseReview:
        return self._writing_module.review_source_release(document, outline, note_set, notes)

    def answer(self, question: str, hits: list[SearchHit]) -> Answer:
        return self._transport_module.answer(question, hits)


def build_knowledge_agent(settings: Settings, telemetry: TelemetryRecorder | None = None):
    if settings.llm == "fixture":
        return FixtureKnowledgeAgent()
    if settings.llm in {"openai", "openai-compatible", "deepseek"}:
        return OpenAIKnowledgeAgent(settings, telemetry)
    raise ValueError(f"Unsupported NOODLE_LLM: {settings.llm}")


def build_embedding(settings: Settings, telemetry: TelemetryRecorder | None = None):
    if settings.embedding_model and settings.embedding_api_key:
        return OpenAIEmbedding(settings, telemetry)
    return HashEmbedding()


__all__ = [
    "FixtureKnowledgeAgent",
    "HashEmbedding",
    "OpenAIEmbedding",
    "OpenAIKnowledgeAgent",
    "_loads_structured_json",
    "_recover_structured_mojibake",
    "build_embedding",
    "build_knowledge_agent",
    "compact_reference_ids",
]
