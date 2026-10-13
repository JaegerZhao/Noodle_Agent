from __future__ import annotations

from typing import Any

from .domain import (
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    SourceNoteOutlinePlan,
    SourceReleaseReview,
)
from .quality import note_content_sha256


def apply_outline_title_patches(
    outline: SourceNoteOutlinePlan,
    review: SourceReleaseReview | None,
) -> SourceNoteOutlinePlan:
    if review is None:
        outline.learning_goal = ""
        for node in outline.nodes:
            node.learning_goal = ""
        return outline
    patches = {
        patch.stable_key: patch.title.strip()
        for patch in review.outline_title_patches
        if patch.title.strip()
    }
    if not patches and not review.summary_patch.strip():
        outline.learning_goal = ""
        for node in outline.nodes:
            node.learning_goal = ""
        return outline
    updated = outline.model_copy(deep=True)
    if review.summary_patch.strip():
        updated.summary = review.summary_patch.strip()
    updated.learning_goal = ""
    for node in updated.nodes:
        node.learning_goal = ""
    for node in updated.nodes:
        if node.stable_key in patches:
            node.title = patches[node.stable_key]
    return updated


def build_release_manifest(
    *,
    outline: SourceNoteOutlinePlan,
    note_set: NoteSetPlan,
    notes: list[NoteDraft],
    evaluations: list[NoteEvaluation],
    failures: list[dict[str, Any]],
    review: SourceReleaseReview | None,
) -> dict[str, Any]:
    """Build the only canonical, resumable post-repair release payload."""

    note_keys = {note.stable_key for note in notes}
    return {
        "schema_version": "source-note-release-v1",
        "outline": outline.model_dump(),
        "note_set_plan": {
            **note_set.model_dump(),
            "notes": [spec.model_dump() for spec in note_set.notes if spec.stable_key in note_keys],
        },
        "notes": [
            {**note.model_dump(), "content_sha256": note_content_sha256(note)} for note in notes
        ],
        "evaluations": [
            evaluation.model_dump()
            for evaluation in evaluations
            if evaluation.stable_key in note_keys
        ],
        "rejected": failures,
        "source_review": review.model_dump() if review is not None else None,
    }
