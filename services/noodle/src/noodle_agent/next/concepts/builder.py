"""Compile Concept revisions with a generation fence inside publication."""

import json

from sqlalchemy import bindparam, text

from ..contracts import Candidate, FragmentRef, Input, Link, Review, Scope, Section, sha
from ..jobs import require_lease
from ..planning import QualityFailure
from ..repository import Conflict, one
from .coordinator import desired, signature
from .writing import write_chapter


def build(store, lease, calls):
    identity = lease.input["entity_id"]
    with store.db.engine.connect() as c:
        state = one(c, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=identity)
        note = one(c, "SELECT * FROM notes WHERE id=:id", id=identity)
        items = desired(c, identity)
    if not state or state["desired_generation"] != lease.input["payload"]["generation"]:
        return {"status": "SUPERSEDED"}
    generation = state["desired_generation"]
    digest = signature(items)
    base = note["current_revision_id"]
    if digest != state["desired_input_hash"]:
        raise Conflict("Effective inputs changed; reconcile required")
    if note["visibility"] != "ACTIVE":
        return {"status": "MANUALLY_HIDDEN"}
    old = None
    old_report = {}
    base_published = False
    if base:
        with store.db.engine.connect() as c:
            base_published = (
                one(c, "SELECT status FROM note_revisions WHERE id=:id", id=base)["status"]
                == "PUBLISHED"
            )
            proof = one(
                c,
                "SELECT * FROM build_artifacts WHERE storage_ref=:ref",
                ref=f"artifacts/builds/{base}.json",
            )
        payload = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
        old = Candidate.model_validate(payload["candidate"])
        old_report = payload["review"]["report"]

    def guard(c):
        require_lease(c, lease)
        latest = one(c, "SELECT * FROM concept_work_state WHERE concept_id=:id", id=identity)
        if latest["desired_generation"] != generation or signature(desired(c, identity)) != digest:
            raise Conflict("Concept generation or effective inputs changed")

    def accepted(c, revision):
        c.execute(
            text(
                "UPDATE concept_work_state SET completed_generation=:g,status='READY' WHERE concept_id=:id"
            ),
            {"g": generation, "id": identity},
        )
        c.execute(
            text(
                "UPDATE contribution_decisions SET applied_revision_id=:r WHERE concept_id=:id AND status='ACCEPTED' AND id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"r": revision, "id": identity, "ids": [i["id"] for i in items]},
        )
        store.repository.event(
            c,
            "MOC_ENTRY_SUGGESTED",
            identity,
            revision,
            {"title": note["title"]},
            key=f"map-suggestion:{revision}",
        )

    grouped = {}
    links = []  # Rebuild this derived projection from current LINK_ONLY inputs.
    for item in items:
        fragment = json.loads(item["fragment_json"])
        decision = json.loads(item["decision_json"])
        if item["action"] == "LINK_ONLY":
            with store.db.engine.connect() as c:
                target = one(
                    c,
                    "SELECT note_id FROM note_revisions WHERE id=:id",
                    id=item["atomic_revision_id"],
                )
            link = Link(
                target_note_id=target["note_id"],
                relation="LINKS_TO",
                status="CONFIRMED",
                basis=decision["reason"],
            )
            if link not in links:
                links.append(link)
        else:
            key = (
                item["section_id"]
                or "section_" + sha(decision.get("section_question") or fragment["section_id"])[:20]
            )
            grouped.setdefault(key, []).append((item, fragment, decision))
    if old:
        for section in old.sections:
            if section.protected and section.section_id not in grouped:
                raise QualityFailure("Unsupported protected chapter needs author review")
    if not grouped:
        with store.repository.transaction() as c:
            guard(c)
            if base:
                current = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)
                if current["current_revision_id"] != base:
                    raise Conflict("Concept head changed")
                c.execute(
                    text("UPDATE note_revisions SET status='WITHDRAWN' WHERE id=:id"), {"id": base}
                )
                store.repository.advance_epoch(c)
                store.repository.event(
                    c,
                    "NOTE_INVALIDATED",
                    identity,
                    base,
                    {"reason": "No current atomic support"},
                    key=f"unsupported:{identity}:{generation}",
                )
            c.execute(
                text(
                    "UPDATE concept_work_state SET completed_generation=:g,status='NO_SUPPORTED_INPUT' WHERE concept_id=:id"
                ),
                {"g": generation, "id": identity},
            )
        return {"status": "NO_SUPPORTED_INPUT"}
    old_sections = {s.section_id: s for s in old.sections} if old else {}
    order = [s.section_id for s in old.sections if s.section_id in grouped] if old else []
    order.extend(k for k in grouped if k not in order)
    sections = []
    used = {}
    checks = {}
    for key in order:
        entries = grouped[key]
        previous = old_sections.get(key)
        ids = [i["id"] for i, _, _ in entries]
        used[key] = ids
        prior = set(old_report.get("section_contributions", {}).get(key, []))
        same = previous is not None and prior == set(ids)
        pure = (
            previous is not None
            and prior
            and prior <= set(ids)
            and all(i["id"] in prior or i["action"] == "ADD_EVIDENCE" for i, _, _ in entries)
        )
        if pure:
            with store.db.engine.connect() as c:
                pure = all(
                    store.content.valid_revision(c, i.atomic_revision_id) for i in previous.inputs
                )
        title = (
            previous.title
            if previous
            else entries[0][2].get("section_title") or entries[0][1]["title"]
        )
        question = previous.question if previous else entries[0][2].get("section_question") or title
        scope = previous.scope if previous else Scope(includes=[question])
        if same or pure:
            markdown = previous.markdown
            checks[key] = {"writer_called": False}
        else:
            if previous and previous.protected:
                raise QualityFailure("Protected chapter cannot be automatically rewritten")
            markdown, checks[key] = write_chapter(
                calls,
                key,
                title,
                question,
                scope,
                [f for _, f, _ in entries],
                previous.markdown if previous else None,
            )
        contributions = []
        evidence = set()
        for item, f, d in entries:
            evidence.update(f["evidence_ids"])
            contributions.append(
                Input(
                    atomic_revision_id=item["atomic_revision_id"],
                    section_ids=[f["section_id"]],
                    fragments=[
                        FragmentRef(
                            section_id=f["section_id"],
                            start_offset=f["start_offset"],
                            end_offset=f["end_offset"],
                            sha256=f["sha256"],
                        )
                    ],
                    evidence_ids=f["evidence_ids"],
                    action=item["action"],
                    role=d["role"],
                )
            )
        sections.append(
            Section(
                section_id=key,
                title=title,
                question=question,
                scope=scope,
                markdown=markdown,
                evidence_ids=sorted(evidence),
                inputs=contributions,
                protected=previous.protected if previous else False,
            )
        )
    with store.db.engine.connect() as c:
        claim = one(
            c,
            "SELECT definition_json FROM concept_identity_claims WHERE concept_id=:id",
            id=identity,
        )
    definition = json.loads(claim["definition_json"]) if claim else {}
    candidate = Candidate(
        note_id=identity,
        title=note["title"],
        summary=old.summary if old else definition.get("question", ""),
        scope=old.scope if old else Scope.model_validate(definition["scope"]),
        sections=sections,
        links=links,
        policy_version=store.repository.policy_version,
    )
    if old and base_published and candidate.fingerprint == old.fingerprint:
        with store.repository.transaction() as c:
            guard(c)
            accepted(c, base)
        return {"status": "UNCHANGED", "revision_id": base}
    result = store.publish(
        candidate,
        expected_revision=base,
        review=Review(
            candidate_sha256=candidate.fingerprint,
            policy_version=candidate.policy_version,
            reviewer="next.concept_review",
            passed=True,
            report={
                "section_contributions": used,
                "checks": checks,
                "input_snapshot_sha256": digest,
                "generation": generation,
            },
        ),
        lease=lease,
        guard=guard,
        accepted=accepted,
    )
    return {"status": "PUBLISHED", **result}
