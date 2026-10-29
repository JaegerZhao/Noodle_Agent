"""Compact learning-object planning, based on the working source-boundary pipeline."""

from typing import Literal

from pydantic import Field, model_validator

from .atomic_planning import PLANNING_POLICY, coalesce_topics, structure_windows
from .contracts import Contract, Id, Text
from .section_contracts import Boundaries, Boundary


class LearningObject(Contract):
    start_unit: Id
    title: Text
    topic_key: str = ""
    parent_title: str = ""
    reason: str = ""
    disposition: Literal["CONTENT", "EXCLUDE"] = "CONTENT"
    exclusion_category: Literal["ADMINISTRATIVE", "NAVIGATION"] = "ADMINISTRATIVE"


class ObjectPlan(Contract):
    topics: list[LearningObject] = Field(min_length=1)
    restore_exclusions: list[Id] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def historical_boundaries(cls, value):
        if isinstance(value, dict) and "sections" in value and "topics" not in value:
            # Historical responses remain unchanged on disk. Auxiliary witnesses
            # do not belong to the source-range contract and are not prerequisites.
            allowed = set(LearningObject.model_fields)
            return {"topics": [{k: v for k, v in section.items() if k in allowed} for section in value["sections"]]}
        return value


def boundaries(value):
    return Boundaries(sections=[Boundary.model_validate(t.model_dump()) for t in value.topics])


def validate_locations(value, units, *, final=False):
    from .planning import QualityFailure
    from .source_sections import validate_assisted_boundaries

    positions = {u.unit_id: i for i, u in enumerate(units)}
    starts = [t.start_unit for t in value.topics]
    if len(set(starts)) != len(starts) or any(s not in positions for s in starts):
        raise QualityFailure("Learning objects need real, unique source start units")
    if [positions[s] for s in starts] != sorted(positions[s] for s in starts):
        raise QualityFailure("Learning objects must follow source order")
    if final:
        validate_assisted_boundaries(boundaries(value), units, True, True, False)
    return value


def summary(value, units):
    positions = {u.unit_id: i for i, u in enumerate(units)}
    result = []
    for i, topic in enumerate(value.topics):
        stop = positions[value.topics[i + 1].start_unit] if i + 1 < len(value.topics) else len(units)
        selected = units[positions[topic.start_unit]:stop]
        result.append({**topic.model_dump(), "pages": sorted({u.page for u in selected}),
            "headings": [u.text for u in selected if u.kind.upper() in {"HEADING", "TITLE"}],
            "has_images": any(u.asset_ref for u in selected)})
    return result


def fragmentation(value, units):
    positions = {u.unit_id: i for i, u in enumerate(units)}
    groups = coalesce_topics(boundaries(value), units).sections
    pages, singletons, count = set(), 0, 0
    for i, topic in enumerate(groups):
        if topic.disposition != "CONTENT":
            continue
        stop = positions[groups[i + 1].start_unit] if i + 1 < len(groups) else len(units)
        selected = {u.page for u in units[positions[topic.start_unit]:stop]}
        pages.update(selected)
        count += 1
        singletons += len(selected) == 1
    return {"object_count": count, "content_pages": len(pages), "single_page_objects": singletons,
        "resembles_page_outline": count >= 12 and count >= .7 * len(pages) and singletons >= .75 * count}


def retain_image_ranges(value, units):
    """Preserve unknown/teaching images without retrying a semantic decision.

    Prefer attaching an excluded image suffix to the following learning object.
    Keep its navigation prefix excluded, avoiding a new cover/contents Atomic.
    No image is declared decorative here; the existing visual stage decides it.
    """
    positions = {u.unit_id: i for i, u in enumerate(units)}
    topics = list(value.topics)
    index = 0
    while index < len(topics):
        topic = topics[index]
        end = positions[topics[index + 1].start_unit] if index + 1 < len(topics) else len(units)
        start = positions[topic.start_unit]
        images = [u for u in units[start:end] if u.asset_ref and not (u.visual or {}).get("is_decorative")]
        if topic.disposition == "EXCLUDE" and images:
            if index + 1 < len(topics) and topics[index + 1].disposition == "CONTENT":
                following = topics[index + 1]
                topics[index + 1] = following.model_copy(update={
                    "start_unit": images[0].unit_id,
                    "reason": following.reason + "；保留前置待判定图片，交后续图文处理",
                })
                if images[0].unit_id == topic.start_unit:
                    topics.pop(index)
            elif index and topics[index - 1].disposition == "CONTENT":
                # A trailing range has no following object: retain it with the
                # preceding one instead of inventing a standalone cover note.
                topics.pop(index)
                continue
            else:
                topics[index] = topic.model_copy(update={
                    "disposition": "CONTENT", "title": "图文内容",
                    "reason": "图片尚未判定，保留来源交后续图文处理",
                })
        index += 1
    return value.model_copy(update={"topics": topics})


def apply_confirmation(original, confirmed, units):
    """A semantic confirmation cannot silently delete explicit source exclusions."""
    from .planning import QualityFailure

    excluded = {t.start_unit: t for t in original.topics if t.disposition == "EXCLUDE"}
    if not set(confirmed.restore_exclusions) <= set(excluded):
        raise QualityFailure("Confirmation restores an unknown excluded source range")
    topics = {t.start_unit: t for t in confirmed.topics}
    for start, topic in excluded.items():
        if start not in confirmed.restore_exclusions:
            topics.setdefault(start, topic)
    positions = {u.unit_id: i for i, u in enumerate(units)}
    if any(start not in positions for start in topics):
        raise QualityFailure("Confirmation names an unknown source boundary")
    result = ObjectPlan(topics=sorted(topics.values(), key=lambda t: positions[t.start_unit]))
    result = retain_image_ranges(result, units)
    validate_locations(result, units, final=True)
    # Granularity is semantic feedback, not a post-correction publication gate.
    # Only real source IDs, order/coverage and the structured contract can block.
    return result


def plan_window(generation, units, cards):
    material = getattr(generation, "material_profile", None)
    context = {"material_profile": material} if material else {}
    guidance = ("\n当前资料策略：" + material["planning_guidance"]) if material else ""
    stage = "source.learning.objects." + units[0].unit_id
    first = generation.call(stage + ".plan", "next.structure",
        "为来源资料规划完整、可独立学习的知识对象。topics每项将成为一篇Atomic，不是页面目录，也不是Atomic内部小节。"
        "先识别方法/知识对象，再把同一对象的定义、假设、公式、训练、例题、实现、输出和局限收在同一范围；这些角色留给Atomic正文分小节。"
        "不要把Introduction/Training/Worked Example/Implementation/Visualization等角色分别建成Atomic。"
        "不同算法和独立学习问题应分开，不以固定篇数或每页一篇规划。长主题的证据预算由程序处理。"
        "仅输出真实start_unit、综合title、topic_key、parent_title、简短reason与disposition。程序推导结束位置、页码和稳定ID。"
        "首项从first_unit开始。封面、纯课程标识、无教学内容的导论/目录、纯章节标题和版权也须显式输出EXCLUDE范围与理由，不能漏掉这些来源。"
        "有实质定义、条件、例题的导论保留。未知图片属于后续视觉工作，保留其CONTENT范围；本阶段不解释图片、不复制公式、不输出教学引文/支持单元列表。" + guidance,
        {"first_unit": units[0].unit_id, "pages": cards, "granularity": PLANNING_POLICY, **context}, ObjectPlan,
        lambda v: validate_locations(v, units), use_pro_model=True, max_attempts=2)
    # This is the one semantic confirmation/correction, not a pass/fail loop.
    # It returns the actual final compact plan, so an observed error can be fixed.
    final = generation.call(stage + ".confirm", "next.structure_exclusion_review",
        "确认并直接给出最终完整topics计划，只做这一次语义归并/排除修正。每个topic是一篇完整Atomic，不是每页小节。"
        "重点将同一方法的引言、数学式、训练、平滑/细节、例题、实现和输出归并；保留真正独立的知识对象，不强行合并不同算法。"
        "若候选近似一页一篇，必须重新判断学习对象完整性，不可仅原样复制。标题/文风差异不用反复修改。"
        "纯封面、课程标识、纯章节标题、行政导航与版权输出EXCLUDE，不生成知识笔记；有教学实质的背景/导论仍保留。"
        "程序保留候选已明确EXCLUDE的范围，不必重述这些不变决定。若排除有误，显式返回该起点的CONTENT；若恢复并归入前一个主题，用restore_exclusions列其原起点。"
        "所有来源最终必须按顺序覆盖，遗漏且未分类的开篇需补明确范围；结束由下一起点推导。"
        "未知图片保留给后续视觉，不要求图片先有OCR或被识别，不把图片定位当文字知识。本阶段不需要任何引文、证据角色或长公式。"
        "输出最终完整topics，不返回passed、findings或核对ID清单。程序做来源完整性验收，不再调用模型复核这份修正。" + guidance,
        {"first_unit": units[0].unit_id, "candidate_objects": summary(first, units),
         "granularity_feedback": fragmentation(first, units),
         "source_pages": cards, **context, "uncovered_prefix": [u.model_dump() for u in units[:next(i for i, u in enumerate(units) if u.unit_id == first.topics[0].start_unit)]]},
        ObjectPlan, lambda v: apply_confirmation(first, v, units), use_pro_model=True, max_attempts=2)
    result = coalesce_topics(boundaries(final), units)
    generation.journal.save(stage + ".acceptance", {"first_unit": units[0].unit_id, "policy": "learning-objects-v1"},
        {"policy": "learning-objects-v1", "original": first.model_dump(), "final": result.model_dump(),
         "review_mode": "ONE_PLAN_ONE_CONFIRMATION", "evidence_requirement": "SOURCE_RANGES_ONLY",
         "post_confirmation_semantic_review": False,
         "post_confirmation_granularity": fragmentation(final, units),
         "image_exclusion_policy": "RETAIN_WITH_ADJACENT_CONTENT_FOR_VISUAL_SELECTION",
         "explicit_exclusion_policy": "Retain original decisions unless explicitly replaced or restored",
         "pending_image_unit_ids": [u.unit_id for u in units if u.asset_ref]})
    return result


def plan_objects(generation, units):
    result = []
    for window, cards in structure_windows(units, maximum=16000):
        result.extend(plan_window(generation, window, cards).sections)
    return coalesce_topics(Boundaries(sections=result), units)
