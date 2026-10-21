"""Compute desired inputs from current accepted contributions, never old prose."""

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..repository import one, rows

POLICY = "concept-decisions-v1"


def desired(connection, concept_id):
    valid = {r["revision_id"] for r in eligible(connection)}
    return [
        dict(r)
        for r in rows(
            connection,
            "SELECT * FROM contribution_decisions WHERE concept_id=:id AND status='ACCEPTED' ORDER BY id",
            id=concept_id,
        )
        if r["atomic_revision_id"] in valid
    ]


def signature(items):
    return sha(
        canonical(
            [
                {
                    k: r[k]
                    for k in (
                        "id",
                        "atomic_revision_id",
                        "section_id",
                        "action",
                        "fragment_json",
                        "decision_json",
                    )
                }
                for r in items
            ]
        )
    )


def schedule(store, connection, concept_id, cause=None):
    inputs = desired(connection, concept_id)
    digest = signature(inputs)
    state = one(connection, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=concept_id)
    if state and state["desired_input_hash"] == digest:
        return state["desired_generation"]
    generation = state["desired_generation"] + 1 if state else 1
    connection.execute(
        text("""INSERT INTO concept_work_state(concept_id,desired_generation,desired_input_hash,status)
       VALUES(:id,:g,:h,'PENDING') ON CONFLICT(concept_id) DO UPDATE SET desired_generation=:g,desired_input_hash=:h,status='PENDING'"""),
        {"id": concept_id, "g": generation, "h": digest},
    )
    store.repository.event(
        connection,
        "CONCEPT_REBUILD_REQUESTED",
        concept_id,
        None,
        {"generation": generation, "input_sha256": digest, "cause": cause},
        key=f"concept-build:{concept_id}:{generation}",
    )
    return generation


def reconcile(store, cause=None):
    with store.repository.transaction() as c:
        ids = {
            r["concept_id"]
            for r in rows(
                c,
                "SELECT DISTINCT concept_id FROM contribution_decisions WHERE concept_id IS NOT NULL",
            )
        }
        ids.update(r["concept_id"] for r in rows(c, "SELECT concept_id FROM concept_work_state"))
        for identity in ids:
            schedule(store, c, identity, cause)
    return {"reconciled": len(ids)}
