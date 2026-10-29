"""Source-grounded, bounded patch scopes shared by chapter generation and repair."""

from .contracts import canonical, sha
from .planning import QualityFailure


def repair_packet(record, findings):
    draft = record["draft"]
    requested = {i.get("section_key") for i in findings if i.get("section_key")}
    known = {s["key"] for s in draft["sections"]}
    if not requested <= known:
        raise QualityFailure("Repair finding references an unknown section")
    whole = any(not i.get("section_key") for i in findings)
    selected = known if whole else requested
    units = {u for u, section in draft["assignments"].items() if section in selected}
    units.update(u for finding in findings for u in finding.get("source_units", []))
    units.update(draft["omissions"])
    available = {u["unit_id"] for u in record["inputs"]["units"]}
    if not units <= available:
        raise QualityFailure("Repair finding references an unknown source unit")
    source = [u for u in record["inputs"]["units"] if u["unit_id"] in units]
    pages = {u["page"] for u in source}
    return {
        "expected_draft_sha256": sha(canonical(draft)),
        "repair_invariant": "Replace the erroneous assertion at its original location; do not retain it and append a contradictory correction. Explicitly label any source quotation that remains unreliable.",
        "draft": {**draft, "sections": [s for s in draft["sections"] if s["key"] in selected]},
        "source_units": source,
        "issues": findings,
        "page_contexts": [c for c in record.get("contexts", []) if c["page"] in pages],
        "allowed_section_keys": sorted(selected),
        "existing_section_keys": sorted(known),
    }


def check_patch_scope(packet, patch):
    keys = [s.key for s in patch.sections]
    if len(keys) != len(set(keys)):
        raise QualityFailure("Repair repeats a section")
    allowed = set(packet["allowed_section_keys"])
    existing = set(packet["existing_section_keys"])
    if any(k in existing and k not in allowed for k in keys):
        raise QualityFailure("Repair changed an unrelated section")
    if any(packet["draft"]["assignments"].get(u) not in allowed for u in patch.omitted_assignments):
        raise QualityFailure("Repair omitted source support from an unrelated section")
    if set(patch.restored_assignments) - set(packet["draft"]["omissions"]):
        raise QualityFailure("Repair restored an unknown omitted unit")


def check_progress(before, after):
    if sha(canonical(before)) == sha(canonical(after)):
        raise QualityFailure(
            "REPAIR_NO_PROGRESS: unchanged candidate; stop repeated semantic repair"
        )
