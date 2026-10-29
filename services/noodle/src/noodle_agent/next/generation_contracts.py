"""Small semantic contracts; source coverage is checked by the application."""

from typing import Annotated, Literal

from pydantic import Field, create_model

from .contracts import Contract, Id, Scope, Text


class Unit(Contract):
    unit_id: Id
    block_id: Text
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: Text
    kind: Text
    asset_ref: str | None = None
    visual: dict | None = None


class Topic(Contract):
    key: Id
    title: Text
    question: Text
    scope: Scope
    units: list[Id] = Field(min_length=1)


class Exclusion(Contract):
    unit_id: Id
    category: Literal["NAVIGATION", "ADMINISTRATIVE", "DUPLICATE", "DECORATIVE"]
    reason: Text
    duplicate_of: Id | None = None


class WindowPlan(Contract):
    topics: list[Topic]
    exclusions: list[Exclusion] = Field(default_factory=list)


class Group(Contract):
    key: Id
    title: Text
    question: Text
    scope: Scope
    topic_keys: list[Id] = Field(min_length=1)


class MergePlan(Contract):
    summary: Text
    groups: list[Group] = Field(min_length=1)


class DraftSection(Contract):
    key: Id
    title: Text
    question: Text
    scope: Scope
    markdown: Text
    units: list[Id] = Field(min_length=1)


class Draft(Contract):
    title: Text
    summary: Text
    sections: list[DraftSection] = Field(min_length=1)


class Assessment(Contract):
    passed: bool
    issues: list[Text]
    supported_units: list[Id]
    reason: Text


class TopicHeader(Contract):
    key: Id
    title: Text
    question: Text
    scope: Scope


class Included(Contract):
    mode: Literal["TOPIC"]
    topic_key: Id


class Navigation(Contract):
    mode: Literal["NAVIGATION"]
    reason: Text


class Administrative(Contract):
    mode: Literal["ADMINISTRATIVE"]
    reason: Text


class Decorative(Contract):
    mode: Literal["DECORATIVE"]
    reason: Text


class Duplicated(Contract):
    mode: Literal["DUPLICATE"]
    reason: Text
    duplicate_of: Id


Decision = Annotated[
    Included | Navigation | Administrative | Decorative | Duplicated, Field(discriminator="mode")
]


class AllocationPlan(Contract):
    topics: list[TopicHeader]
    assignments: dict[Id, Decision]


def allocation_contract(units: list[Unit], *, compact=False):
    """Every input unit is a required JSON property with one legal decision."""
    if compact:
        return AllocationPlan
    fields = {}
    for unit in units:
        routes = (
            Included | Navigation | Administrative | Duplicated | Decorative
            if (unit.visual or {}).get("is_decorative")
            else Included | Navigation | Administrative | Duplicated
        )
        fields[unit.unit_id] = (Annotated[routes, Field(discriminator="mode")], ...)
    assignments = create_model("RequiredUnitAssignments", __base__=Contract, **fields)
    return create_model(
        "WindowAllocation",
        __base__=Contract,
        topics=(list[TopicHeader], ...),
        assignments=(assignments, ...),
    )


def allocated_window(value) -> WindowPlan:
    assignments = (
        value.assignments.model_dump()
        if isinstance(value.assignments, Contract)
        else value.model_dump()["assignments"]
    )
    topics = {topic.key: topic for topic in value.topics}
    if len(topics) != len(value.topics):
        raise ValueError("Duplicate topic identity")
    by_topic = {key: [] for key in topics}
    exclusions = []
    for unit_id, decision in assignments.items():
        mode = decision["mode"]
        if mode == "TOPIC":
            if decision["topic_key"] not in topics:
                raise ValueError(f"Unknown topic {decision['topic_key']} assigned to {unit_id}")
            by_topic[decision["topic_key"]].append(unit_id)
        else:
            exclusions.append(
                Exclusion(
                    unit_id=unit_id,
                    category=mode,
                    reason=decision["reason"],
                    duplicate_of=decision.get("duplicate_of"),
                )
            )
    empty = [key for key, items in by_topic.items() if not items]
    if empty:
        raise ValueError(f"Topic has no assigned source units: {empty}")
    return WindowPlan(
        topics=[
            Topic(**header.model_dump(), units=by_topic[header.key]) for header in value.topics
        ],
        exclusions=exclusions,
    )


class WrittenSection(Contract):
    key: Id
    title: Text
    question: Text
    scope: Scope
    markdown: Text


class WritingPlan(Contract):
    title: Text
    summary: Text
    sections: list[WrittenSection] = Field(min_length=1)
    assignments: dict[Id, Id]


def writing_contract(units, *, compact=False):
    if compact:
        return WritingPlan
    assignments = create_model(
        "RequiredEvidenceAssignments",
        __base__=Contract,
        **{unit.unit_id: (Id, ...) for unit in units},
    )
    return create_model(
        "WrittenAtomicNote",
        __base__=Contract,
        title=(Text, ...),
        summary=(Text, ...),
        sections=(list[WrittenSection], ...),
        assignments=(assignments, ...),
    )


def allocated_draft(value) -> Draft:
    assignments = (
        value.assignments.model_dump()
        if isinstance(value.assignments, Contract)
        else value.model_dump()["assignments"]
    )
    by_section = {section.key: [] for section in value.sections}
    if len(by_section) != len(value.sections):
        raise ValueError("Duplicate written section key")
    for unit_id, key in assignments.items():
        if key not in by_section:
            raise ValueError(f"Unknown section {key} assigned to {unit_id}")
        by_section[key].append(unit_id)
    empty = [key for key, items in by_section.items() if not items]
    if empty:
        raise ValueError(f"Written section has no supporting unit: {empty}")
    return Draft(
        title=value.title,
        summary=value.summary,
        sections=[
            DraftSection(**section.model_dump(), units=by_section[section.key])
            for section in value.sections
        ],
    )


def canonical_writing_response(raw):
    """Normalize redundant evidence only when it agrees with required assignments.

    Empty nested assignments carry no evidence. Nonempty nested mappings and units
    must exactly match the required top-level mapping; conflicts are never dropped.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("assignments"), dict):
        return raw
    sections = raw.get("sections")
    if not isinstance(sections, list):
        return raw
    normalized = []
    for section in sections:
        if not isinstance(section, dict):
            normalized.append(section)
            continue
        expected = {
            uid: key for uid, key in raw["assignments"].items() if key == section.get("key")
        }
        if "units" in section:
            ids = section["units"]
            if (
                not isinstance(ids, list)
                or any(not isinstance(uid, str) for uid in ids)
                or len(ids) != len(set(ids))
                or set(ids) != set(expected)
            ):
                raise ValueError("Redundant section units conflict with required assignments")
        if "assignments" in section and section["assignments"] not in ({}, expected):
            raise ValueError("Nested section assignments conflict with required assignments")
        normalized.append(
            {key: value for key, value in section.items() if key not in {"units", "assignments"}}
        )
    return {**raw, "sections": normalized}


class SectionPatch(Contract):
    key: Id
    markdown: Text


class NoteRepair(Contract):
    sections: list[SectionPatch] = Field(min_length=1)
    summary: str | None = None
