"""One semantic plan review, one local patch, then deterministic acceptance only."""

from collections import defaultdict
from typing import Annotated, Literal

from pydantic import Field

from .atomic_planning import PLANNING_POLICY, coalesce_topics
from .contracts import Contract, Id, canonical
from .planning import QualityFailure
from .section_contracts import Boundaries, Boundary, TeachingSupport
from .segmentation import tokens

POLICY = "learning-topics-v7"


class Witness(TeachingSupport):
    quote: Annotated[str, Field(min_length=1, max_length=180)]


class Topic(Boundary):
    title: Annotated[str, Field(min_length=1, max_length=180)]
    topic_key: Annotated[str, Field(max_length=100)] = ""
    reason: Annotated[str, Field(min_length=1, max_length=320)]
    teaching_support: list[Witness] = Field(default_factory=list, max_length=3)


class TopicPlan(Contract):
    sections: list[Topic] = Field(min_length=1)


class Finding(Contract):
    category: Literal["BLOCKING", "ADVISORY"]
    range_starts: list[Id] = Field(min_length=1)
    issue: Annotated[str, Field(min_length=1, max_length=600)]


class Review(Contract):
    checked_starts: list[Id]
    findings: list[Finding] = Field(default_factory=list)


class Edit(Contract):
    replace_starts: list[Id] = Field(min_length=1)
    sections: list[Topic] = Field(min_length=1)


class Patch(Contract):
    edits: list[Edit] = Field(min_length=1)


def source_cards(units):
    """Send each native unit once; layout kinds are hints, not semantic verdicts."""
    pages = defaultdict(list)
    for unit in units:
        placeholder = unit.asset_ref and unit.text.startswith("教学图片：")
        pages[unit.page].append([unit.unit_id, "IMAGE_PENDING" if unit.asset_ref else unit.kind,
                                 "" if placeholder else unit.text])
    return [{"page": page, "units": items} for page, items in pages.items()]


def windows(units, maximum=16000):
    current, cost = [], 0
    by_page = defaultdict(list)
    for unit in units:
        by_page[unit.page].append(unit)
    for card in source_cards(units):
        size = tokens(canonical(card))
        if current and (cost + size > maximum or len(current) >= 64):
            yield [u for p in current for u in by_page[p]]
            current, cost = [], 0
        current.append(card["page"])
        cost += size
    if current:
        yield [u for p in current for u in by_page[p]]


def ranges(value, units):
    from .source_sections import validate_boundaries

    validate_boundaries(value, units)
    positions = {u.unit_id: i for i, u in enumerate(units)}
    for index, section in enumerate(value.sections):
        end = positions[value.sections[index + 1].start_unit] if index + 1 < len(value.sections) else len(units)
        yield section, units[positions[section.start_unit]:end]


def program_issues(value, units):
    """Check facts about the contract; pure titles are judged in the one review."""
    issues = []
    for section, selected in ranges(value, units):
        def issue(code, detail, unit_id=None, *, range_start=section.start_unit):
            issues.append({"range_start": range_start, "code": code, "detail": detail,
                           **({"unit_id": unit_id} if unit_id else {})})

        by_id = {u.unit_id: u for u in selected}
        if section.disposition == "EXCLUDE":
            if not section.reason.strip():
                issue("MISSING_EXCLUSION_REASON", "Excluded range needs a concrete reason")
            if section.teaching_support:
                issue("EXCLUDED_SUPPORT", "Excluded range cannot claim teaching witnesses")
            if any(u.asset_ref and not (u.visual or {}).get("is_decorative") for u in selected):
                issue("UNVERIFIED_IMAGE_EXCLUSION", "Keep unverified images for downstream grounded vision")
            continue
        if not section.reason.strip() or not section.topic_key.strip():
            issue("MISSING_TOPIC_REASON", "Content needs an explicit learning topic and boundary reason")
        if section.support_state == "PENDING_VISION":
            if section.teaching_support or not any(u.asset_ref for u in selected):
                issue("INVALID_PENDING_VISION", "Pending vision requires an actual image and no claimed text witnesses")
        elif not section.teaching_support:
            issue("MISSING_WITNESS", "Content needs at least one actual teaching witness")
        for proof in section.teaching_support:
            unit = by_id.get(proof.unit_id)
            if unit is None:
                issue("OUT_OF_RANGE_WITNESS", "Witness must belong to this range", proof.unit_id)
            elif proof.quote not in unit.text:
                issue("INEXACT_WITNESS", "Quote must be an exact source substring", proof.unit_id)
            elif unit.asset_ref and unit.text.startswith("教学图片："):
                issue("IMAGE_PLACEHOLDER_WITNESS", "Asset locator is not image understanding", proof.unit_id)
    return issues


def apply_patch(value, patch, units, required_starts):
    starts = [s.start_unit for s in value.sections]
    positions = {u.unit_id: i for i, u in enumerate(units)}
    replacements, touched = {}, set()
    for edit in patch.edits:
        if any(s not in starts for s in edit.replace_starts):
            raise QualityFailure("Plan patch refers to an unknown original range")
        indexes = [starts.index(s) for s in edit.replace_starts]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))) or touched.intersection(edit.replace_starts):
            raise QualityFailure("Plan patch ranges must be contiguous, ordered and nonoverlapping")
        first, last = indexes[0], indexes[-1]
        end = positions[starts[last + 1]] if last + 1 < len(starts) else len(units)
        selected = units[positions[starts[first]]:end]
        replacement = Boundaries(sections=[Boundary.model_validate(s.model_dump()) for s in edit.sections])
        # A patch may split or merge this interval, never shift its outer bounds.
        list(ranges(replacement, selected))
        replacements[first] = (last, replacement.sections)
        touched.update(edit.replace_starts)
    if not required_starts <= touched:
        raise QualityFailure("Local patch did not address every blocking range")
    if not touched <= required_starts:
        raise QualityFailure("Local patch attempted to rewrite an unrelated accepted range")
    sections, index = [], 0
    while index < len(starts):
        if index in replacements:
            last, values = replacements[index]
            sections.extend(values)
            index = last + 1
        else:
            sections.append(value.sections[index])
            index += 1
    result = Boundaries(sections=sections)
    issues = program_issues(result, units)
    if issues:
        raise QualityFailure("Plan patch failed deterministic acceptance: " + canonical(issues))
    return result


def plan_window(generation, units):
    prefix = "source.learning.v7." + units[0].unit_id
    pages = source_cards(units)
    raw = generation.call(prefix + ".plan", "next.structure",
        "按完整、连续、可独立学习的知识主题规划Atomic，不写正文。一个主题的定义、条件、训练、公式、例题、代码和局限通常归在一起。"
        "直接输出综合主题范围，不先逐页输出再分篇。换页、续页、角色变化不构成新主题。不同算法保持独立，不预设篇数。"
        "排除纯封面、版权、课程标识、行政导论和纯导航；导论里的实质定义、背景、条件仍保留。第一项从first_unit开始。"
        "每项给真实start_unit，程序推导结束；相邻同一主题共用topic_key。每CONTENT只选1到3条代表性逐字教学引文，单条不超过180字符，禁止回显所有单元。"
        "kind是解析版式提示，HEADING也可能是完整定义，不能机械认定无教学价值；纯方法名/标题本身不能作为教学证明。"
        "units元组为[unit_id,kind,text]。IMAGE_PENDING是尚待视觉核实的真实图片：不引用占位文字，不删除未知图片；无文字支撑时support_state=PENDING_VISION且teaching_support为空。"
        "TEXT表示有实际文字教学支撑。reason简短说明独立学习问题，避免为例题/代码单独起篇。",
        {"first_unit": units[0].unit_id, "pages": pages, "policy": PLANNING_POLICY}, TopicPlan,
        use_pro_model=True, max_attempts=1)
    boundaries = Boundaries(sections=[Boundary.model_validate(s.model_dump()) for s in raw.sections])
    # First validate locations, then merge declared topics and retain every witness.
    list(ranges(boundaries, units))
    merged = coalesce_topics(boundaries, units)
    hard_issues = program_issues(merged, units)
    starts = [s.start_unit for s in merged.sections]

    def check_review(value):
        if len(value.checked_starts) != len(starts) or set(value.checked_starts) != set(starts):
            raise QualityFailure("Planning review did not cover each actual range exactly once")
        if any(len(f.range_starts) != len(set(f.range_starts)) or not set(f.range_starts) <= set(starts) for f in value.findings):
            raise QualityFailure("Planning review finding must locate actual ranges")

    review = generation.call(prefix + ".review", "next.structure_exclusion_review",
        "只做一次独立语义检查。逐范围核对实际来源：排除是否误删教学、保留是否只有导航、证据是否有教学实质、主题是否按定义/例题/代码过碎。"
        "解析kind仅为版式提示，完整定义即使标HEADING也可作证；IMAGE_PENDING不能凭空断言图意。"
        "checked_starts覆盖所有实际范围。确需修正的错误写BLOCKING并列出所有需一起修改的range_starts，纯改名/风格偏好写ADVISORY。"
        "合并相邻范围时把双方起点都列出，拆分只定位原范围。不输出修订计划，不为建议制造阻断。",
        {"pages": pages, "ranges": merged.model_dump()["sections"], "program_issues": hard_issues}, Review,
        check_review, use_pro_model=True, max_attempts=1)
    required = {i["range_start"] for i in hard_issues}
    required.update(s for f in review.findings if f.category == "BLOCKING" for s in f.range_starts)
    final = merged
    if required:
        affected = [(s, selected) for s, selected in ranges(merged, units) if s.start_unit in required]
        patch = generation.call(prefix + ".patch", "next.structure_patch",
            "按已定位问题只修一次规划。仅输出edits：replace_starts是被替换的连续原范围起点，sections是覆盖同一区间的新范围。"
            "必须处理全部required_starts；不得修改其他已接受范围，不重述整份计划。每个编辑的首项保持原区间起点，结束由后一个未修改范围决定。"
            "可拆分行政与教学，或合并同时列出的相邻主题。CONTENT用1到3条180字符内的真实教学引文；HEADING可含有效定义，纯标题不能证明教学；未知图保持PENDING_VISION。"
            "修正后只有程序验收，不再模型复核，请自行确认排除、来源与主题语义正确。",
            {"required_starts": sorted(required), "findings": [f.model_dump() for f in review.findings if f.category == "BLOCKING"],
             "program_issues": hard_issues, "ranges": [s.model_dump() for s, _ in affected],
             "pages": source_cards([u for _, selected in affected for u in selected])}, Patch,
            use_pro_model=True, max_attempts=1)
        final = apply_patch(merged, patch, units, required)
    else:
        if hard_issues:
            raise QualityFailure("Planning has unresolved deterministic errors")
    receipt = {"policy": POLICY, "original": raw.model_dump(), "merged": merged.model_dump(),
               "review": review.model_dump(), "program_issues": hard_issues, "patched": bool(required),
               "final": final.model_dump(), "post_patch_semantic_review": False,
               "pending_vision_starts": [s.start_unit for s in final.sections if s.support_state == "PENDING_VISION"]}
    generation.journal.save(prefix + ".acceptance", {"first_unit": units[0].unit_id, "policy": POLICY}, receipt)
    return final
