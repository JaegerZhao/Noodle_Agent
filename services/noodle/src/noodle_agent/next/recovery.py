"""Operator cancellation of an explicitly requested atomic-generation rebuild."""

import json

from sqlalchemy import text

from .contracts import Candidate, Review, canonical, sha
from .repository import Conflict, identifier, one


def cancel_rebuild(store, job_id: str, *, reason: str):
    if not reason.strip():
        raise ValueError("Cancellation reason is required")
    with store.repository.transaction() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:id", id=job_id)
        if (
            not job
            or job["kind"] != "GENERATE_ATOMICS"
            or job["status"] not in {"PENDING", "RUNNING", "RETRY", "REVIEW"}
        ):
            raise Conflict("No unfinished generation rebuild to cancel")
        baseline = json.loads(job["result_json"])
        pending = baseline.pop("pending_rebuild", None)
        if not pending or pending["baseline_sha256"] != sha(canonical(baseline)):
            raise Conflict("An explicit full-rebuild cycle and intact baseline are required")
        atomics = baseline.get("notes", [])
        outputs = atomics + [baseline["source_map"]] if atomics and "source_map" in baseline else []
        if not outputs:
            raise Conflict("A completed pre-rebuild result is required")
        discarded = []
        for output in outputs:
            note = one(c, "SELECT * FROM notes WHERE id=:id", id=output["note_id"])
            old_id = output["revision_id"]
            proof = one(
                c,
                "SELECT * FROM build_artifacts WHERE job_id=:job AND storage_ref=:ref",
                job=job_id,
                ref=f"artifacts/builds/{old_id}.json",
            )
            invalidations = (
                c.execute(
                    text(
                        "SELECT payload_json FROM events WHERE event_type='NOTE_INVALIDATED' AND revision_id=:id"
                    ),
                    {"id": old_id},
                )
                .scalars()
                .all()
            )
            if (
                not note
                or note["kind"] not in {"ATOMIC", "MOC"}
                or note["visibility"] != "ACTIVE"
                or not proof
                or (
                    note["kind"] == "ATOMIC"
                    and not any(
                        json.loads(p).get("job_id") == job_id
                        and json.loads(p).get("rebuild_id") == pending["request_id"]
                        for p in invalidations
                    )
                )
            ):
                raise Conflict("Pre-rebuild output ownership or withdrawal proof is missing")
            build = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
            candidate = Candidate.model_validate(build["candidate"])
            review = Review.model_validate(build["review"])
            if (
                candidate.note_id != note["id"]
                or not review.passed
                or review.candidate_sha256 != candidate.fingerprint
            ):
                raise Conflict("Pre-rebuild candidate approval does not match")
            old = one(
                c,
                "SELECT * FROM note_revisions WHERE id=:id AND note_id=:note",
                id=old_id,
                note=note["id"],
            )
            if not old or old["markdown_sha256"] != build["markdown_sha256"]:
                raise Conflict("Pre-rebuild Markdown receipt differs")
            store.read(old["markdown_ref"], old["markdown_sha256"])
            current = note["current_revision_id"]
            if current != old_id:
                current_proof = one(
                    c,
                    "SELECT id FROM build_artifacts WHERE job_id=:job AND storage_ref=:ref",
                    job=job_id,
                    ref=f"artifacts/builds/{current}.json",
                )
                if not current_proof:
                    raise Conflict(
                        "Another writer changed this note; cancellation cannot overwrite it"
                    )
                c.execute(
                    text("UPDATE note_revisions SET status='WITHDRAWN' WHERE id=:id"),
                    {"id": current},
                )
                discarded.append(current)
            c.execute(
                text("UPDATE note_revisions SET status='PUBLISHED' WHERE id=:id AND note_id=:note"),
                {"id": old_id, "note": note["id"]},
            )
            c.execute(
                text("UPDATE notes SET current_revision_id=:revision,title=:title WHERE id=:id"),
                {"revision": old_id, "title": candidate.title, "id": note["id"]},
            )
            if not store.content.valid_revision(c, old_id):
                raise Conflict(
                    "Pre-rebuild source or policy changed; cannot restore invalid knowledge"
                )
            store.repository.event(
                c,
                "NOTE_RESTORED",
                note["id"],
                old_id,
                {"reason": reason, "job_id": job_id, "previous_head": current},
                key=f"rebuild-restore:{old_id}:{identifier()}",
            )
        # Restore the previously completed result, not a claim that the cancelled rebuild succeeded.
        cancellation = {
            "reason": reason,
            "restored_notes": len(atomics),
            "discarded_revisions": discarded,
        }
        restored = {**baseline, "rebuild_cancellation": cancellation}
        c.execute(
            text("""UPDATE jobs SET status='SUCCEEDED',result_json=:result,error_json='{}',
                          lease_owner=NULL,lease_until=NULL WHERE id=:id"""),
            {"id": job_id, "result": canonical(restored)},
        )
        unfinished = one(
            c,
            "SELECT id FROM jobs WHERE event_id=:event AND status!='SUCCEEDED' LIMIT 1",
            event=job["event_id"],
        )
        c.execute(
            text("UPDATE events SET status=:status WHERE id=:id"),
            {"id": job["event_id"], "status": "DISPATCHED" if unfinished else "COMPLETED"},
        )
        store.repository.advance_epoch(c)
        store.repository.event(
            c,
            "GENERATION_REBUILD_CANCELLED",
            job_id,
            None,
            {**cancellation, "baseline_sha256": sha(canonical(baseline))},
            key=f"cancel-rebuild:{job_id}:{identifier()}",
        )
        return cancellation
