from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class KnowledgeSyncRetryRequest(BaseModel):
    module_ids: list[str] = Field(default_factory=list, max_length=100)


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    source_version_id: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class RestartGenerationRequest(BaseModel):
    from_step: str = Field(
        pattern="^(profile|knowledge-map|outline|note-set|vision|drafts|evaluation|validation)$"
    )


class EditNoteBlockRequest(BaseModel):
    expected_version_id: str = Field(min_length=1)
    block_key: str = Field(min_length=1, max_length=200)
    new_text: str = Field(min_length=1, max_length=100_000)


class AcceptConceptContributionRequest(BaseModel):
    note_version_id: str = Field(min_length=1)
    expected_concept_version_id: str = Field(min_length=1)
    module_key: str = Field(min_length=1, max_length=180)
    rationale: str = Field(min_length=1, max_length=2_000)


class TransitionConceptContributionRequest(BaseModel):
    action: Literal["REJECT", "RETRACT", "SUPERSEDE"]
    expected_concept_version_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    replacement_note_version_id: str | None = None


class SynthesisOutlineNodeRequest(BaseModel):
    stable_key: str | None = Field(default=None, max_length=220)
    title: str = Field(min_length=1, max_length=500)
    node_kind: Literal["GROUP", "CONCEPT"] = "CONCEPT"
    concept_id: str | None = None
    children: list[SynthesisOutlineNodeRequest] = Field(default_factory=list)


class CreateSynthesisRequest(BaseModel):
    stable_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,159}$")
    domain_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4_000)
    outline: list[SynthesisOutlineNodeRequest] = Field(min_length=1, max_length=200)


class PublishSynthesisRequest(BaseModel):
    expected_version_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=4_000)
    outline: list[SynthesisOutlineNodeRequest] = Field(min_length=1, max_length=200)


class MergeConceptsRequest(BaseModel):
    source_concept_id: str = Field(min_length=1)
    target_concept_id: str = Field(min_length=1)
    expected_source_version_id: str = Field(min_length=1)
    expected_target_version_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class ReplaceImagesRequest(BaseModel):
    attachment_id: str = Field(min_length=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    expected_source_note_version_id: str = Field(min_length=1)
    target_note_title: str | None = None
    caption: str | None = None
    purpose: str | None = None


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = None


class FolderRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MoveSourceRequest(BaseModel):
    folder_id: str | None = None


class ReorderSourcesRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=500)


class HarnessSessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class HarnessSessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class HarnessPromptRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=100_000)
    context: str | None = Field(default=None, max_length=8_000)
    mode: str = Field(default="queue", pattern="^(queue|steer)$")


class HarnessApprovalDecisionRequest(BaseModel):
    outcome: Literal["allowed-once", "rejected"]


class IngestResponse(BaseModel):
    source_id: str
    source_version_id: str
    job_id: str
    deduplicated: str
    job_url: str
    timeline_url: str
    result_url: str
