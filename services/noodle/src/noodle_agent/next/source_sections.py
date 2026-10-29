"""Source headings are the default plan; no per-window semantic allocation."""

import re
import unicodedata
from collections import Counter

from .contracts import canonical, sha
from .generation_contracts import Unit
from .planning import QualityFailure
from .section_contracts import POLICY, Boundaries, PlanningExclusionCheck
from .segmentation import ranges, tokens


def units_from_blocks(artifact):
    result = []
    for block in artifact["blocks"]:
        text = block["text"] or (
            "教学图片：" + block["asset_ref"] if block.get("asset_ref") else ""
        )
        if not text.strip():
            continue
        # Preserve semantic blocks; a single oversize block fails rather than truncates.
        spans = [(0, len(text))] if block.get("asset_ref") else ranges(text, target=1200)
        for start, end in spans:
            result.append(
                Unit(
                    unit_id=f"u{len(result):05d}",
                    block_id=block["id"],
                    page=block["page"],
                    start=start,
                    end=end,
                    text=text[start:end],
                    kind=block["kind"],
                    asset_ref=block.get("asset_ref"),
                    visual=block.get("visual"),
                )
            )
    if not result:
        raise QualityFailure("No readable source units")
    return result


def generic_title(title):
    """Page labels in common scripts are navigation, never semantic boundaries."""
    title = unicodedata.normalize("NFKC", title).strip()
    number = r"[\d一二三四五六七八九十百千万零〇兩两]+"
    label = r"(?:slide|page|幻灯片|幻燈片|投影片|页|頁)"
    return bool(re.fullmatch(
        rf"(?:全部内容|{number}|{label}\s*(?:(?:number|no\.?)\s*)?{number}(?:\s*(?:/|of)\s*{number})?|第?\s*{number}\s*(?:页|頁|[张張]\s*(?:幻灯片|幻燈片|投影片)?))",
        title, re.IGNORECASE,
    ))


def outline_trust(outline):
    counts = Counter(n.title.strip().casefold() for n in outline.nodes)
    return {
        n.node_id: not generic_title(n.title) and counts[n.title.strip().casefold()] == 1
        for n in outline.nodes
    }


def document_profile(artifact, outline):
    headings = [
        b["text"].strip() for b in artifact["blocks"] if b["kind"].upper() in {"TITLE", "HEADING"}
    ]
    generic = not any(outline_trust(outline).values())
    repeat = 1 - len(set(headings)) / max(1, len(headings))
    kinds = {n.kind for n in outline.nodes}
    joined = " ".join(headings).casefold()
    paper = bool(
        re.search(r"\babstract\b|摘要", joined)
        and re.search(r"\b(?:methods?|experiments?|results?)\b|实验|方法", joined)
    )
    if paper:
        kind, confidence = "PAPER", "MEDIUM"
    elif "CHAPTER" in kinds:
        kind, confidence = "BOOK", "MEDIUM"
    elif artifact["page_count"] >= 10 and (
        repeat > 0.15 or (generic and len(headings) > artifact["page_count"])
    ):
        kind, confidence = "SLIDES", "MEDIUM"
    elif not generic:
        kind, confidence = "ARTICLE", "MEDIUM"
    else:
        kind, confidence = "UNKNOWN", "LOW"
    return {
        "kind": kind,
        "confidence": confidence,
        "signals": {
            "generic_bookmarks": generic,
            "heading_repetition": round(repeat, 3),
            "page_count": artifact["page_count"],
        },
        "layout_policy": POLICY,
        "writing_policy": "source-faithful-v1",
    }


def validate_boundaries(value, units):
    positions = {u.unit_id: i for i, u in enumerate(units)}
    ids = [s.start_unit for s in value.sections]
    if len(set(ids)) != len(ids) or any(i not in positions for i in ids):
        raise QualityFailure("Invalid or duplicate source boundary")
    order = [positions[i] for i in ids]
    if order != sorted(order) or order[0] != 0:
        raise QualityFailure("Boundaries must start at the first unit and retain source order")
    # Endpoints cannot be invented inside a sentence or outside the source.
    return value


PLAN_POLICY = "learning-objects-v1"


def load_source_plan(generation, parsed, units):
    # A published job keeps its checked plan even when the default policy evolves.
    import json

    from .repository import Conflict, one
    with generation.store.db.engine.connect() as c:
        published = one(c, "SELECT 1 FROM job_checkpoints WHERE job_id=:j AND stage LIKE 'source.publish.%' AND state='DONE' LIMIT 1", j=generation.lease.job_id)
        frozen = one(c, "SELECT * FROM job_checkpoints WHERE job_id=:j AND stage LIKE 'source.sections.plan.%' AND state='DONE' ORDER BY created_at DESC LIMIT 1", j=generation.lease.job_id) if published else None
    if frozen:
        plan = json.loads(generation.store.read(frozen["artifact_ref"], frozen["artifact_sha256"]))
        if plan["parse_id"] != parsed["parse_id"] or plan["source_revision_id"] != generation.lease.input["revision_id"]:
            raise Conflict("Frozen source plan no longer matches its source/parse")
        generation.active_plan_policy = plan["planning_policy"]
        return plan
    legacy = published and generation.journal.exists("source.sections.plan")
    mode = generation.profile.get("planning_mode")
    plan_policy = "adaptive-source-v1" if mode == "adaptive-source-v1" else PLAN_POLICY if mode == "learning-objects-v1" else "coherent-source-v8" if mode == "learning-topics-v8" else "coherent-source-v7" if mode == "learning-topics-v7" else "coherent-source-v6"
    generation.active_plan_policy = POLICY if legacy else plan_policy
    stage = "source.sections.plan" if legacy else "source.sections.plan." + plan_policy
    return generation.journal.compute(stage, {"parse_id": parsed["parse_id"], "policy": generation.active_plan_policy}, lambda: make_plan(generation, parsed, units))


def validate_assisted_boundaries(value, units, coherent=False, teaching=False, support=False):
    validate_boundaries(value, units)
    if coherent and any(re.search(r"[（(](?:续\d*|continued)[）)]$", s.title, re.IGNORECASE) for s in value.sections):
        raise QualityFailure("Do not create continuation slide boundaries. Combine adjacent continuation slides into the same section; the program splits only when the section exceeds its token budget.")
    if teaching:
        positions = {u.unit_id: i for i, u in enumerate(units)}
        for index, section in enumerate(value.sections):
            end = positions[value.sections[index + 1].start_unit] if index + 1 < len(value.sections) else len(units)
            selected = {u.unit_id: u for u in units[positions[section.start_unit]:end]}
            if support and section.disposition == "CONTENT":
                if not section.teaching_support:
                    raise QualityFailure("CONTENT needs concrete teaching_support, not just a new topic title")
                for proof in section.teaching_support:
                    unit = selected.get(proof.unit_id)
                    if unit is None or proof.quote not in unit.text:
                        raise QualityFailure("Teaching support must quote a real unit inside this range")
                    if unit.kind.upper() in {"HEADING", "TITLE"} and not re.search(r"[=$<>]|\\(?:frac|sum|begin)", unit.text):
                        raise QualityFailure("A bare heading is not teaching support; cite actual content or exclude pure navigation")
            if section.disposition != "EXCLUDE":
                continue
            if not section.reason.strip():
                raise QualityFailure("Excluded planning ranges need a concrete non-teaching reason")
            if any(u.asset_ref and not (u.visual or {}).get("is_decorative") for u in units[positions[section.start_unit]:end]):
                raise QualityFailure("Do not exclude an unverified image from compact text hints; keep its range as CONTENT for grounded review")
    return value


def make_plan(generation, parsed, units):
    from .atomic_planning import (
        PLANNING_POLICY,
        coalesce_topics,
        granularity_report,
        page_cards,
        structure_windows,
    )
    from .document_outline import build_outline, unit_node_map

    store = generation.store
    mode = getattr(generation, "profile", {}).get("planning_mode")
    teaching = mode in {"learning-topics-v5", "learning-topics-v6", "learning-topics-v7", "learning-topics-v8", "learning-objects-v1", "adaptive-source-v1"}
    supported = mode == "learning-topics-v6"
    with store.db.engine.connect() as c:
        from .repository import one

        source = one(
            c,
            "SELECT * FROM source_revisions WHERE id=:id",
            id=generation.lease.input["revision_id"],
        )
    original = store.read(source["blob_ref"], source["sha256"])
    outline = build_outline(original, parsed["artifact"]["blocks"])
    substantive = [u for u in units if u.kind.upper() not in {"HEADER", "FOOTER", "PAGE_NUMBER"}]
    layout = [
        {
            "unit_id": u.unit_id,
            "category": "NAVIGATION",
            "reason": "解析器标注的页眉、页脚或页码",
            "duplicate_of": None,
        }
        for u in units
        if u.kind.upper() in {"HEADER", "FOOTER", "PAGE_NUMBER"}
    ]
    if not substantive:
        raise QualityFailure("Source contains no substantive units")
    trust = outline_trust(outline)
    generic = not all(trust.values())
    profile = document_profile(parsed["artifact"], outline)
    if mode == "adaptive-source-v1":
        from .adaptive_planning import plan_adaptive
        from .material_profiles import assess_source

        profile = assess_source(original, parsed["artifact"], outline, generation.profile.get("material_type"))
    positions = {u.unit_id: i for i, u in enumerate(substantive)}
    if mode == "adaptive-source-v1":
        boundaries, basis = plan_adaptive(generation, substantive, outline, profile)
    elif mode == "learning-objects-v1":
        from .learning_objects import plan_objects
        boundaries = plan_objects(generation, substantive)
        basis = "REVIEWED_LEARNING_OBJECTS"
    elif mode in {"learning-topics-v7", "learning-topics-v8"}:
        from .learning_plan import plan_window, windows
        if mode == "learning-topics-v8":
            from .learning_plan_v8 import plan_window

        starts = [s for window in windows(substantive) for s in plan_window(generation, window).sections]
        boundaries = Boundaries(sections=starts)
        basis = "REVIEWED_LEARNING_TOPICS_V7"
    elif not teaching and not generic and not any(i["severity"] == "ERROR" for i in outline.issues):
        owners = unit_node_map(outline, substantive)
        by_id = {n.node_id: n for n in outline.nodes}
        starts = []
        previous = object()
        for unit in substantive:
            owner = owners.get(unit.unit_id)
            if owner != previous:
                node = by_id.get(owner)
                parent = by_id.get(node.parent_id) if node else None
                starts.append(
                    {
                        "start_unit": unit.unit_id,
                        "title": node.title if node else "开篇",
                        "parent_title": parent.title if parent else "",
                    }
                )
                previous = owner
        boundaries = Boundaries.model_validate({"sections": starts})
        basis = "SOURCE_OUTLINE"
    else:
        owners = unit_node_map(outline, substantive)
        by_id = {n.node_id: n for n in outline.nodes}
        safe_order = not any(i["severity"] == "ERROR" for i in outline.issues)
        spans = []
        for unit in substantive:
            owner = owners.get(unit.unit_id)
            trusted = owner if not teaching and safe_order and trust.get(owner) else None
            if not spans or spans[-1][0] != trusted:
                spans.append((trusted, []))
            spans[-1][1].append(unit)
        starts = []
        for owner, values in spans:
            if owner:
                node = by_id[owner]
                parent = by_id.get(node.parent_id)
                starts.append({"start_unit": values[0].unit_id, "title": node.title,
                               "parent_title": parent.title if parent and trust.get(parent.node_id) else ""})
                continue
            for window, pages in structure_windows(values, maximum=16000 if teaching else 6500):
                assisted = generation.call(
                    "source.boundaries." + window[0].unit_id, "next.structure",
                    "规划可独立阅读、足够综合的Atomic来源主题，不写正文，不以页为单位分篇。"
                    "先识别连续的主要知识对象/学习问题；同一对象的定义、假设、公式、训练步骤、完整例题、实现和限制优先保留在同一Atomic，内部再分章节。"
                    "重复标题、换页、续页、新公式或代码截图都不自动产生Atomic边界。无标题的连续代码和输出优先与其问题、准备步骤一起组织。"
                    "只有转向一个可独立解释的问题，或证据预算确实要求时才拆分；相同主题的过长范围由程序在保留完整单元前提下处理。"
                    "不要固定篇数，不为缩短篇数合并无关算法，不省略难内容、重要条件或例题。"
                    "disposition=EXCLUDE仅用于纯封面、课程标识、纯行政导论/目录、版权等无教学实质范围，exclusion_category写ADMINISTRATIVE或NAVIGATION，并说明具体reason。"
                    "这些范围不生成Atomic，禁止把它们包装成课程介绍笔记或混入知识标题。不能只因标题叫引言而排除：定义、问题背景、公式条件、例题、代码、局限都属于CONTENT。"
                    "混合页先在真实单元边界分开行政与教学内容；未知图片保留CONTENT交后续图文检查，不凭短提示删除。"
                    "每项仅返回真实start_unit、综合title、parent_title、同一连续主题共用的topic_key、说明为何从此处另起Atomic的reason。"
                    "CONTENT须在teaching_support给范围内实际教学单元unit_id、逐字quote与知识角色；不能只引纯标题或用新主题作为知识证明。纯标题可并入相邻教学主题作为导航，或EXCLUDE。"
                    "topic_key不是全库实体ID。不要把原理/例子等角色各自命名为不同topic_key。第一项必须从first_unit开始；程序推导结束边界、页码和稳定ID。",
                    {"first_unit": window[0].unit_id, "pages": pages, "granularity": PLANNING_POLICY}, Boundaries,
                    lambda v, window=window: validate_assisted_boundaries(v, window, True, teaching, supported),
                    **({"use_pro_model": True} if teaching else {}),
                )
                starts.extend(s.model_dump() for s in assisted.sections)
        boundaries = Boundaries.model_validate({"sections": starts})
        basis = "MIXED_STRUCTURE_ASSIST" if any(trust.values()) else "LOCAL_STRUCTURE_ASSIST"
    validate_boundaries(boundaries, substantive)
    boundaries = coalesce_topics(boundaries, substantive)
    if mode == 'adaptive-source-v1' and getattr(generation, 'profile', {}).get('content_policy') == 'concise-foundation-v1':
        from .learning_labels import merge_short_tails
        boundaries = merge_short_tails(boundaries, substantive)
    if teaching and mode not in {"learning-topics-v7", "learning-topics-v8", "learning-objects-v1", "adaptive-source-v1"}:
        excluded, retained = [], []
        for index, boundary in enumerate(boundaries.sections):
            end = positions[boundaries.sections[index + 1].start_unit] if index + 1 < len(boundaries.sections) else len(substantive)
            selected = substantive[positions[boundary.start_unit]:end]
            if boundary.disposition == "EXCLUDE":
                excluded.append({"start_unit": boundary.start_unit, "reason": boundary.reason,
                    "units": [u.model_dump() for u in selected]})
            else:
                retained.append({"start_unit": boundary.start_unit, "title": boundary.title,
                    "teaching_support": [p.model_dump() for p in boundary.teaching_support],
                    "support_state": boundary.support_state,
                    "pages": [{k: v for k, v in card.items() if k in {"page", "headings", "continued_from_heading"}} for card in page_cards(selected)] if supported else page_cards(selected),
                    "support_units": [{"unit_id": u.unit_id, "text": u.text} for u in selected if u.unit_id in {p.unit_id for p in boundary.teaching_support}]})
        if excluded or retained:
            def checked_exclusions(value):
                expected = {v["start_unit"] for v in excluded}
                content = {v["start_unit"] for v in retained}
                if not value.passed or value.issues or set(value.checked_starts) != expected or len(value.checked_starts) != len(expected) or set(value.checked_content_starts) != content or len(value.checked_content_starts) != len(content):
                    raise QualityFailure("Planning exclusion check rejected missing or teaching source content: " + canonical(value.model_dump()))
            generation.call("source.exclusions.review", "next.structure_exclusion_review",
                "独立核对拟排除范围的完整原文。只有纯封面、课程标识、无教学实质的行政导论/目录、版权可排除。"
                "不要将定义、问题背景、假设、公式、实例、代码、限制或混合教学内容当行政信息。不是按标题关键词判定。"
                "逐排除范围确认无教学价值；也检查content_ranges没有把纯行政信息包装成Atomic，且连续知识对象没有按定义/例子等角色过度拆篇。"
                "无法确认或有实质问题时passed=false并给具体问题。checked_starts完整列出排除范围起点，checked_content_starts完整列出保留范围起点。",
                {"excluded_ranges": excluded, "content_ranges": retained}, PlanningExclusionCheck, checked_exclusions, use_pro_model=True)
    plans, nodes, parents = [], [], {}
    for index, boundary in enumerate(boundaries.sections):
        start = positions[boundary.start_unit]
        end = (
            positions[boundaries.sections[index + 1].start_unit]
            if index + 1 < len(boundaries.sections)
            else len(substantive)
        )
        selected = substantive[start:end]
        if boundary.disposition == "EXCLUDE":
            layout.extend({"unit_id": u.unit_id, "category": boundary.exclusion_category,
                "reason": boundary.reason, "duplicate_of": None} for u in selected)
            continue
        # Split long source sections at whole units only, retaining source hierarchy.
        batches, current, current_tokens = [], [], 0
        for unit in selected:
            unit_tokens = tokens(unit.text)
            if unit_tokens > 8000:
                raise QualityFailure(
                    "A source block exceeds writing budget; structural split required"
                )
            limit = 7500 if getattr(generation, 'profile', {}).get('content_policy') == 'concise-foundation-v1' else 6500
            if current and current_tokens + unit_tokens > limit:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(unit)
            current_tokens += unit_tokens
        if current:
            batches.append(current)
        parent = None
        if boundary.parent_title:
            # Reuse only the current contiguous parent, never group distant repeats.
            if plans and plans[-1]["parent_title"] == boundary.parent_title:
                parent = plans[-1]["parent_id"]
            else:
                parent = "source_parent_" + sha(canonical([index, boundary.parent_title]))[:18]
                nodes.append(
                    {
                        "node_id": parent,
                        "parent_id": None,
                        "title": boundary.parent_title,
                        "kind": "CHAPTER",
                        "level": 1,
                        "position": len(nodes),
                        "start": selected[0].page,
                        "end": selected[-1].page,
                        "basis": basis,
                    }
                )
                parents[parent] = nodes[-1]
        for continuation, values in enumerate(batches):
            key = "source_" + sha(canonical([values[0].block_id, values[0].start]))[:20]
            title = boundary.title + (f"（续{continuation + 1}）" if len(batches) > 1 else "")
            node = {
                "node_id": key,
                "parent_id": parent,
                "title": title,
                "kind": "SECTION",
                "level": 2 if parent else 1,
                "position": len(nodes),
                "start": values[0].page,
                "end": values[-1].page,
                "basis": basis,
            }
            nodes.append(node)
            if parent:
                parents[parent]["end"] = max(parents[parent]["end"], values[-1].page)
            plans.append(
                {
                    "key": key,
                    "title": title,
                    "parent_id": parent,
                    "parent_title": boundary.parent_title,
                    "unit_ids": [u.unit_id for u in values],
                    "pages": sorted({u.page for u in values}),
                    "asset_ids": list(dict.fromkeys(u.asset_ref for u in values if u.asset_ref)),
                    "input_sha256": sha(canonical([u.model_dump() for u in values])),
                    "topic_key": boundary.topic_key,
                    "outline_node_id": boundary.outline_node_id,
                    "chapter_key": boundary.chapter_key,
                    "teaching_support": [p.model_dump() for p in boundary.teaching_support],
                    "support_state": boundary.support_state,
                    "split_reason": boundary.reason or "来源语义目录边界",
                    "pending_image_unit_ids": [u.unit_id for u in values if u.asset_ref],
                    "split_origin": "EVIDENCE_BUDGET" if len(batches) > 1 else "SEMANTIC_TOPIC",
                }
            )
    from .learning_labels import localize
    generation.document_source_title = parsed['artifact'].get('title', '')
    plans = localize(generation, plans, substantive, outline)
    by_key = {p['key']: p for p in plans}
    for node in nodes:
        if node['node_id'] in by_key:
            node['title'] = by_key[node['node_id']]['title']
    coverage = Counter(u for p in plans for u in p["unit_ids"])
    coverage.update(e["unit_id"] for e in layout)
    if set(coverage) != {u.unit_id for u in units} or any(n != 1 for n in coverage.values()):
        raise QualityFailure("Source plan must account for each unit exactly once")
    return {
        "policy": POLICY,
        "planning_policy": getattr(generation, "active_plan_policy", POLICY),
        "planning_model": (getattr(getattr(generation, 'agent', None), 'pro_model', None)
            if getattr(generation, 'profile', {}).get('pro_usage') == 'paper-plan-only' and profile['kind'] == 'PAPER'
            else getattr(generation, "profile", {}).get("planning_model")),
        "exclusion_policy": "trusted-book-navigation-or-one-topic-confirmation" if mode == "adaptive-source-v1" else "learning-objects-v1" if mode == "learning-objects-v1" else "one-review-local-patch-v8" if mode == "learning-topics-v8" else "one-review-local-patch-v7" if mode == "learning-topics-v7" else "reviewed-non-teaching-v1" if teaching else "legacy-layout-only",
        "parse_id": parsed["parse_id"],
        "source_revision_id": generation.lease.input["revision_id"],
        "display_titles": getattr(generation, 'display_titles', {}),
        "display_title": getattr(generation, 'document_display_title', ''),
        "plans": plans,
        "execution_batches": execution_batches(plans, profile["kind"]),
        "layout_exclusions": layout,
        "outline": {
            "policy": POLICY,
            "source_sha256": source["sha256"],
            "page_count": parsed["artifact"]["page_count"],
            "nodes": nodes,
            "segments": [],
            "issues": [],
        },
        "basis": basis,
        "document_profile": profile,
        **({"source_outline": outline.model_dump(), "chapter_tasks": chapter_tasks(plans, outline)} if mode == "adaptive-source-v1" else {}),
        "granularity": granularity_report(plans, substantive),
    }


def execution_batches(plans, kind):
    """Freeze bounded execution membership independently of the reading tree."""
    batches, current, owner = [], [], None
    for plan in plans:
        scope = plan.get("chapter_key") or plan["parent_id"] or (plan["key"] if kind == "BOOK" else "document")
        if current and (scope != owner or len(current) >= 4):
            batches.append(current)
            current = []
        current.append(plan["key"])
        owner = scope
    if current:
        batches.append(current)
    return [{"key": "execution_" + sha(canonical(keys))[:20], "plan_keys": keys} for keys in batches]


def chapter_tasks(plans, outline):
    by_id = {n.node_id: n for n in outline.nodes}
    tasks = {}
    for plan in plans:
        key = plan.get("chapter_key")
        if key:
            task = tasks.setdefault(key, {"chapter_key": key, "title": by_id[key].title,
                "start": by_id[key].start, "end": by_id[key].end, "plan_keys": []})
            task["plan_keys"].append(plan["key"])
            task['title'] = plan.get('chapter_display_title') or task['title']
    return list(tasks.values())
