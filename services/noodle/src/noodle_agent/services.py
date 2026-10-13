from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from sqlalchemy.orm import Session

from .adapters.blob import LocalBlobStore
from .adapters.llm import build_embedding, build_knowledge_agent
from .adapters.mineru import build_parser
from .adapters.vision import build_vision_provider
from .application.activation import ActivationModule
from .application.attachments import AttachmentsModule
from .application.common import (
    TERMINAL_STATUSES,
)
from .application.editing import EditingModule
from .application.exporting import ExportingModule
from .application.generation_tasks import GenerationTasksModule
from .application.jobs import JobsModule
from .application.knowledge import KnowledgeModule
from .application.parsing import ParsingModule
from .application.persistence import PersistenceModule
from .application.planning import PlanningModule
from .application.reading import ReadingModule
from .application.retrieval import RetrievalModule
from .application.sources import SourcesModule
from .application.validation import ValidationModule
from .config import Settings
from .db import (
    AtomicNote,
    Attachment,
    BlobObject,
    Database,
    Job,
    NoteVersion,
    SourceNote,
    SourceNoteVersion,
    VisualAsset,
)
from .domain import (
    Answer,
    DocumentProfile,
    GenerationBundle,
    JobStage,
    KnowledgeMap,
    KnowledgePlan,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedDocument,
    SearchHit,
    SourceNoteOutlinePlan,
)
from .synthesis import (
    BackboneSourceMigrator,
    D2LSourceImporter,
    SemanticSynthesisBuilder,
    SynthesisService,
)
from .synthesis.curation import KnowledgeCurationService
from .synthesis.knowledge_sync import KnowledgeSyncService
from .telemetry import TelemetryRecorder


class NoodleService:
    """Public service boundary; implementation is composed by responsibility."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_directories()
        self.db = Database(settings.database_url)
        self.telemetry = TelemetryRecorder(self.db, settings.data_dir)
        self.blobs = LocalBlobStore(settings.blob_dir)
        self.parser = build_parser(settings)
        self.agent = build_knowledge_agent(settings, self.telemetry)
        self.embedding = build_embedding(settings, self.telemetry)
        self.vision = build_vision_provider(settings, self.telemetry)
        self.synthesis = SynthesisService(self.db, self.agent)
        self.knowledge_curation = KnowledgeCurationService(self.db, self.embedding)
        self.backbone_migrator = BackboneSourceMigrator(self.db, self.blobs)
        self.d2l_importer = D2LSourceImporter(self.db, self.blobs)
        self.semantic_synthesis = SemanticSynthesisBuilder(self.db, self.agent)
        self.knowledge_sync = KnowledgeSyncService(self)
        self._knowledge_module = KnowledgeModule(self)
        self._sources_module = SourcesModule(self)
        self._jobs_module = JobsModule(self)
        self._parsing_module = ParsingModule(self)
        self._generation_tasks_module = GenerationTasksModule(self)
        self._planning_module = PlanningModule(self)
        self._activation_module = ActivationModule(self)
        self._persistence_module = PersistenceModule(self)
        self._validation_module = ValidationModule(self)
        self._reading_module = ReadingModule(self)
        self._attachments_module = AttachmentsModule(self)
        self._editing_module = EditingModule(self)
        self._exporting_module = ExportingModule(self)
        self._retrieval_module = RetrievalModule(self)

    def init(self) -> None:
        self.db.init()

    def close(self) -> None:
        self.db.close()

    def register_backbone(self, pdf_path: Path) -> dict[str, Any]:
        return self._knowledge_module.register_backbone(pdf_path)

    def extract_backbone_figures(self, corpus_id: str) -> dict[str, Any]:
        return self._knowledge_module.extract_backbone_figures(corpus_id)

    def translate_backbone_figure_captions(
        self, corpus_id: str, *, batch_size: int = 16, concurrency: int = 4, force: bool = False
    ) -> dict[str, Any]:
        return self._knowledge_module.translate_backbone_figure_captions(
            corpus_id, batch_size=batch_size, concurrency=concurrency, force=force
        )

    def import_d2l_source(self, repository: Path) -> dict[str, Any]:
        return self._knowledge_module.import_d2l_source(repository)

    def migrate_backbone_source(self, corpus_id: str) -> dict[str, Any]:
        return self._knowledge_module.migrate_backbone_source(corpus_id)

    def build_semantic_synthesis(
        self, corpus_id: str, source_version_id: str, *, concurrency: int = 4
    ) -> dict[str, Any]:
        return self._knowledge_module.build_semantic_synthesis(
            corpus_id, source_version_id, concurrency=concurrency
        )

    def refresh_shared_concept_versions(self) -> dict[str, Any]:
        return self._knowledge_module.refresh_shared_concept_versions()

    def accept_atomic_note_contribution(
        self,
        *,
        concept_id: str,
        module_key: str,
        note_version_id: str,
        expected_concept_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._knowledge_module.accept_atomic_note_contribution(
            concept_id=concept_id,
            module_key=module_key,
            note_version_id=note_version_id,
            expected_concept_version_id=expected_concept_version_id,
            rationale=rationale,
            actor=actor,
        )

    def run_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self._knowledge_module.run_concept_update_job(job_id)

    def get_concept_update_job(self, job_id: str) -> dict[str, Any]:
        return self._knowledge_module.get_concept_update_job(job_id)

    def list_concept_update_jobs(self, concept_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._knowledge_module.list_concept_update_jobs(concept_id, limit=limit)

    def list_concept_contributions(
        self, concept_id: str, *, module_key: str | None = None
    ) -> list[dict[str, Any]]:
        return self._knowledge_module.list_concept_contributions(concept_id, module_key=module_key)

    def transition_concept_contribution(self, **kwargs: Any) -> dict[str, Any]:
        return self._knowledge_module.transition_concept_contribution(**kwargs)

    def retry_concept_update_job(self, job_id: str, *, actor: str) -> dict[str, Any]:
        return self._knowledge_module.retry_concept_update_job(job_id, actor=actor)

    def suggest_concept_candidates(
        self, concept_id: str, module_key: str, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        return self._knowledge_module.suggest_concept_candidates(
            concept_id, module_key, limit=limit
        )

    def get_concept_update_diff(self, job_id: str) -> dict[str, Any]:
        return self._knowledge_module.get_concept_update_diff(job_id)

    def get_backbone_figure_path(self, figure_id: str) -> Path:
        return self._knowledge_module.get_backbone_figure_path(figure_id)

    def enrich_backbone(
        self,
        corpus_id: str,
        *,
        limit: int | None = None,
        concurrency: int | None = None,
        include_published: bool = False,
        long_leaves_only: bool = False,
    ) -> dict[str, Any]:
        return self._knowledge_module.enrich_backbone(
            corpus_id,
            limit=limit,
            concurrency=concurrency,
            include_published=include_published,
            long_leaves_only=long_leaves_only,
        )

    def list_syntheses(self) -> list[dict[str, Any]]:
        return self._knowledge_module.list_syntheses()

    def get_synthesis(self, synthesis_id: str) -> dict[str, Any]:
        return self._knowledge_module.get_synthesis(synthesis_id)

    def get_concept(self, concept_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        return self._knowledge_module.get_concept(concept_id, version_id=version_id)

    def list_concepts(self, query: str = "", *, limit: int = 50) -> list[dict[str, Any]]:
        return self._knowledge_module.list_concepts(query, limit=limit)

    def create_synthesis(
        self,
        *,
        stable_key: str,
        domain_key: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self._knowledge_module.create_synthesis(
            stable_key=stable_key,
            domain_key=domain_key,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def publish_synthesis(
        self,
        synthesis_id: str,
        *,
        expected_version_id: str,
        title: str,
        description: str,
        outline: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        return self._knowledge_module.publish_synthesis(
            synthesis_id,
            expected_version_id=expected_version_id,
            title=title,
            description=description,
            outline=outline,
            actor=actor,
        )

    def merge_concepts(
        self,
        *,
        source_concept_id: str,
        target_concept_id: str,
        expected_source_version_id: str,
        expected_target_version_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._knowledge_module.merge_concepts(
            source_concept_id=source_concept_id,
            target_concept_id=target_concept_id,
            expected_source_version_id=expected_source_version_id,
            expected_target_version_id=expected_target_version_id,
            rationale=rationale,
            actor=actor,
        )

    def __enter__(self) -> Self:
        self.init()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def ingest_pdf(
        self,
        pdf_path: Path,
        *,
        title: str | None = None,
        source_id: str | None = None,
        force_new_version: bool = False,
    ) -> dict[str, str]:
        return self._sources_module.ingest_pdf(
            pdf_path, title=title, source_id=source_id, force_new_version=force_new_version
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._jobs_module.get_job(job_id)

    def get_job_result(self, job_id: str) -> dict[str, Any]:
        return self._jobs_module.get_job_result(job_id)

    def review_existing_release(self, job_id: str) -> dict[str, Any]:
        return self._jobs_module.review_existing_release(job_id)

    def list_library_folders(self) -> list[dict[str, Any]]:
        return self._sources_module.list_library_folders()

    def create_library_folder(self, name: str, *, parent_id: str | None = None) -> dict[str, Any]:
        return self._sources_module.create_library_folder(name, parent_id=parent_id)

    def rename_library_folder(self, folder_id: str, name: str) -> dict[str, Any]:
        return self._sources_module.rename_library_folder(folder_id, name)

    def move_library_source(self, source_id: str, *, folder_id: str | None) -> dict[str, Any]:
        return self._sources_module.move_library_source(source_id, folder_id=folder_id)

    def reorder_library_sources(self, folder_id: str, source_ids: list[str]) -> dict[str, Any]:
        return self._sources_module.reorder_library_sources(folder_id, source_ids)

    def get_library_overview(self) -> dict[str, Any]:
        return self._sources_module.get_library_overview()

    def get_source_pdf(self, source_id: str) -> dict[str, Any]:
        return self._sources_module.get_source_pdf(source_id)

    def list_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._jobs_module.list_jobs(limit)

    def get_job_timeline(self, job_id: str) -> dict[str, Any]:
        return self._jobs_module.get_job_timeline(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        return self._jobs_module.resume_job(job_id)

    def delete_source_local_data(
        self, source_id: str, *, actor: str = "local-user"
    ) -> dict[str, Any]:
        return self._sources_module.delete_source_local_data(source_id, actor=actor)

    def restart_generation(
        self, job_id: str, from_step: str, *, actor: str = "local-user"
    ) -> dict[str, Any]:
        return self._jobs_module.restart_generation(job_id, from_step, actor=actor)

    @staticmethod
    def _job_dict(job: Job) -> dict[str, Any]:
        return JobsModule._job_dict(job)

    def link_job_to_harness(
        self, job_id: str, *, session_id: str | None, tool_call_id: str | None
    ) -> None:
        return self._jobs_module.link_job_to_harness(
            job_id, session_id=session_id, tool_call_id=tool_call_id
        )

    def run_job_once(self, job_id: str, *, force_poll: bool = False) -> dict[str, Any]:
        return self._jobs_module.run_job_once(job_id, force_poll=force_poll)

    def run_job_until_terminal(
        self, job_id: str, *, timeout_seconds: float = 1800.0
    ) -> dict[str, Any]:
        return self._jobs_module.run_job_until_terminal(job_id, timeout_seconds=timeout_seconds)

    def _submit_parse(self, job_id: str) -> None:
        return self._parsing_module._submit_parse(job_id)

    def _poll_or_normalize(self, job_id: str) -> None:
        return self._parsing_module._poll_or_normalize(job_id)

    def _persist_parsed_document(
        self, job_id: str, document: ParsedDocument, normalized_path: Path
    ) -> None:
        return self._parsing_module._persist_parsed_document(job_id, document, normalized_path)

    def _update_generation_stage(
        self, job_id: str, stage: JobStage, progress: float, **state_updates: Any
    ) -> None:
        return self._generation_tasks_module._update_generation_stage(
            job_id, stage, progress, **state_updates
        )

    def _invoke_agent(
        self, job_id: str, stage: JobStage, method, *args, note_key: str | None = None, **kwargs
    ):
        return self._generation_tasks_module._invoke_agent(
            job_id, stage, method, *args, note_key=note_key, **kwargs
        )

    def _invoke_agent_with_task_retries(
        self, job_id: str, stage: JobStage, method, *args, note_key: str | None = None, **kwargs
    ):
        return self._generation_tasks_module._invoke_agent_with_task_retries(
            job_id, stage, method, *args, note_key=note_key, **kwargs
        )

    @staticmethod
    def _read_generation_artifact(path: Path) -> dict[str, Any]:
        return GenerationTasksModule._read_generation_artifact(path)

    @staticmethod
    def _write_generation_artifact(path: Path, payload: dict[str, Any]) -> None:
        return GenerationTasksModule._write_generation_artifact(path, payload)

    def _analyze_planned_visuals(
        self, job_id: str, document: ParsedDocument, note_set: NoteSetPlan
    ) -> dict[str, dict[str, Any]]:
        return self._generation_tasks_module._analyze_planned_visuals(job_id, document, note_set)

    def _draft_specs_concurrently(
        self,
        *,
        job_id: str,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        specs: list[NoteSpec],
        vision_context: dict[str, dict[str, Any]],
        artifact_path: Path,
        payload: dict[str, Any],
        drafts: dict[str, Any],
    ) -> dict[str, Any]:
        return self._generation_tasks_module._draft_specs_concurrently(
            job_id=job_id,
            document=document,
            profile=profile,
            knowledge_map=knowledge_map,
            note_set=note_set,
            specs=specs,
            vision_context=vision_context,
            artifact_path=artifact_path,
            payload=payload,
            drafts=drafts,
        )

    def _evaluate_notes_batched(
        self,
        job_id: str,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        return self._generation_tasks_module._evaluate_notes_batched(
            job_id, document, note_set, plan, knowledge_map, vision_context
        )

    def _build_generation_resumable(
        self, job_id: str, document: ParsedDocument, normalized_path: Path
    ) -> GenerationBundle:
        return self._planning_module._build_generation_resumable(job_id, document, normalized_path)

    def _generate_and_activate(self, job_id: str) -> None:
        return self._activation_module._generate_and_activate(job_id)

    def _record_candidate_results(
        self,
        knowledge_map_id: str,
        *,
        passed: list,
        failures: list[dict[str, Any]],
        repair_attempted_keys: set[str],
    ) -> None:
        return self._persistence_module._record_candidate_results(
            knowledge_map_id,
            passed=passed,
            failures=failures,
            repair_attempted_keys=repair_attempted_keys,
        )

    def _record_quality_evaluations(
        self, knowledge_map_id: str, evaluations: list[NoteEvaluation]
    ) -> None:
        return self._persistence_module._record_quality_evaluations(knowledge_map_id, evaluations)

    @staticmethod
    def _deterministic_note_set_review(
        knowledge_map: KnowledgeMap, note_set: NoteSetPlan
    ) -> NoteSetReview:
        return ValidationModule._deterministic_note_set_review(knowledge_map, note_set)

    @staticmethod
    def _normalize_combined_plan_contracts(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        return ValidationModule._normalize_combined_plan_contracts(
            document, knowledge_map, outline, note_set
        )

    @staticmethod
    def _normalize_minimum_outline_depth(
        outline: SourceNoteOutlinePlan, note_set: NoteSetPlan
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan, list[dict[str, Any]]]:
        return ValidationModule._normalize_minimum_outline_depth(outline, note_set)

    @staticmethod
    def _prune_unbound_outline_leaves(
        outline: SourceNoteOutlinePlan, note_set: NoteSetPlan
    ) -> SourceNoteOutlinePlan:
        return ValidationModule._prune_unbound_outline_leaves(outline, note_set)

    @staticmethod
    def _validate_source_note_outline(outline: SourceNoteOutlinePlan) -> list[str]:
        return ValidationModule._validate_source_note_outline(outline)

    @staticmethod
    def _validate_note_set(
        document: ParsedDocument,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        review: NoteSetReview,
        source_note_outline: SourceNoteOutlinePlan | None = None,
    ) -> list[str]:
        return ValidationModule._validate_note_set(
            document, knowledge_map, note_set, review, source_note_outline
        )

    @staticmethod
    def _normalize_table_shapes(plan: KnowledgePlan) -> tuple[KnowledgePlan, list[dict[str, Any]]]:
        return ValidationModule._normalize_table_shapes(plan)

    @staticmethod
    def _validate_plan(
        document: ParsedDocument,
        plan: KnowledgePlan,
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list, list[dict[str, Any]]]:
        return ValidationModule._validate_plan(document, plan, note_set, vision_context)

    @staticmethod
    def _fallback_source_note_plan(
        source_title: str, plan: KnowledgePlan
    ) -> tuple[SourceNoteOutlinePlan, NoteSetPlan]:
        return ValidationModule._fallback_source_note_plan(source_title, plan)

    def _persist_source_note(
        self,
        *,
        job_id: str,
        source_id: str,
        source_version_id: str,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        passed_notes: list[NoteDraft],
    ) -> str:
        return self._persistence_module._persist_source_note(
            job_id=job_id,
            source_id=source_id,
            source_version_id=source_version_id,
            outline=outline,
            note_set=note_set,
            passed_notes=passed_notes,
        )

    @staticmethod
    def _reconcile_note_version_assets(
        session: Session, *, note_version: NoteVersion, parse_run_id: str, note: NoteDraft
    ) -> int:
        return PersistenceModule._reconcile_note_version_assets(
            session, note_version=note_version, parse_run_id=parse_run_id, note=note
        )

    def _persist_notes(
        self,
        job_id: str,
        source_id: str,
        source_version_id: str,
        notes: list,
        *,
        publish_release: bool = True,
    ) -> list[str]:
        return self._persistence_module._persist_notes(
            job_id, source_id, source_version_id, notes, publish_release=publish_release
        )

    def _fail_job(self, job_id: str, exc: Exception) -> None:
        return self._jobs_module._fail_job(job_id, exc)

    def backfill_source_notes(self) -> dict[str, int]:
        return self._reading_module.backfill_source_notes()

    def _active_note_identities(self, source_version_id: str) -> list[dict[str, str]]:
        return self._reading_module._active_note_identities(source_version_id)

    @staticmethod
    def _attachment_payload(attachment: Attachment, blob: BlobObject) -> dict[str, Any]:
        return AttachmentsModule._attachment_payload(attachment, blob)

    def create_agent_attachment(
        self,
        image_path: Path,
        *,
        original_name: str | None = None,
        media_type: str | None = None,
        conversation_id: str | None = None,
        analyze: bool = True,
    ) -> dict[str, Any]:
        return self._attachments_module.create_agent_attachment(
            image_path,
            original_name=original_name,
            media_type=media_type,
            conversation_id=conversation_id,
            analyze=analyze,
        )

    def get_agent_attachments(self, attachment_ids: list[str]) -> list[dict[str, Any]]:
        return self._attachments_module.get_agent_attachments(attachment_ids)

    def claim_agent_attachments(
        self, attachment_ids: list[str], *, conversation_id: str, message_id: str
    ) -> None:
        return self._attachments_module.claim_agent_attachments(
            attachment_ids, conversation_id=conversation_id, message_id=message_id
        )

    @staticmethod
    def _numeric_page_label(value: str) -> int | None:
        return EditingModule._numeric_page_label(value)

    @staticmethod
    def _select_edit_target(
        affected: list[tuple[AtomicNote, NoteVersion]],
        *,
        target_note_title: str | None,
        vision_text: str,
    ) -> tuple[AtomicNote, NoteVersion]:
        return EditingModule._select_edit_target(
            affected, target_note_title=target_note_title, vision_text=vision_text
        )

    def _clone_note_for_image_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        removed_visual_ids: set[str],
        replacement_visual: VisualAsset,
        add_replacement: bool,
        attachment: Attachment,
        caption: str,
        purpose: str,
        actor: str,
    ) -> NoteVersion:
        return self._editing_module._clone_note_for_image_edit(
            session,
            atomic=atomic,
            parent=parent,
            removed_visual_ids=removed_visual_ids,
            replacement_visual=replacement_visual,
            add_replacement=add_replacement,
            attachment=attachment,
            caption=caption,
            purpose=purpose,
            actor=actor,
        )

    def _clone_note_for_block_edit(
        self,
        session: Session,
        *,
        atomic: AtomicNote,
        parent: NoteVersion,
        block_key: str,
        new_text: str,
        actor: str,
    ) -> NoteVersion:
        return self._editing_module._clone_note_for_block_edit(
            session,
            atomic=atomic,
            parent=parent,
            block_key=block_key,
            new_text=new_text,
            actor=actor,
        )

    def edit_note_block(
        self,
        note_id: str,
        *,
        expected_version_id: str,
        block_key: str,
        new_text: str,
        actor: str = "user-via-harness",
    ) -> dict[str, Any]:
        return self._editing_module.edit_note_block(
            note_id,
            expected_version_id=expected_version_id,
            block_key=block_key,
            new_text=new_text,
            actor=actor,
        )

    @staticmethod
    def _clone_source_note_release(
        session: Session,
        *,
        source_note: SourceNote,
        parent: SourceNoteVersion,
        actor: str,
        edit_signature: dict[str, Any],
    ) -> SourceNoteVersion:
        return EditingModule._clone_source_note_release(
            session,
            source_note=source_note,
            parent=parent,
            actor=actor,
            edit_signature=edit_signature,
        )

    def replace_source_note_images(
        self,
        *,
        attachment_id: str,
        page_start: int,
        page_end: int,
        source_note_id: str | None = None,
        source_ref: str | None = None,
        target_note_title: str | None = None,
        caption: str | None = None,
        purpose: str | None = None,
        expected_source_note_version_id: str | None = None,
        actor: str = "user-via-agent",
    ) -> dict[str, Any]:
        return self._editing_module.replace_source_note_images(
            attachment_id=attachment_id,
            page_start=page_start,
            page_end=page_end,
            source_note_id=source_note_id,
            source_ref=source_ref,
            target_note_title=target_note_title,
            caption=caption,
            purpose=purpose,
            expected_source_note_version_id=expected_source_note_version_id,
            actor=actor,
        )

    def list_source_notes(self) -> list[dict[str, Any]]:
        return self._reading_module.list_source_notes()

    def get_source_note(self, source_note_id: str) -> dict[str, Any]:
        return self._reading_module.get_source_note(source_note_id)

    def list_notes(self) -> list[dict[str, Any]]:
        return self._reading_module.list_notes()

    def get_note(self, note_id: str, *, version_id: str | None = None) -> dict[str, Any]:
        return self._reading_module.get_note(note_id, version_id=version_id)

    def reconcile_source_note_release(self, source_note_id: str) -> dict[str, int]:
        return self._reading_module.reconcile_source_note_release(source_note_id)

    def export_markdown(self, source_version_id: str | None = None) -> list[dict[str, Any]]:
        return self._exporting_module.export_markdown(source_version_id)

    def _export_source_markdown(self, source_version_id: str) -> dict[str, Any]:
        return self._exporting_module._export_source_markdown(source_version_id)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        return self._retrieval_module.search(
            query, limit=limit, min_score=min_score, source_version_id=source_version_id
        )

    def search_knowledge(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        source_version_id: str | None = None,
    ) -> list[SearchHit]:
        return self._retrieval_module.search_knowledge(
            query, limit=limit, min_score=min_score, source_version_id=source_version_id
        )

    @staticmethod
    def _fts_query(query: str) -> str:
        return RetrievalModule._fts_query(query)

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        return RetrievalModule._lexical_similarity(left, right)

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return RetrievalModule._cosine(left, right)

    @staticmethod
    def _evidence_for_version(session: Session, version_id: str) -> list[dict[str, Any]]:
        return RetrievalModule._evidence_for_version(session, version_id)

    def _assets_for_version(self, session: Session, version_id: str) -> list[dict[str, Any]]:
        return self._retrieval_module._assets_for_version(session, version_id)

    def ask(self, question: str, *, limit: int = 5, source_version_id: str | None = None) -> Answer:
        return self._retrieval_module.ask(
            question, limit=limit, source_version_id=source_version_id
        )

    def rebuild_index(self) -> int:
        return self._retrieval_module.rebuild_index()


__all__ = ["TERMINAL_STATUSES", "NoodleService"]
