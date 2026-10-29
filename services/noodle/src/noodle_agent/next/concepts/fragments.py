"""Frozen contribution ranges retain section context and survive partial retries."""

import json

from ..contracts import canonical, sha
from ..planning import QualityFailure
from ..repository import rows
from ..segmentation import ranges


def fragments(store, note):
    ref = "artifacts/concept-manifests/" + note["revision"]["id"] + ".json"
    if store.path(ref).exists():
        frozen = json.loads(store.path(ref).read_text(encoding="utf-8"))
        if frozen["markdown_sha256"] != note["revision"]["markdown_sha256"]:
            raise QualityFailure("Frozen contribution manifest differs from its Revision")
        return frozen["fragments"]
    with store.db.engine.connect() as c:
        evidence = rows(
            c,
            "SELECT section_id,evidence_id FROM section_evidence WHERE revision_id=:r",
            r=note["revision"]["id"],
        )
        existing = {
            r["fragment_id"]
            for r in rows(
                c,
                "SELECT fragment_id FROM contribution_decisions WHERE atomic_revision_id=:r",
                r=note["revision"]["id"],
            )
        }

    def build(target):
        result = []
        for section in note["sections"]:
            start, end = section["start_offset"], section["end_offset"]
            for a, b in ranges(note["markdown"][start:end], target=target):
                text = note["markdown"][start + a : start + b]
                position = {
                    "atomic_revision_id": note["revision"]["id"],
                    "section_id": section["section_id"],
                    "start_offset": start + a,
                    "end_offset": start + b,
                    "sha256": sha(text),
                }
                result.append(
                    {
                        **position,
                        "fragment_id": "fragment_" + sha(canonical(position))[:24],
                        "text": text,
                        "title": note["title"],
                        "section_title": section["title"],
                        "section_question": section.get("question", section["title"]),
                        "scope": json.loads(section.get("scope_json", "{}")),
                        "domain": note["domain"],
                        "evidence_ids": [
                            e["evidence_id"]
                            for e in evidence
                            if e["section_id"] == section["section_id"]
                        ],
                    }
                )
        return result

    result = build(1000)
    if existing and not existing <= {f["fragment_id"] for f in result}:
        result = build(600)
        if not existing <= {f["fragment_id"] for f in result}:
            raise QualityFailure(
                "Existing contribution ranges disagree; do not resplit or overlap accepted evidence"
            )
    store.put(
        ref,
        canonical(
            {"markdown_sha256": note["revision"]["markdown_sha256"], "fragments": result}
        ).encode("utf-8"),
    )
    return result
