"""Essential planning review: source references, repairable gaps, one local repair."""

from typing import Literal

from pydantic import Field, model_validator

from .atomic_planning import PLANNING_POLICY, coalesce_topics
from .contracts import Contract, Id, canonical
from .learning_plan import source_cards
from .planning import QualityFailure
from .section_contracts import Boundaries, Boundary, TeachingSupport


class Topic(Contract):
    start_unit: Id
    title: str
    topic_key: str = ""
    reason: str = ""
    parent_title: str = ""
    disposition: Literal["CONTENT", "EXCLUDE", "UNRESOLVED"] = "CONTENT"
    exclusion_category: Literal["ADMINISTRATIVE", "NAVIGATION"] = "ADMINISTRATIVE"
    teaching_units: list[Id] = Field(default_factory=list)
    support_state: Literal["TEXT", "PENDING_VISION"] = "TEXT"

    @model_validator(mode="before")
    @classmethod
    def legacy_witnesses(cls, value):
        # Old wire responses can be replayed without asking a model to copy LaTeX.
        # The original response remains immutable; evidence text comes from source.
        if isinstance(value, dict) and "teaching_support" in value:
            value = dict(value)
            old = value.pop("teaching_support")
            value.setdefault("teaching_units", [p["unit_id"] for p in old])
        return value


class Plan(Contract):
    sections: list[Topic] = Field(min_length=1)


class Finding(Contract):
    category: Literal["BLOCKING", "ADVISORY"]
    range_starts: list[Id] = Field(min_length=1)
    issue: str


class Check(Contract):
    findings: list[Finding] = Field(default_factory=list)


class Edit(Contract):
    replace_starts: list[Id] = Field(min_length=1)
    sections: list[Topic] = Field(min_length=1)


class Patch(Contract):
    edits: list[Edit] = Field(min_length=1)


def ranges(plan, units):
    positions = {u.unit_id: i for i, u in enumerate(units)}
    starts = [s.start_unit for s in plan.sections]
    if len(starts) != len(set(starts)) or any(s not in positions for s in starts):
        raise QualityFailure("Cannot locate duplicate or unknown planning boundaries")
    order = [positions[s] for s in starts]
    if order != sorted(order) or order[0] != 0:
        raise QualityFailure("Planning ranges must cover the actual source in order")
    for i, section in enumerate(plan.sections):
        end = order[i + 1] if i + 1 < len(order) else len(units)
        yield section, units[order[i]:end]


def prepare(plan, units):
    positions = {u.unit_id: i for i, u in enumerate(units)}
    if any(s.start_unit not in positions for s in plan.sections):
        raise QualityFailure("Cannot safely locate unknown boundary; no source invented")
    sections = list(plan.sections)
    if positions[sections[0].start_unit] > 0:
        sections.insert(0, Topic(start_unit=units[0].unit_id, title="待确认来源范围",
            disposition="UNRESOLVED", reason="Candidate omitted this source prefix; classify explicitly"))
    prepared = Plan(sections=sections)
    list(ranges(prepared, units))
    return prepared


def problems(plan, units):
    result = []
    for section, selected in ranges(plan, units):
        codes = []
        by_id = {u.unit_id: u for u in selected}
        if not section.title.strip():
            codes.append("MISSING_TITLE")
        if section.disposition == "UNRESOLVED":
            codes.append("UNCOVERED_SOURCE_RANGE")
        elif section.disposition == "EXCLUDE":
            if not section.reason.strip():
                codes.append("MISSING_EXCLUSION_REASON")
            if any(u.asset_ref and not (u.visual or {}).get("is_decorative") for u in selected):
                codes.append("UNVERIFIED_IMAGE_EXCLUSION")
        else:
            valid = []
            for ref in section.teaching_units:
                unit = by_id.get(ref)
                if unit is None:
                    codes.append("OUT_OF_RANGE_SUPPORT:" + ref)
                elif unit.asset_ref and unit.text.startswith("教学图片："):
                    codes.append("IMAGE_LOCATOR_IS_NOT_TEXT_SUPPORT:" + ref)
                else:
                    valid.append(ref)
            if not valid and not any(u.asset_ref for u in selected):
                codes.append("MISSING_SOURCE_SUPPORT")
        result.extend({"range_start": section.start_unit, "code": code} for code in codes)
    return result


def patch_plan(plan, patch, units, required):
    starts = [s.start_unit for s in plan.sections]
    intervals = {s.start_unit: selected for s, selected in ranges(plan, units)}
    edits, touched = {}, set()
    for edit in patch.edits:
        if any(s not in starts for s in edit.replace_starts):
            raise QualityFailure("Patch must locate original ranges")
        indexes = [starts.index(s) for s in edit.replace_starts]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))) or touched.intersection(edit.replace_starts):
            raise QualityFailure("Patch ranges must be contiguous and nonoverlapping")
        selected = [u for s in edit.replace_starts for u in intervals[s]]
        replacement = Plan(sections=edit.sections)
        list(ranges(replacement, selected))
        edits[indexes[0]] = (indexes[-1], edit.sections)
        touched.update(edit.replace_starts)
    if touched != required:
        raise QualityFailure("Patch must repair blocking ranges only and address every blocker")
    sections, index = [], 0
    while index < len(starts):
        if index in edits:
            end, replacement = edits[index]
            sections.extend(replacement)
            index = end + 1
        else:
            sections.append(plan.sections[index])
            index += 1
    result = Plan(sections=sections)
    remaining = problems(result, units)
    if remaining:
        raise QualityFailure("Planning repair failed source integrity: " + canonical(remaining))
    return result


def bind(plan, units):
    issues = problems(plan, units)
    if issues:
        raise QualityFailure("Unresolved essential planning issues: " + canonical(issues))
    sections = []
    for section, selected in ranges(plan, units):
        by_id = {u.unit_id: u for u in selected}
        refs = list(dict.fromkeys(section.teaching_units)) if section.disposition == "CONTENT" else []
        proofs = [TeachingSupport(unit_id=r, quote=by_id[r].text, role="CONTENT") for r in refs]
        pending = section.disposition == "CONTENT" and not proofs and any(u.asset_ref for u in selected)
        sections.append(Boundary(start_unit=section.start_unit, title=section.title,
            parent_title=section.parent_title, topic_key=section.topic_key, reason=section.reason,
            disposition=section.disposition, exclusion_category=section.exclusion_category,
            teaching_support=proofs, support_state="PENDING_VISION" if pending else "TEXT"))
    return coalesce_topics(Boundaries(sections=sections), units)


def plan_window(generation, units):
    stage = "source.learning.v8." + units[0].unit_id
    pages = source_cards(units)
    candidate = generation.call(stage + ".plan", "next.structure",
        "规划连续、足够综合、可独立学习的Atomic主题，不写正文。不按页拆分；同一方法的定义、条件、公式、训练、例题、代码、局限优先放在一篇。"
        "不同算法不强行合并，不预设篇数。每个来源单元必须在某个范围，排除也须显式给disposition=EXCLUDE及真实start_unit、reason；不能简单省略封面/导航。"
        "第一项从first_unit开始。纯封面、版权、课程标识、无教学内容的行政导论/导航应单独EXCLUDE；混合页沿真实单元分开。"
        "CONTENT只在teaching_units选1到3个代表性真实unit_id，程序读取完整原文；不复制引文和公式，不输出teaching_support。kind只是版式，HEADING也可含有效定义。"
        "IMAGE_PENDING是真实未知图片，不能据此猜图意或删除；纯图范围保留CONTENT，teaching_units可空，等待后续必要视觉。"
        "输出综合title、同主题topic_key和简短reason，不为角色/例题/代码另起篇。units格式为[id,kind,text]，结束边界由程序推导。",
        {"first_unit": units[0].unit_id, "pages": pages, "policy": PLANNING_POLICY}, Plan,
        use_pro_model=True, max_attempts=1)
    prepared = prepare(candidate, units)
    hard = problems(prepared, units)
    starts = {s.start_unit for s in prepared.sections}

    def located(value):
        if any(not set(f.range_starts) <= starts for f in value.findings):
            raise QualityFailure("Planning finding must locate actual candidate ranges")

    review = generation.call(stage + ".review", "next.structure_exclusion_review",
        "仅做一次必要的规划语义核对：不能误删教学内容、把纯封面/导航包装成知识，或把同一学习主题按例题/代码切碎。"
        "允许合理单页独立问题、标题差异和多根主题，不要求百科全书式补充。措辞、命名、风格或可选扩展仅ADVISORY，不阻断。"
        "teaching_units的真实正文在pages中，kind是版式提示；不因HEADING拒绝完整定义。UNRESOLVED必须明确判断保留还是排除，未知图片保留后续视觉。"
        "只输出findings；BLOCKING给必须一起修改的全部range_starts和具体实质错误，合并要列双方起点。不重复输出已检查ID清单。",
        {"pages": pages, "ranges": prepared.model_dump()["sections"], "program_issues": hard}, Check,
        located, use_pro_model=True, max_attempts=1)
    required = {i["range_start"] for i in hard}
    required.update(s for f in review.findings if f.category == "BLOCKING" for s in f.range_starts)
    final = prepared
    if required:
        affected = [(s, selected) for s, selected in ranges(prepared, units) if s.start_unit in required]
        patch = generation.call(stage + ".patch", "next.structure_patch",
            "只修一次已定位问题。edits的replace_starts列连续被替换原范围，sections必须覆盖同一实际来源区间；不能改其他范围。"
            "必须修复全部required_starts和program_issues。UNRESOLVED来源缺口须输出明确CONTENT/EXCLUDE；范围首项从原区间首单元开始，可在真实单元处分开行政与教学。"
            "CONTENT用真实teaching_units引用原文，不复制公式引文，无字数限制检查；未知图保留待后续视觉。排除也输出范围与理由。"
            "修正后仅程序验收，不再模型复核，请自行确认语义；不要重写未涉及主题。",
            {"required_starts": sorted(required), "program_issues": hard,
             "findings": [f.model_dump() for f in review.findings if f.category == "BLOCKING"],
             "ranges": [s.model_dump() for s, _ in affected], "pages": source_cards([u for _, selected in affected for u in selected])},
            Patch, use_pro_model=True, max_attempts=1)
        final = patch_plan(prepared, patch, units, required)
    bound = bind(final, units)
    receipt = {"policy": "learning-topics-v8", "original": candidate.model_dump(), "prepared": prepared.model_dump(),
        "review": review.model_dump(), "program_issues": hard, "patched": bool(required),
        "final": bound.model_dump(), "post_patch_semantic_review": False,
        "evidence_binding": "Program reads exact native units; model does not copy formulas",
        "pending_vision_starts": [s.start_unit for s in bound.sections if s.support_state == "PENDING_VISION"]}
    generation.journal.save(stage + ".acceptance", {"policy": "learning-topics-v8", "first_unit": units[0].unit_id}, receipt)
    return bound
