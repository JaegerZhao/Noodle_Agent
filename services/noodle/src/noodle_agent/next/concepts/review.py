"""Explicit human resolutions use CAS and retain the previous model decision."""

import json
from typing import Literal

from sqlalchemy import text

from ..contracts import Contract, Text, canonical, sha
from ..repository import Conflict, one
from .analysis import catalogue, validate
from .contracts import Decision
from .coordinator import schedule


class Resolution(Contract):
    expected_sha256: Text
    status: Literal["ACCEPTED", "SKIPPED"]
    reason: Text
    decision: Decision | None = None


def resolve(store, decision_id, value):
    with store.repository.transaction() as c:
        previous = one(c, "SELECT * FROM contribution_decisions WHERE id=:id", id=decision_id)
        if (
            not previous
            or previous["status"] != "REVIEW"
            or sha(previous["decision_json"]) != value.expected_sha256
        ):
            raise Conflict("Review decision changed")
        target = previous["concept_id"]
        section = previous["section_id"]
        action = previous["action"]
        decision = json.loads(previous["decision_json"])
        if value.status == "ACCEPTED":
            proposed = value.decision
            if not proposed or proposed.action not in {
                "ADD_EVIDENCE",
                "UPDATE_SECTION",
                "CREATE_SECTION",
                "LINK_ONLY",
            }:
                raise ValueError("Accepted human resolution must target an existing Concept")
            if not store.content.valid_revision(c, previous["atomic_revision_id"]):
                raise Conflict("Atomic support is no longer current")
            validate(proposed, catalogue(store, c))
            decision = {**proposed.model_dump(), "_requires_publication": True}
            target = proposed.concept_id
            section = proposed.section_id
            if proposed.action == "CREATE_SECTION":
                section = "section_" + sha(proposed.section_question)[:20]
            action = proposed.action
        data = canonical(
            {"previous": dict(previous), "resolution": value.model_dump(), "actor": "HUMAN"}
        ).encode()
        store.put(f"artifacts/reviews/{sha(data)}.json", data)
        c.execute(
            text(
                "UPDATE contribution_decisions SET status=:status,concept_id=:target,section_id=:section,action=:action,decision_json=:decision WHERE id=:id"
            ),
            {
                "status": value.status,
                "target": target,
                "section": section,
                "action": action,
                "decision": canonical(decision),
                "id": decision_id,
            },
        )
        for identity in {previous["concept_id"], target} - {None}:
            schedule(store, c, identity, "human-review")
        store.repository.advance_epoch(c)
        store.repository.event(
            c,
            "CONTRIBUTION_RESOLVED",
            decision_id,
            previous["atomic_revision_id"],
            {"resolution_ref": f"artifacts/reviews/{sha(data)}.json"},
            key=f"resolution:{decision_id}:{sha(data)}",
        )
    return {"decision_id": decision_id, "status": value.status}
