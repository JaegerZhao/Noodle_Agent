from ..contracts import canonical, sha
from ..repository import rows
from ..segmentation import ranges


def fragments(store, note):
    result = []
    with store.db.engine.connect() as c:
        evidence = rows(
            c,
            "SELECT section_id,evidence_id FROM section_evidence WHERE revision_id=:r",
            r=note["revision"]["id"],
        )
    for section in note["sections"]:
        start, end = section["start_offset"], section["end_offset"]
        for a, b in ranges(note["markdown"][start:end], target=600):
            text = note["markdown"][start + a : start + b]
            ref = {
                "atomic_revision_id": note["revision"]["id"],
                "section_id": section["section_id"],
                "start_offset": start + a,
                "end_offset": start + b,
                "sha256": sha(text),
            }
            result.append(
                {
                    **ref,
                    "fragment_id": "fragment_" + sha(canonical(ref))[:24],
                    "text": text,
                    "title": note["title"],
                    "domain": note["domain"],
                    "evidence_ids": [
                        e["evidence_id"]
                        for e in evidence
                        if e["section_id"] == section["section_id"]
                    ],
                }
            )
    return result
