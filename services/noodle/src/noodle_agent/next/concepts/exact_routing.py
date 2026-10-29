"""One required primary decision per Atomic, enforced before routing persistence."""

from typing import Literal

from pydantic import Field, create_model, model_validator

from ..contracts import Contract, canonical, sha
from ..planning import QualityFailure
from ..structured_quality import ChapterMap
from .canonical_relations import CanonicalDecision
from .entity_contracts import (
    Assignment,
    MediaContribution,
    ObjectMatch,
    Role,
    Selection,
    TextContribution,
)


class PrimaryAssignment(Contract):
    target: str | None = None
    action: Literal["REUSE", "CREATE", "DISTRIBUTE", "SOURCE_ONLY", "REVIEW"]
    role: Role = "principles"
    sections: dict[str, Role] = Field(default_factory=dict)
    topic: str = ""
    topic_key: str = ''
    contribution_mode: Literal['CORE', 'EVIDENCE'] = 'CORE'
    text_contribution: TextContribution = 'AUTO'
    media_contribution: MediaContribution = 'AUTO'
    object_match: ObjectMatch | None = None
    outline_action: Literal['KEEP', 'REVISE'] | None = None
    outline_revision_reason: str = ''
    selections: list[Selection] = Field(default_factory=list)
    reason: str


class ExactDecision(CanonicalDecision):
    @model_validator(mode="before")
    @classmethod
    def unique_legacy_assignments(cls, value):
        if isinstance(value, dict) and isinstance(value.get("assignments"), list):
            value = dict(value)
            assignments = {}
            for raw in value["assignments"]:
                item = Assignment.model_validate(raw)
                if item.atomic in assignments:
                    raise ValueError("Every A requires one decision; use DISTRIBUTE selections for multiple objects")
                assignments[item.atomic] = item.model_dump(exclude={"atomic"})
            value["assignments"] = assignments
        return value


def contract(refs):
    tag = sha(canonical(refs))[:12]
    mapping = create_model("PrimaryAtomicKeys_" + tag, __base__=ChapterMap,
        **{ref: (PrimaryAssignment, ...) for ref in refs})
    return create_model("ExactRouting_" + tag, __base__=ExactDecision, assignments=(mapping, ...))


def canonical_decision(value):
    if isinstance(value.assignments, list):
        seen = [a.atomic for a in value.assignments]
        if len(set(seen)) != len(seen):
            raise QualityFailure("Every Atomic requires exactly one primary assignment")
        return value
    data = value.model_dump()
    data["assignments"] = [{"atomic": ref, **item} for ref, item in data["assignments"].items()]
    return CanonicalDecision.model_validate(data)
