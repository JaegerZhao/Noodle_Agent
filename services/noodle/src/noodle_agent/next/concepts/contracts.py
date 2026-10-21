from typing import Literal

from pydantic import Field

from ..contracts import Contract, Id, Scope, Text


class Identity(Contract):
    title: Text
    domain: Text
    identity_key: Id
    question: Text
    scope: Scope


class Decision(Contract):
    action: Literal[
        "NO_CHANGE",
        "ADD_EVIDENCE",
        "UPDATE_SECTION",
        "CREATE_SECTION",
        "LINK_ONLY",
        "CREATE_CONCEPT",
        "CONFLICT",
        "INSUFFICIENT",
    ]
    concept_id: Id | None = None
    section_id: Id | None = None
    section_title: str = ""
    section_question: str = ""
    proposal: Identity | None = None
    role: Literal["SUPPORT", "EXAMPLE", "CONTRAST", "CONFLICT"] = "SUPPORT"
    reason: Text


class Check(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    reason: str = ""


class SectionDraft(Contract):
    markdown: Text


class SectionCheck(Check):
    supported_fragment_ids: list[Id]
