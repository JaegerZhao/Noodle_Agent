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
    if set(counts) != set(units) or any(value != 1 for value in counts.values()):
        raise QualityFailure("Read-back found omitted or multiply assigned source units")
    # This is an engineering/content-format gate. Independent scientific accuracy
    # and readability assessment must be recorded separately, never inferred here.
    return {
        "job_id": job_id,
        "status": "ENGINEERING_READBACK_PASSED",
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
