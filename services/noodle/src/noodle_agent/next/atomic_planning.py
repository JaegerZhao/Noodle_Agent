"""Compact source-topic planning and auditable granularity, independent of execution."""

import re
from collections import Counter
from statistics import median

from .contracts import canonical
from .section_contracts import Boundaries
from .segmentation import tokens

NATIVE_BUDGET = 6500
PLANNING_POLICY = {
    "atomic_unit": "complete contiguous learning topic",
    "keep_together": ["definition", "assumptions", "formulas", "training or procedure", "worked example", "implementation", "limitations", "continuation slides"],
    "split_only_for": ["independent learning question", "bounded evidence budget"],
    "not_a_boundary": ["page break", "repeated heading", "new content role", "new code screenshot"],
    "native_token_budget": NATIVE_BUDGET,
    "count_target": None,
    "exclude_only": ["cover and course identification", "administrative introduction without teaching content", "copyright", "navigation without new knowledge"],
    "retain_in_introductions": ["definitions", "problem background", "conditions", "formulas", "examples", "code", "limitations"],
}


def page_cards(units):
    pages = {}
    for unit in units:
        pages.setdefault(unit.page, []).append(unit)
    cards, previous = [], ""
    for page, values in pages.items():
        heads = [u for u in values if u.kind.upper() in {"TITLE", "HEADING"}]
        title = heads[0].text.strip() if heads else ""
        cards.append({
            "page": page, "first_unit": values[0].unit_id,
            "headings": [{"id": u.unit_id, "title_hint": u.text[:220]} for u in heads],
            "continued_from_heading": previous if not title else "",
            "opening_hint": next((u.text[:200] for u in values if u not in heads and not u.asset_ref), values[0].text[:200]),
            "opening_unit": next((u.unit_id for u in values if u not in heads and not u.asset_ref), values[0].unit_id),
            "native_tokens": sum(tokens(u.text) for u in values),
            "assets": sum(bool(u.asset_ref) for u in values),
            "short_page_units": [{"id": u.unit_id, "kind": u.kind, "text": u.text} for u in values]
            if sum(len(u.text) for u in values) <= 1000 else [],
        })
        previous = title or previous
    return cards


def structure_windows(units, maximum=6500):
    """Partition compact page cards, never arbitrary body units inside a slide."""
    by_page = {}
    for unit in units:
        by_page.setdefault(unit.page, []).append(unit)
    windows, current, cost = [], [], 0
    for card in page_cards(units):
        size = tokens(canonical(card))
        if current and (cost + size > maximum or len(current) >= 64):
            pages = [c["page"] for c in current]
            windows.append(([u for p in pages for u in by_page[p]], current))
            current, cost = [], 0
        current.append(card)
        cost += size
    if current:
        pages = [c["page"] for c in current]
        windows.append(([u for p in pages for u in by_page[p]], current))
    return windows


def coalesce_topics(boundaries, units):
    """Only an explicit same-topic declaration permits adjacent range coalescing."""
    positions = {u.unit_id: i for i, u in enumerate(units)}
    sections = []
    for index, boundary in enumerate(boundaries.sections):
        end = positions[boundaries.sections[index + 1].start_unit] if index + 1 < len(boundaries.sections) else len(units)
        if sections and boundary.disposition == sections[-1].disposition == "CONTENT" and boundary.topic_key and sections[-1].topic_key == boundary.topic_key:
            start = positions[sections[-1].start_unit]
            if sum(tokens(u.text) for u in units[start:end]) <= NATIVE_BUDGET:
                previous = sections[-1]
                proofs = {canonical(p.model_dump()): p for p in [*previous.teaching_support, *boundary.teaching_support]}
                sections[-1] = previous.model_copy(update={
                    "teaching_support": list(proofs.values()),
                    "support_state": "TEXT" if proofs else "PENDING_VISION" if "PENDING_VISION" in {previous.support_state, boundary.support_state} else "TEXT",
                })
                continue
        sections.append(boundary)
    return Boundaries(sections=sections)


def granularity_report(plans, units):
    by_id = {u.unit_id: u for u in units}
    ranges = [{"key": p["key"], "title": p["title"], "pages": p["pages"],
               "native_tokens": sum(tokens(by_id[i].text) for i in p["unit_ids"]),
               "topic_key": p.get("topic_key", ""), "reason": p.get("split_reason", ""),
               "split_origin": p.get("split_origin", "SOURCE_STRUCTURE")} for p in plans]
    counts = [len(p["pages"]) for p in plans]
    repeated = Counter(re.sub(r"\s+", " ", p["title"]).casefold() for p in plans)
    return {"policy": PLANNING_POLICY, "planned_atomics": len(plans),
            "single_page_count": sum(n == 1 for n in counts),
            "single_page_ratio": round(sum(n == 1 for n in counts) / max(1, len(counts)), 3),
            "median_pages": median(counts) if counts else 0,
            "small_single_page_ranges": [p["key"] for p in ranges if len(p["pages"]) == 1 and p["native_tokens"] < 250],
            "duplicate_titles": {k: v for k, v in repeated.items() if v > 1}, "ranges": ranges}
