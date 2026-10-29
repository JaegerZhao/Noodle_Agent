"""Small contracts for source-aligned plans and assembly review."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, EntityHints, Id, Text

POLICY = "source-sections-v1"


class TeachingSupport(Contract):
    unit_id: Id
    quote: Text
    role: Literal["CONTENT", "DEFINITION", "CONDITION", "PROCEDURE", "EXAMPLE", "RELATION", "CODE", "LIMITATION", "FORMULA"]


class Boundary(Contract):
    start_unit: Id
    title: Text
    parent_title: str = ""
    topic_key: str = ""
    outline_node_id: str | None = None
    chapter_key: str | None = None
    reason: str = ""
    disposition: Literal["CONTENT", "EXCLUDE"] = "CONTENT"
    exclusion_category: Literal["ADMINISTRATIVE", "NAVIGATION"] = "ADMINISTRATIVE"
    teaching_support: list[TeachingSupport] = Field(default_factory=list)
    support_state: Literal["TEXT", "PENDING_VISION"] = "TEXT"


class Boundaries(Contract):
    sections: list[Boundary] = Field(min_length=1)


class PlanningExclusionCheck(Contract):
    passed: bool
    checked_starts: list[Id]
    checked_content_starts: list[Id] = Field(default_factory=list)
    issues: list[Text] = Field(default_factory=list)


class WrittenSection(Contract):
    key: Id
    title: Text
    markdown: Text


class SourceDraft(Contract):
    title: Text
    summary: str = Field(default="", description="知识内容简介，不是修复摘要或编辑日志")
    entity_hints: EntityHints | None = None
    sections: list[WrittenSection]
    assignments: dict[Id, Id]
    omissions: dict[Id, Text] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def canonical_assignments(cls, value):
        if not isinstance(value, dict):
            return value
        result = dict(value)
        sections = value.get("sections", [])
        mapping = value.get("assignments", {})
        if not isinstance(mapping, dict):
            return value
        section_keys = {s.get("key") for s in sections if isinstance(s, dict)}
        if mapping and set(mapping) <= section_keys:
            inverted = {}
            for section, units in mapping.items():
                units = units if isinstance(units, list) else [units]
                for unit in units:
                    if not isinstance(unit, str) or unit in inverted:
                        raise ValueError("Ambiguous reversed assignment mapping")
                    inverted[unit] = section
            mapping = inverted
        nested = {}
        clean = []
        for section in sections:
            section = dict(section)
            assignments = section.pop("assignments", {})
            if not isinstance(assignments, dict):
                raise ValueError("Invalid nested source assignments")  # noqa: TRY004 -- Pydantic validation error
            for unit, target in assignments.items():
                if target not in section_keys or unit in nested and nested[unit] != target:
                    raise ValueError("Conflicting nested assignments")
                nested[unit] = target
            for field in ("unit_ids", "units"):
                declared = section.pop(field, None)
                if declared is None:
                    continue
                if not isinstance(declared, list) or any(not isinstance(u, str) for u in declared) or len(declared) != len(set(declared)):
                    raise ValueError("Section unit aliases must contain distinct real unit IDs")
                for unit in declared:
                    target = section.get("key")
                    if unit in nested and nested[unit] != target:
                        raise ValueError("Section unit alias conflicts with another source assignment")
                    if mapping and mapping.get(unit) != target:
                        raise ValueError("Section unit alias disagrees with the top-level source assignment")
                    nested[unit] = target
            asset = section.pop("asset_ref", None)
            if asset is not None:
                if not isinstance(asset, str) or not isinstance(section.get("markdown"), str):
                    raise ValueError("Image metadata needs a resource path and Markdown body")
                if asset and f"]({asset})" not in section["markdown"]:
                    # Compile an explicit model-selected image, never choose a new
                    # resource. The usual approved-asset check still validates it.
                    section["markdown"] += "\n\n![教学插图](" + asset + ")"
            clean.append(section)
        if mapping and any(mapping.get(u) != target for u, target in nested.items()):
            raise ValueError("Nested source assignments disagree with the top-level mapping")
        result["assignments"] = mapping or nested
        result["sections"] = clean
        return result


def bounded_source_draft(unit_ids, *, ranges=False, source_order=None, navigation_ids=()):
    """Describe the existing source whitelist to the writer before it generates."""
    class BoundedSourceDraft(SourceDraft):
        @model_validator(mode='before')
        @classmethod
        def expand_ranges(cls, value):
            if not isinstance(value, dict):
                return value
            result = dict(value)
            nested, sections = [], []
            for section in result.get('sections', []):
                section = dict(section)
                local = section.pop('source_ranges', None)
                if local is not None:
                    if not isinstance(local, list):
                        raise ValueError('Section source_ranges must be a list')
                    for item in local:
                        if not isinstance(item, dict):
                            raise ValueError('Each nested source range must be an object')  # noqa: TRY004
                        item = dict(item)
                        if item.get('section_key') and item['section_key'] != section.get('key'):
                            raise ValueError('Nested range names a different section')
                        if not item.get('omission_reason'):
                            item['section_key'], item['omission_reason'] = section.get('key'), ''
                        nested.append(item)
                sections.append(section)
            result['sections'] = sections
            if 'source_ranges' not in result and not nested:
                return result
            declared = result.pop('source_ranges', [])
            if not isinstance(declared, list):
                raise ValueError('source_ranges must be a list')  # noqa: TRY004 -- Pydantic validation error
            order = list(source_order or unit_ids)
            positions = {u:i for i,u in enumerate(order)}
            selected, navigation = set(unit_ids), set(navigation_ids)
            def expand(items):
                accounting = {}
                for item in items:
                    if not isinstance(item,dict):
                        raise ValueError('Each source range must be an object')  # noqa: TRY004
                    start, end = positions.get(item.get('start_unit')), positions.get(item.get('end_unit'))
                    section, reason = item.get('section_key',''), item.get('omission_reason','')
                    if start is None or end is None or end < start or bool(section)==bool(reason):
                        raise ValueError('Source range needs real ordered endpoints and exactly one section or omission reason')
                    span=order[start:end+1]
                    if set(span)-selected-navigation:
                        raise ValueError('Source range crosses substantive units outside this Atomic')
                    for unit in (u for u in span if u in selected):
                        if unit in accounting:
                            raise ValueError('Overlapping source ranges')
                        accounting[unit] = (section or '', reason or '')
                return accounting
            accounting = expand(declared)
            for unit, target in expand(nested).items():
                if unit in accounting and accounting[unit] != target:
                    raise ValueError('Nested and top-level source ranges disagree')
                accounting[unit] = target
            if set(accounting) != set(unit_ids):
                raise ValueError('Source ranges must cover every selected unit, without gaps')
            assignments = {u:section for u,(section,_) in accounting.items() if section}
            omissions = {u:reason for u,(_,reason) in accounting.items() if reason}
            if result.get('assignments') and result['assignments'] != assignments or result.get('omissions') and result['omissions'] != omissions:
                raise ValueError('Range and legacy source accounting disagree')
            return {**result,'assignments':assignments,'omissions':omissions}

        @classmethod
        def model_json_schema(cls, **kwargs):
            schema = super().model_json_schema(**kwargs)
            if ranges:
                for field in ('assignments','omissions'):
                    schema['properties'].pop(field,None)
                schema['required'] = [k for k in schema['required'] if k != 'assignments'] + ['source_ranges']
                schema['properties']['source_ranges'] = {'type':'array','description':'按输入units顺序，无损覆盖每个单元。起止均包含；非连续证据用多个范围。section_key与omission_reason恰好一个非空。',
                    'items':{'type':'object','additionalProperties':False,'properties':{k:{'type':'string'} for k in ('start_unit','end_unit','section_key','omission_reason')},
                        'required':['start_unit','end_unit','section_key','omission_reason']}}
                return schema
            for field in ('assignments', 'omissions'):
                schema['properties'][field].update(propertyNames={'enum': list(unit_ids)}, maxProperties=len(unit_ids))
            return schema
    return BoundedSourceDraft


class Finding(Contract):
    plan_key: Id
    section_key: str = ""
    issue: Text
    source_units: list[Id] = Field(default_factory=list)


class AssemblyCheck(Contract):
    passed: bool
    checked_plans: list[Id]
    checked_omissions: list[Id]
    issues: list[Finding] = Field(default_factory=list)


class SectionPatch(Contract):
    key: Id
    title: str = ""
    markdown: Text


class DraftPatch(Contract):
    unchanged_reason: str = Field(default="", description="仅在对照来源确认指控不成立、无需改正确正文时说明原因")
    summary: str | None = Field(default=None, description="如需修正简介，概括新正文内容；不写修复了什么")
    entity_hints: EntityHints | None = None
    sections: list[SectionPatch]
    # An omitted unit may be restored into a section; unchanged assignments stay frozen.
    restored_assignments: dict[Id, Id] = Field(default_factory=dict)
    omitted_assignments: dict[Id, Text] = Field(default_factory=dict)
