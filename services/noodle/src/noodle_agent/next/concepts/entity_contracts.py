"""Compact entity-level routing contracts; no generated field is a fact by itself."""

from typing import Literal

from pydantic import Field, PrivateAttr, model_validator

from ..contracts import Contract, Id, Scope, Text
from .contracts import Identity

POLICY = "entity-concepts-v2"
ROLE_TITLES = {
    "overview": "概述",
    "principles": "原理",
    "formulas": "公式与条件",
    "procedure": "方法步骤",
    "examples": "实例与实现",
    "comparison": "方法联系与比较",
    "limitations": "适用条件与局限",
}
Role = Literal[
    "overview", "principles", "formulas", "procedure", "examples", "comparison", "limitations"
]
ObjectMatch = Literal['SAME_OBJECT', 'INTERNAL_VARIANT', 'USES', 'DIFFERENT_OBJECT', 'UNCERTAIN']
Purpose = Literal['CORE_DELTA', 'SUPPORT_ONLY', 'FURTHER_READING', 'CONTEXT_ONLY', 'CONFLICT']
TextContribution = Literal['AUTO', 'CORE_DELTA', 'SUPPORT_ONLY', 'FURTHER_READING', 'CONTEXT_ONLY', 'CONFLICT']
MediaContribution = Literal['AUTO', 'NONE', 'KEY_FIGURE', 'BETTER_FIGURE']


class Selection(Contract):
    """Sparse exceptions to the primary assignment; section refs are packet-local."""
    target: str
    sections: list[str] = Field(default_factory=list)
    fragments: list[str] = Field(default_factory=list)
    purpose: Purpose
    reason: Text

    @model_validator(mode='after')
    def located(self):
        if not self.sections and not self.fragments:
            raise ValueError('A contribution selection needs sections or fragments')
        return self


class SelectedContribution(Contract):
    entity_key: Id
    section_ids: list[str] = Field(default_factory=list)
    fragment_ids: list[str] = Field(default_factory=list)
    purpose: Purpose
    reason: Text


class Entity(Identity):
    description: Text
    aliases: list[Text] = Field(default_factory=list)
    reuse_id: Id | None = None
    storage_identity_key: Id | None = None


class Route(Contract):
    entity_key: Id | None = None
    sections: dict[str, Role] = Field(default_factory=dict)
    topic: str = ""
    topic_key: str = ''
    contribution_mode: Literal['CORE', 'EVIDENCE'] = 'CORE'
    text_contribution: TextContribution = 'AUTO'
    media_contribution: MediaContribution = 'AUTO'
    object_match: ObjectMatch | None = None
    outline_action: Literal['KEEP', 'REVISE'] | None = None
    outline_revision_reason: str = ''
    selected_contributions: list[SelectedContribution] = Field(default_factory=list)
    binding: dict = Field(default_factory=dict)
    reason: Text


class Relation(Contract):
    source_key: Id
    target_key: Id
    navigation_role: Literal["TOPIC_CHILD", "TOPIC_PARENT"] | None = None
    relation: Literal["LINKS_TO", "RELATED_TO", "COMPARES_WITH", 'USES', 'VARIANT_OF'] = "LINKS_TO"
    semantic_direction: Literal['OUTGOING', 'INCOMING'] | None = None
    support_atomic_ids: list[Id] = Field(min_length=1)
    target_support_atomic_ids: list[Id] = Field(default_factory=list)
    reason: Text
    position: int = Field(default=0, ge=0)


class EntityPlan(Contract):
    entities: list[Entity]
    routes: dict[str, Route]
    relations: list[Relation] = Field(default_factory=list)
    identity_updates: list[dict] = Field(default_factory=list)
    _unresolved_relations: list[dict] = PrivateAttr(default_factory=list)
    _unresolved_atomics: dict[str, str] = PrivateAttr(default_factory=dict)
    _protocol_issues: list[dict] = PrivateAttr(default_factory=list)
    _normalizations: list[dict] = PrivateAttr(default_factory=list)

    @property
    def unresolved_relations(self):
        return self._unresolved_relations


class NewEntity(Contract):
    ref: str = Field(pattern=r"^N\d+$")
    title: Text
    domain: Text
    identity_key: Id
    description: Text
    aliases: list[Text] = Field(default_factory=list)
    scope_includes: list[Text] = Field(default_factory=list)


class Assignment(Contract):
    atomic: str
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
    reason: Text

    @model_validator(mode="after")
    def explicit_target(self):
        if self.action in {"CREATE", "REUSE"} and not self.target:
            raise ValueError("CREATE/REUSE requires target: a declared N reference or input C reference")
        if self.action == 'DISTRIBUTE' and (self.target or not self.selections):
            raise ValueError('DISTRIBUTE has no primary target and needs selected object contributions')
        return self


class IdentityUpdate(Contract):
    ref: str
    title: Text
    description: Text
    aliases: list[Text] = Field(default_factory=list)
    scope: Scope
    reason: Text


class RoutingDecision(Contract):
    assignments: list[Assignment]
    new_entities: list[NewEntity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    identity_updates: list[IdentityUpdate] = Field(default_factory=list)


class ImageChoice(Contract):
    action: Literal["USE", "OMIT"]
    reason: Text
    caption: str | None = None
    after_heading: str | None = None


class Chapter(Contract):
    markdown: Text
    images: dict[str, ImageChoice] = Field(default_factory=dict)


class ChapterCheck(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    supported_fragment_ids: list[Id]
    checked_image_ids: list[str] = Field(default_factory=list)


class EntityPatch(Contract):
    chapters: dict[str, Chapter]


class EntityPatchCheck(Contract):
    chapters: dict[str, ChapterCheck]


class EntityPlanCheck(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    checked_atomic_ids: list[Id]
    review_mode: str = "INDEPENDENT_LLM"
