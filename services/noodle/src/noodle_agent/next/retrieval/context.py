"""Small model-facing views; complete provenance stays in the query artifact."""

from ..segmentation import ranges, tokens


def answer_knowledge(passages):
    return [
        {
            **{k: p[k] for k in ("citation_id", "title", "section_title", "body")},
            # These are PDF page positions, not additional factual evidence.
            # Retain them when compacting; otherwise the model denies available pages.
            "source_pages": sorted({
                item["page"] for item in p.get("provenance", [])
                if isinstance(item.get("page"), int) and not isinstance(item["page"], bool)
            }),
        }
        for p in passages
    ]


def ranking_passages(passages):
    return [
        {
            "chunk_id": str(i),
            "title": p.get("title", ""),
            "section_title": p.get("section_title", ""),
            "body": p["body"],
        }
        for i, p in enumerate(passages)
    ]


def ranking_document(passage):
    return (
        passage.get("title", "") + " / " + passage.get("section_title", "") + "\n" + passage["body"]
    )


def expanded_range(markdown, section, chunk, limit=800):
    start, end = section["start_offset"], section["end_offset"]
    text = markdown[start:end]
    if tokens(text) <= limit:
        return start, end, "COMPLETE_SECTION"
    a, b = chunk["start_offset"], chunk["end_offset"]
    # Atomic blocks preserve equations/code. Expand only inside this Section/Revision.
    blocks = [(start + x, start + y) for x, y in ranges(text, target=1)]
    neighbors = [
        (max(a - y, x - b, 0), 0 if y <= a else 1, x, y) for x, y in blocks if y <= a or x >= b
    ]
    for _, _, x, y in sorted(neighbors):
        left, right = min(a, x), max(b, y)
        if tokens(markdown[left:right]) <= limit:
            a, b = left, right
    reason = (
        "ADJACENT_BLOCKS"
        if (a, b) != (chunk["start_offset"], chunk["end_offset"])
        else "MATCH_ONLY"
    )
    return a, b, reason
