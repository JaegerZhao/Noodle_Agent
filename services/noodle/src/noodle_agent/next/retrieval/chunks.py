"""Chunks are ranges in immutable note Markdown, not a second content store."""

import re

from ..contracts import canonical, sha
from ..repository import rows
from ..segmentation import ranges, tokens

CHUNK_POLICY = "semantic-cl100k-v1"


def compile_chunks(store, note):
    result = []
    body = note["markdown"]
    revision = note["revision"]["id"]
    with store.db.engine.connect() as c:
        evidence = [
            dict(e)
            for e in rows(
                c,
                "SELECT se.section_id,e.id evidence_id,e.source_revision_id,e.page_number,sr.source_id,sr.sha256 source_sha256 FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id JOIN source_revisions sr ON sr.id=e.source_revision_id WHERE se.revision_id=:r",
                r=revision,
            )
        ]
    for section in note["sections"]:
        start, end = section["start_offset"], section["end_offset"]
        for a, b in ranges(body[start:end]):
            value = body[start + a : start + b]
            refs = [e for e in evidence if e["section_id"] == section["section_id"]]
            identity = {
                "revision_id": revision,
                "section_id": section["section_id"],
                "start_offset": start + a,
                "end_offset": start + b,
                "sha256": sha(value),
            }
            result.append(
                {
                    **identity,
                    "id": "chunk_" + sha(canonical({**identity, "policy": CHUNK_POLICY}))[:32],
                    "note_id": note["id"],
                    "kind": note["kind"],
                    "title": note["title"],
                    "section_title": section["title"],
                    "position": len(result),
                    "tokens": tokens(value),
                    "evidence": refs,
                    "assets": re.findall(r"!\[[^\]]*\]\(([^)]+)\)", value),
                    "text": value,
                    "embedding_text": note["title"] + " / " + section["title"] + "\n" + value,
                }
            )
    return result
