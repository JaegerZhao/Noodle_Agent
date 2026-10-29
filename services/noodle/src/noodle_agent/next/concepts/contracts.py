from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..contracts import Contract, Id, Scope, Text


class Identity(Contract):
    title: Text
    domain: Text
    identity_key: Id
    question: Text
    scope: Scope

    @field_validator("scope")
    @classmethod
    def automatic_identity_scope(cls, value):
        # Automatic identities can start with partial coverage. Absence in the
        # first fragment must not permanently forbid later chapters. Object,
        # domain and question still constrain routing; factual conditions stay
        # in the source-supported sections. Manual chapter protection is separate.
        return value.model_copy(update={"excludes": []})


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

    @model_validator(mode="before")
    @classmethod
    def canonical_section_proposal(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("proposal"), dict):
            return value
        result, proposal = dict(value), dict(value["proposal"])
        for key in ("section_title", "section_question", "role"):
            if key in proposal:
                nested = proposal.pop(key)
                if result.get(key) not in (None, "", nested):
                    raise ValueError("Conflicting Concept section proposal: " + key)
                result[key] = nested
        result["proposal"] = proposal
        return result


class Check(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    reason: str = ""


class SectionDraft(Contract):
    markdown: Text


class SectionCheck(Check):
    supported_fragment_ids: list[Id]
