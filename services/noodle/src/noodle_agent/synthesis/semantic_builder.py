from __future__ import annotations

from threading import Lock, RLock
from typing import Any

from ..db import (
    BackboneSection,
    Concept,
    Database,
)
from .blueprints import MACHINE_LEARNING, SynthesisBlueprint
from .domain import (
    ConceptModuleJudgeResult,
    SemanticConceptDraft,
    SemanticConceptModuleDraft,
    SemanticSynthesisReport,
)
from .machine_learning_blueprint import ConceptSpec
from .semantic.common import (
    ConceptUpdateError,
    ModuleReviewRequired,
    _canonical_module_role,
)
from .semantic.compilation import CompilationModule
from .semantic.contributions import ContributionsModule
from .semantic.inputs import InputsModule
from .semantic.publication import PublicationModule
from .semantic.review import ReviewModule
from .semantic.writing import WritingModule


class SemanticSynthesisBuilder:
    """Public service boundary; implementation is composed by responsibility."""

    def __init__(
        self,
        database: Database,
        knowledge_agent: Any,
        blueprint: SynthesisBlueprint = MACHINE_LEARNING,
    ):
        self.db = database
        self.agent = knowledge_agent
        self.blueprint = blueprint
        self._concept_locks_guard = Lock()
        self._concept_locks: dict[str, RLock] = {}
        self._inputs_module = InputsModule(self)
        self._writing_module = WritingModule(self)
        self._contributions_module = ContributionsModule(self)
        self._review_module = ReviewModule(self)
        self._compilation_module = CompilationModule(self)
        self._publication_module = PublicationModule(self)

    def _concept_lock(self, stable_key: str) -> RLock:
        return self._inputs_module._concept_lock(stable_key)

    def _global_spec(self, spec: ConceptSpec) -> dict[str, Any]:
        return self._inputs_module._global_spec(spec)

    def _owner(self, number: str) -> tuple[ConceptSpec, str] | None:
        return self._inputs_module._owner(number)

    @staticmethod
    def _source_text(note: dict[str, Any], *, max_chars: int = 6000) -> str:
        return InputsModule._source_text(note, max_chars=max_chars)

    def _prepare_inputs(
        self, corpus_id: str, source_version_id: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        return self._inputs_module._prepare_inputs(corpus_id, source_version_id)

    def _source_ref(self, section: BackboneSection) -> str:
        return self._inputs_module._source_ref(section)

    def _register_foundation_contributions(self, prepared: dict[str, dict[str, Any]]) -> None:
        return self._inputs_module._register_foundation_contributions(prepared)

    def _prepared_from_contributions(
        self,
        spec: ConceptSpec,
        *,
        include_contribution_ids: set[str] | None = None,
        exclude_contribution_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        return self._inputs_module._prepared_from_contributions(
            spec,
            include_contribution_ids=include_contribution_ids,
            exclude_contribution_ids=exclude_contribution_ids,
        )

    @staticmethod
    def _fallback_module(seed: dict[str, Any]) -> SemanticConceptModuleDraft:
        return WritingModule._fallback_module(seed)

    @staticmethod
    def _v3_module_candidate(raw: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
        return WritingModule._v3_module_candidate(raw, seed)

    def _normalize_draft(
        self, payload: dict[str, Any], spec: ConceptSpec, modules: list[dict[str, Any]]
    ) -> tuple[SemanticConceptDraft, bool, str]:
        return self._writing_module._normalize_draft(payload, spec, modules)

    def _write_draft(
        self,
        spec: ConceptSpec,
        modules: list[dict[str, Any]],
        update_context: dict[str, Any] | None = None,
    ) -> tuple[SemanticConceptDraft, bool, str]:
        return self._writing_module._write_draft(spec, modules, update_context)

    def _ensure_concepts(self) -> None:
        return self._contributions_module._ensure_concepts()

    def accept_atomic_note(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        run_inline: bool = True,
    ) -> dict[str, Any]:
        return self._contributions_module.accept_atomic_note(
            concept_id=concept_id,
            module_key=module_key,
            note_version_id=note_version_id,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
            run_inline=run_inline,
        )

    def _accept_atomic_note_locked(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        run_inline: bool,
    ) -> dict[str, Any]:
        return self._contributions_module._accept_atomic_note_locked(
            concept_id=concept_id,
            module_key=module_key,
            note_version_id=note_version_id,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
            run_inline=run_inline,
        )

    def transition_contribution(
        self,
        *,
        contribution_id: str,
        action: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
        replacement_note_version_id: str | None = None,
    ) -> dict[str, Any]:
        return self._contributions_module.transition_contribution(
            contribution_id=contribution_id,
            action=action,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
            replacement_note_version_id=replacement_note_version_id,
        )

    def retry_concept_update_job(self, job_id: str, *, actor: str) -> dict[str, Any]:
        return self._contributions_module.retry_concept_update_job(job_id, actor=actor)

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self._contributions_module.run_concept_update_job(job_id)

    def _run_concept_update_job_locked(self, job_id: str) -> dict[str, Any]:
        return self._contributions_module._run_concept_update_job_locked(job_id)

    @staticmethod
    def _release_contains_contribution(
        session, concept_version_id: str | None, module_id: str, contribution_id: str
    ) -> bool:
        return ReviewModule._release_contains_contribution(
            session, concept_version_id, module_id, contribution_id
        )

    @staticmethod
    def _validate_update_draft(draft: SemanticConceptDraft, module_seed: dict[str, Any]) -> None:
        return ReviewModule._validate_update_draft(draft, module_seed)

    def _update_context(self, job_id: str) -> dict[str, Any]:
        return self._review_module._update_context(job_id)

    def _judge_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        job_id: str,
        attempt_no: int,
    ) -> ConceptModuleJudgeResult:
        return self._review_module._judge_module_candidate(
            spec=spec,
            module_seed=module_seed,
            module_draft=module_draft,
            job_id=job_id,
            attempt_no=attempt_no,
        )

    def _repair_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        judge: ConceptModuleJudgeResult,
        job_id: str,
    ) -> SemanticConceptModuleDraft:
        return self._review_module._repair_module_candidate(
            spec=spec,
            module_seed=module_seed,
            module_draft=module_draft,
            judge=judge,
            job_id=job_id,
        )

    def _repair_module_schema(self, candidate, module_seed, detail):
        return self._review_module._repair_module_schema(candidate, module_seed, detail)

    def _align_declared_provenance(self, draft, module_seed):
        return self._review_module._align_declared_provenance(draft, module_seed)

    def _validate_or_repair_update_draft(self, draft, module_seed):
        return self._review_module._validate_or_repair_update_draft(draft, module_seed)

    def _compile_module_update_candidate(
        self, prepared: dict[str, Any], *, concept_id: str, base_version_id: str, job_id: str
    ) -> dict[str, Any]:
        return self._compilation_module._compile_module_update_candidate(
            prepared, concept_id=concept_id, base_version_id=base_version_id, job_id=job_id
        )

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self._compilation_module.get_concept_update_job(job_id)

    def list_concept_update_jobs(self, concept_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._compilation_module.list_concept_update_jobs(concept_id, limit=limit)

    def list_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        return self._compilation_module.list_contributions(concept_id, module_key=module_key)

    def _compile_one(self, prepared: dict[str, Any]) -> dict[str, Any]:
        return self._compilation_module._compile_one(prepared)

    def _compile_one_locked(self, prepared: dict[str, Any]) -> dict[str, Any]:
        return self._publication_module._compile_one_locked(prepared)

    def _refresh_synthesis_releases(
        self,
        concept_versions: dict[str, str],
        *,
        source_version_id: str | None = None,
        source_role: str = "SUPPORT",
        exclude_synthesis_id: str | None = None,
    ) -> list[str]:
        return self._publication_module._refresh_synthesis_releases(
            concept_versions,
            source_version_id=source_version_id,
            source_role=source_role,
            exclude_synthesis_id=exclude_synthesis_id,
        )

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        return self._publication_module.refresh_shared_concept_versions()

    def _publish_synthesis(
        self,
        corpus_id: str,
        source_version_id: str,
        *,
        allow_replace_legacy: bool,
        compiled_versions: dict[str, str],
        failures: list[dict[str, str]],
    ) -> tuple[str, str, int]:
        return self._publication_module._publish_synthesis(
            corpus_id,
            source_version_id,
            allow_replace_legacy=allow_replace_legacy,
            compiled_versions=compiled_versions,
            failures=failures,
        )

    def _append_concept_node(
        self,
        session,
        synthesis_version_id: str,
        spec: ConceptSpec,
        concepts: dict[str, Concept],
        selected_versions: dict[str, str],
        node_ids: dict[str, str],
        sort_order: int,
        *,
        level: int,
    ) -> int:
        return self._publication_module._append_concept_node(
            session,
            synthesis_version_id,
            spec,
            concepts,
            selected_versions,
            node_ids,
            sort_order,
            level=level,
        )

    def build(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> SemanticSynthesisReport:
        return self._publication_module.build(corpus_id, source_version_id, concurrency=concurrency)


__all__ = [
    "ConceptUpdateError",
    "ModuleReviewRequired",
    "SemanticSynthesisBuilder",
    "_canonical_module_role",
]
