"""Read-back acceptance of a completed generation job, independent of its worker."""

import json
import re
from collections import Counter

from .contracts import Candidate, Review, sha
from .math_check import validate_math
from .planning import QualityFailure
from .repository import one


def verify_generation(store, job_id: str) -> dict:
    with store.db.engine.connect() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:id", id=job_id)
        if not job or job["status"] != "SUCCEEDED":
            raise QualityFailure("Only completed generation jobs can pass read-back acceptance")
        result = json.loads(job["result_json"])
        row = one(
            c,
            "SELECT * FROM job_checkpoints WHERE job_id=:job AND stage='units' AND state='DONE'",
            job=job_id,
        )
        if not row:
            raise QualityFailure("Missing unit manifest")
        manifest = json.loads(store.read(row["artifact_ref"], row["artifact_sha256"]))
        parsed = one(c, "SELECT * FROM parse_runs WHERE id=:id", id=result["parse_id"])
    artifact = json.loads(store.read(parsed["artifact_ref"], parsed["artifact_sha256"]))
    units = {unit["unit_id"]: unit for unit in manifest["units"]}
    covered = []
    formula_count = 0
    arithmetic_count = 0
    notes = []
    for output in result["notes"] + [result["source_map"]]:
        note = store.get_note(output["note_id"])
        if note["revision"]["id"] != output["revision_id"]:
            raise QualityFailure("Job output is no longer the active revision")
        ref = f"artifacts/builds/{output['revision_id']}.json"
        with store.db.engine.connect() as c:
            build = one(
                c,
                "SELECT * FROM build_artifacts WHERE job_id=:job AND storage_ref=:ref",
                job=job_id,
                ref=ref,
            )
        if not build:
            raise QualityFailure("Missing fenced publication artifact")
        payload = json.loads(store.read(build["storage_ref"], build["sha256"]))
        candidate = Candidate.model_validate(payload["candidate"])
        review = Review.model_validate(payload["review"])
        if not review.passed or candidate.fingerprint != review.candidate_sha256:
            raise QualityFailure("Publication receipt is not bound to this candidate")
        math_report = validate_math(note["markdown"])
        formula_count += math_report["count"]
        arithmetic_count += math_report.get("arithmetic_checks", 0)
        if note["kind"] == "ATOMIC":
            selected = review.report["units"]
            if not set(selected) <= set(units):
                raise QualityFailure("Publication names unknown source units")
            covered.extend(selected)
        for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", note["markdown"]):
            if (
                not link.startswith("../assets/")
                or not store.path(link.removeprefix("../")).is_file()
            ):
                raise QualityFailure("Image is missing or escapes the exported asset root")
        notes.append(
            {
                "note_id": note["id"],
                "kind": note["kind"],
                "title": note["title"],
                "revision_id": note["revision"]["id"],
                "sha256": sha(note["markdown"]),
                "stable_path": note["stable_path"],
            }
        )
    excluded = [entry["unit_id"] for entry in result["coverage"]["exclusions"]]
    counts = Counter(covered + excluded)
    missing = set(units) - set(counts)
    declared_missing = set(result['coverage'].get('missing_unit_ids', []))
    partial = bool(result['coverage'].get('failed_plans'))
    if set(counts) - set(units) or any(value != 1 for value in counts.values()) or missing != declared_missing or missing and not partial:
        raise QualityFailure("Read-back found omitted or multiply assigned source units")
    # This is an engineering/content-format gate. Independent scientific accuracy
    # and readability assessment must be recorded separately, never inferred here.
    return {
        "job_id": job_id,
        "status": "ENGINEERING_READBACK_PARTIAL" if partial else "ENGINEERING_READBACK_PASSED",
        "incomplete_atomics": result['coverage'].get('failed_plans', []),
        "missing_source_units": sorted(missing),
        "pages": artifact["page_count"],
        "page_coverage": artifact.get("page_coverage"),
        "source_units": len(units),
        "included_units": len(covered),
        "excluded_units": len(excluded),
        "formula_count": formula_count,
        "literal_arithmetic_checks": arithmetic_count,
        "symbolic_derivations_verified": False,
        "notes": notes,
        "semantic_assessment": "LLM_REVIEW_PASSED_HUMAN_ACCEPTANCE_REQUIRED",
    }


def verify_current_generation(store, job_id):
    """Follow only checked system repair revisions; keep the original generation receipt."""
    from .navigation import frozen_candidate
    with store.db.engine.connect() as c:
        job = one(c, "SELECT result_json,status FROM jobs WHERE id=:id", id=job_id)
        if not job or job['status'] != 'SUCCEEDED':
            raise QualityFailure('Generation is not complete')
    result = json.loads(job['result_json'])
    repaired = []
    current = []
    for output in result['notes']:
        note = store.get_note(output['note_id'])
        revision = note['revision']['id']
        chain = []
        while revision != output['revision_id']:
            with store.db.engine.connect() as c:
                row = one(c,'SELECT parent_revision_id FROM note_revisions WHERE id=:id',id=revision)
                proof = one(c,'SELECT * FROM build_artifacts WHERE storage_ref=:r',r=f'artifacts/builds/{revision}.json')
            if not row or not row['parent_revision_id'] or not proof or len(chain)>=2:
                raise QualityFailure('Current head is outside the bounded automatic repair lineage')
            artifact = json.loads(store.read(proof['storage_ref'], proof['sha256']))
            candidate = Candidate.model_validate(artifact['candidate'])
            review = Review.model_validate(artifact['review'])
            report = review.report
            if not review.passed or candidate.fingerprint != review.candidate_sha256 or review.reviewer != 'next.atomic_quality_review' or report.get('parent_revision') != row['parent_revision_id']:
                raise QualityFailure('Repair publication is not bound to its candidate/parent')
            parent = frozen_candidate(store,row['parent_revision_id'])
            if candidate.title != parent.title or candidate.entity_hints != parent.entity_hints or [s.section_id for s in candidate.sections] != [s.section_id for s in parent.sections] or any(s.evidence_ids != p.evidence_ids for s,p in zip(candidate.sections,parent.sections,strict=True)):
                raise QualityFailure('Content repair changed identity, directory or evidence ownership')
            checked = report['independent_check']
            changed = {s.section_id for s,p in zip(candidate.sections,parent.sections,strict=True) if s.markdown != p.markdown}
            if not checked['passed'] or checked['issues'] or not changed <= set(checked['checked_section_ids']):
                raise QualityFailure('Repaired sections lack independent verification')
            chain.append(revision)
            revision = row['parent_revision_id']
        if chain:
            repaired.append({'note_id':note['id'],'title':note['title'],'current_revision':note['revision']['id'],'repair_revisions':chain})
        current.append({'note_id':note['id'],'revision_id':note['revision']['id'],'title':note['title']})
    incomplete = result.get('coverage', {}).get('failed_plans', [])
    return {'passed':not incomplete,'version_integrity_passed':True,'incomplete_atomics':incomplete,'current_atomic_count':len(current),'automatic_repair_count':sum(len(n['repair_revisions']) for n in repaired),'repaired_notes':repaired,'notes':current,'manual_candidate_edits':0,'scope':'Version and source bookkeeping; content check evidence remains in each publication receipt.'}
