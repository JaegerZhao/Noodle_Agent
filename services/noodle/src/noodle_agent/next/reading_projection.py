"""Source-unit anchors for reading; independent of Atomic batch ownership."""

import json
from collections import Counter

from .document_outline import unit_node_map
from .repository import rows


def anchor_context(outline, units):
    # A PDF bookmark gives the page where the next heading begins, not the
    # position within that page. Keep preceding text with the preceding heading
    # until the new heading is encountered. Frozen planning ownership is untouched.
    owners = unit_node_map(outline, units, reading_boundary=True)
    blocks = {}
    for u in units:
        if u.kind not in {'HEADER', 'FOOTER', 'PAGE_NUMBER', 'PAGE_FOOTNOTE'}:
            blocks.setdefault(u.block_id, []).append(u)
    return owners, blocks


def section_anchors(store, revision, outline, units, *, context=None):
    owners, blocks = context if context is not None else anchor_context(outline, units)
    result = []
    with store.db.engine.connect() as c:
        evidence = rows(c, "SELECT s.section_id,s.position,a.page_number,a.locator_json,a.quoted_text FROM sections s JOIN section_evidence e ON e.revision_id=s.revision_id AND e.section_id=s.section_id JOIN evidence_anchors a ON a.id=e.evidence_id WHERE s.revision_id=:r ORDER BY s.position", r=revision)
    grouped = {}
    for row in evidence:
        grouped.setdefault((row['position'], row['section_id']), []).append(row)
    for (_, identity), items in grouped.items():
        weights = Counter()
        pages = sorted({v['page_number'] for v in items if v['page_number']})
        for item in items:
            locator = json.loads(item['locator_json'])
            for unit in blocks.get(locator.get('block_id'), []):
                owner = owners.get(unit.unit_id)
                if owner:
                    weights[owner] += len(unit.text)
        result.append({'section_id': identity, 'node_id': weights.most_common(1)[0][0] if weights else None,
                       'context_node_ids': list(weights), 'pages': pages})
    return result
