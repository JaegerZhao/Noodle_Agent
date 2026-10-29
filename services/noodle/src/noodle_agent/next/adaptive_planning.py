"""Material-specific planning; trusted book chapters are bounded source tasks."""

import bisect
import re
from collections import Counter

from .atomic_planning import NATIVE_BUDGET
from .contracts import sha
from .section_contracts import Boundaries, Boundary
from .segmentation import tokens


def book_boundaries(units, outline):
    """Use the largest containing TOC scope that fits; never combine different chapters."""
    by_id = {n.node_id: n for n in outline.nodes}
    clean = lambda s: re.sub(r"[^\w]", "", re.sub(r"<[^>]+>", "", s).casefold())
    toc_titles = {clean(n.title) for n in outline.nodes}
    headings, pages = {}, [u.page for u in units]
    costs = [0]
    for i, unit in enumerate(units):
        costs.append(costs[-1] + tokens(unit.text))
        if unit.kind.upper() in {"TITLE", "HEADING"}:
            headings.setdefault(clean(unit.text), []).append(i)
    starts, previous = {}, 0
    for node in outline.nodes:
        matches = [i for i in headings.get(clean(node.title), []) if abs(units[i].page - node.start) <= 1 and i >= previous]
        start = min(matches, key=lambda i: (abs(units[i].page - node.start), i)) if matches else bisect.bisect_left(pages, node.start)
        starts[node.node_id] = previous = max(previous, min(start, len(units)))
    children = {}
    for node in outline.nodes:
        children.setdefault(node.parent_id, []).append(node)
    spans = []

    def visit(node, start, end, chapter):
        if start >= end:
            return
        if node.kind in {"CHAPTER", "APPENDIX"} or chapter is None and node.kind != "SECTION":
            chapter = node.node_id
        nested = children.get(node.node_id, [])
        if not nested or costs[end] - costs[start] <= NATIVE_BUDGET and node.kind != "PART":
            spans.append((node.node_id, chapter, units[start:end]))
            return
        cursor = start
        for i, child in enumerate(nested):
            a = max(start, starts[child.node_id])
            b = min(end, starts[nested[i + 1].node_id]) if i + 1 < len(nested) else end
            if cursor < a and i == 0:
                # Chapter/section introductions travel with the first child,
                # rather than becoming separate short introductory Atomics.
                a = cursor
            elif cursor < a:
                spans.append((node.node_id, chapter, units[cursor:a]))
            visit(child, a, b, chapter)
            cursor = max(cursor, b)

    roots = children.get(None, [])
    cursor = 0
    for i, node in enumerate(roots):
        start = starts[node.node_id]
        if cursor < start:
            spans.append((node.node_id, node.node_id, units[cursor:start]))
        end = starts[roots[i + 1].node_id] if i + 1 < len(roots) else len(units)
        visit(node, start, end, None)
        cursor = end
    # A bare chapter/section heading belongs with its first actual section. This
    # also preserves same-page starts without inventing a separate teaching note.
    normalized = []
    prefix = []
    for index, (anchor, chapter, selected) in enumerate(spans):
        bare = all(u.kind.upper() in {"HEADING", "TITLE"} and not u.asset_ref for u in selected)
        if bare and index + 1 < len(spans):
            prefix.extend(selected)
            continue
        normalized.append((anchor, chapter, prefix + selected))
        prefix = []
    if prefix:
        normalized[-1][2].extend(prefix)
    grouped = []
    for anchor, chapter, selected in normalized:
        node = by_id.get(anchor)
        parent = by_id.get(node.parent_id) if node else None
        if grouped and parent and parent.kind == "SECTION" and grouped[-1][1] == chapter:
            previous_anchor = by_id[grouped[-1][0]]
            same_parent = previous_anchor.node_id == parent.node_id or previous_anchor.parent_id == parent.node_id
            if same_parent and sum(tokens(u.text) for u in [*grouped[-1][2], *selected]) <= NATIVE_BUDGET:
                grouped[-1] = (parent.node_id, chapter, grouped[-1][2] + selected)
                continue
        grouped.append((anchor, chapter, selected))
    repeated = Counter(anchor for anchor, _, _ in grouped)
    result = []
    for anchor, chapter, selected in grouped:
        node = by_id.get(anchor)
        parent = by_id.get(chapter)
        is_index = node and node.kind in {"INDEX", "BIBLIOGRAPHY"}
        # A verified pre-chapter contents/colophon range is navigation; Preface
        # is a separate node and is never excluded merely for being front matter.
        def navigational_text(text):
            lines = [line for line in text.splitlines() if line.strip()]
            toc_lines = sum(bool(re.match(r"^\s*(?:\d+(?:\.\d+)*|[IVX]+|[A-Z])\s+.+\s+\d+\s*$", line)) for line in lines)
            return bool(lines and toc_lines / len(lines) >= .5) or bool(re.search(
                r"copyright|ISBN|all rights reserved|permission|printed in|peer reviewers|quality of our publications|版权|出版", text, re.IGNORECASE))

        substantive_front = any(u.kind.upper() == "TEXT" and len(u.text) > 220
            and len(re.findall(r"[.!?]\s+[A-Z]|[。！？]", u.text)) >= 2
            and not navigational_text(u.text) for u in selected)
        front_navigation = node and node.kind == "FRONT" and not substantive_front and any(
            re.fullmatch(r"(?:brief\s+)?contents|目录", u.text.strip(), re.IGNORECASE) for u in selected)
        title = node.title if node else "来源开篇"
        if repeated[anchor] > 1:
            nested_titles = [u.text for u in selected if u.kind.upper() in {"TITLE", "HEADING"}
                and clean(u.text) != clean(title) and clean(u.text) in toc_titles]
            detail = " — ".join(dict.fromkeys([nested_titles[0], nested_titles[-1]])) if nested_titles else f"来源第{selected[0].page}–{selected[-1].page}页"
            title += "：" + detail
        result.append(Boundary(start_unit=selected[0].unit_id,
            title=title, parent_title=parent.title if parent and parent is not node else "",
            topic_key="toc:" + (anchor or sha(selected[0].unit_id)),
            outline_node_id=anchor, chapter_key=chapter,
            reason="可信目录范围；长范围仅按完整来源单元与证据预算细分",
            disposition="EXCLUDE" if is_index or front_navigation else "CONTENT", exclusion_category="NAVIGATION"))
    return Boundaries(sections=result)


def plan_adaptive(generation, units, outline, profile):
    from .learning_objects import plan_objects

    safe_book = profile["kind"] == "BOOK" and not any(i["severity"] == "ERROR" for i in outline.issues)
    if safe_book and any(n.kind == "CHAPTER" for n in outline.nodes):
        return book_boundaries(units, outline), "TRUSTED_BOOK_TOC"
    if profile["kind"] == "BOOK":
        profile["selected_route"] = "BOOK_STRUCTURE_FALLBACK"
        profile["fallback_reason"] = "Book directory is unavailable or has unresolved structural issues; use bounded topic planning"
    generation.material_profile = profile
    result = plan_objects(generation, units)
    return result, "MATERIAL_ADAPTED_TOPICS"
