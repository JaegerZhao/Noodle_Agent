"""Validated build contracts. Publication approval is supplied by the application."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")]
Text = Annotated[str, Field(min_length=1)]


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(Contract):
    includes: list[Text] = Field(min_length=1)
    excludes: list[Text] = Field(default_factory=list)
    prerequisites: list[Text] = Field(default_factory=list)


class Input(Contract):
    atomic_revision_id: Id
    section_ids: list[Id] = Field(min_length=1)
    evidence_ids: list[Id] = Field(min_length=1)
    role: Literal["SUPPORT", "EXAMPLE", "CONTRAST", "CONFLICT"] = "SUPPORT"
    action: Literal["ADD_EVIDENCE", "UPDATE_SECTION", "CREATE_SECTION", "CREATE_CONCEPT"]


class Section(Contract):
    section_id: Id
    title: Text
    question: Text
    scope: Scope
    markdown: Text
    evidence_ids: list[Id] = Field(default_factory=list)
    inputs: list[Input] = Field(default_factory=list)
    protected: bool = False

    @model_validator(mode="after")
    def unique_references(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Duplicate evidence ID")
        return self


class MapEntry(Contract):
    entry_id: Id
    parent_entry_id: Id | None = None
    title: Text
    target_note_id: Id | None = None
    target_revision_id: Id | None = None
    description: str = ""


class Link(Contract):
    from_section_id: Id | None = None
    target_note_id: Id
    target_section_id: Id | None = None
    relation: Literal["LINKS_TO", "PREREQUISITE_OF", "RELATED_TO", "COMPARES_WITH", "CONTRADICTS"]
    status: Literal["CONFIRMED", "SUGGESTED"]
    basis: Text


class Candidate(Contract):
    contract_version: Literal["next-v1"] = "next-v1"
    note_id: Id
    title: Text
    summary: str = ""
    scope: Scope
    sections: list[Section] = Field(min_length=1)
    map_entries: list[MapEntry] = Field(default_factory=list)
    map_policy: Literal["MANUAL", "AUTO", "SOURCE"] = "MANUAL"
    links: list[Link] = Field(default_factory=list)
    policy_version: Text = "writing-next-v1"

    @model_validator(mode="after")
    def unique_ids(self):
        for values in (
            [s.section_id for s in self.sections],
            [m.entry_id for m in self.map_entries],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate section or map entry ID")
        return self

    @property
    def fingerprint(self) -> str:
        return sha(canonical(self.model_dump()))


class Review(Contract):
    candidate_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    policy_version: Text
    reviewer: Text
    passed: bool
    report: dict = Field(default_factory=dict)
