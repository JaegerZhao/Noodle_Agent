"""Section-level author changes with optimistic concurrency and explicit author evidence."""

import json

from sqlalchemy import text

from .contracts import Candidate, FragmentRef, Input, Review, sha
from .navigation import frozen_candidate
from .repository import Conflict, InvalidKnowledge, one, rows


def section_content(section):
    return section.model_dump(exclude={"evidence_ids", "inputs"})


def edit_section(store, note_id, *, expected_revision, section_id, markdown, title=None):
    if not markdown.strip():
        raise InvalidKnowledge("Section cannot be empty")
    base = store.get_note(note_id, revision_id=expected_revision, historical=True)
    current = store.get_note(note_id)
    candidate = frozen_candidate(store, current["revision"]["id"])
    if current["kind"] == "MOC" and candidate.map_policy != "MANUAL":
        raise InvalidKnowledge(
            "System directories are generated automatically; create a personal map to edit"
        )
    original = frozen_candidate(store, expected_revision)
    old = next((s for s in original.sections if s.section_id == section_id), None)
    target = next((s for s in candidate.sections if s.section_id == section_id), None)
    if not old or not target:
        raise Conflict("Section was removed or moved; reload the current revision")
    if base["revision"]["id"] != current["revision"]["id"] and section_content(
        target
    ) != section_content(old):
        if target.markdown == markdown and (not title or candidate.title == title):
            return {"note_id": note_id, "revision_id": current["revision"]["id"], "unchanged": True}
        raise Conflict("This section changed after editing started; compare before saving")
    if title and candidate.title != original.title and title != candidate.title:
        raise Conflict("Title changed after editing started")
    if target.markdown == markdown and (not title or title == candidate.title):
        return {"note_id": note_id, "revision_id": current["revision"]["id"], "unchanged": True}
    # Human revisions are authored statements; they must not reuse PDF page evidence as if generated.
    updated = target.model_copy(update={"markdown": markdown, "protected": True})
    sections = [updated if s.section_id == section_id else s for s in candidate.sections]
    snapshot = "\n\n".join(s.markdown for s in sections)
    assets = set(__import__("re").findall(r"!?\[[^\]]*\]\(([^)]+)\)", snapshot))
    allowed = {
        "../" + p.relative_to(store.root).as_posix()
        for p in store.path("assets").glob("*")
        if p.is_file()
    }
    if not assets <= allowed:
        raise InvalidKnowledge("Only stored local assets are allowed in edited knowledge")
    from .generation import Generation
    from .generation_contracts import Draft, DraftSection

    Generation.validate_draft(
        Draft(
            title=title or candidate.title,
            sections=[
                DraftSection(
                    key=s.section_id,
                    title=s.title,
                    question=s.question,
                    scope=s.scope,
                    markdown=s.markdown,
                    units=[s.section_id],
                )
                for s in sections
            ],
            summary=candidate.summary or candidate.title,
        ),
        {s.section_id for s in sections},
        allowed,
    )
    source = store.add_source(
        snapshot.encode(), f"作者修订：{title or candidate.title}", kind="AUTHOR"
    )
    if source.get("deleted"):
        raise InvalidKnowledge("Matching author snapshot was withdrawn")
    origins = []
    support_cleanup = None
    if current["kind"] == "ATOMIC":
        with store.db.engine.connect() as c:
            origins = [
                r["source_revision_id"]
                for r in rows(
                    c,
                    "SELECT DISTINCT e.source_revision_id FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id JOIN source_revisions sr ON sr.id=e.source_revision_id JOIN sources so ON so.id=sr.source_id WHERE se.revision_id=:r AND NOT EXISTS(SELECT 1 FROM author_origin_sources ao WHERE ao.revision_id=:r) UNION SELECT source_revision_id FROM author_origin_sources WHERE revision_id=:r",
                    r=current["revision"]["id"],
                )
            ]
        sections = [
            s.model_copy(
                update={
                    "evidence_ids": [store.add_evidence(source["revision_id"], s.markdown)],
                    "inputs": [],
                }
            )
            for s in sections
        ]
    elif current["kind"] == "CONCEPT":
        atomic_id = store.content.create_note(
            "ATOMIC",
            f"作者补充：{candidate.title} / {target.title}",
            domain=current["domain"],
            creation_key=f"author:{note_id}:{expected_revision}:{section_id}:{sha(markdown)}",
        )
        evidence = store.add_evidence(source["revision_id"], markdown)
        support = Candidate(
            note_id=atomic_id,
            title=f"作者补充：{candidate.title} / {target.title}",
            scope=target.scope,
            sections=[updated.model_copy(update={"evidence_ids": [evidence], "inputs": []})],
            policy_version=candidate.policy_version,
        )
        with store.db.engine.connect() as c:
            head = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=atomic_id)[
                "current_revision_id"
            ]
        if head is not None:
            with store.repository.transaction() as c:
                support_note = one(c, "SELECT visibility FROM notes WHERE id=:id", id=atomic_id)
                if support_note["visibility"] != "ACTIVE":
                    withdrawal = one(
                        c,
                        "SELECT payload_json FROM events WHERE entity_id=:id AND event_type='NOTE_WITHDRAWN' ORDER BY created_at DESC LIMIT 1",
                        id=atomic_id,
                    )
                    if (
                        not withdrawal
                        or json.loads(withdrawal["payload_json"]).get("reason")
                        != "AUTHOR_EDIT_ABORTED"
                    ):
                        raise Conflict("Author support was withdrawn separately")
                    c.execute(
                        text("UPDATE notes SET visibility='ACTIVE' WHERE id=:id"), {"id": atomic_id}
                    )
                    store.repository.advance_epoch(c)
        if head is None:
            pub = store.publish(
                support,
                expected_revision=None,
                review=Review(
                    candidate_sha256=support.fingerprint,
                    policy_version=support.policy_version,
                    reviewer="author.explicit",
                    passed=True,
                    report={"authored": True},
                ),
            )
            head = pub["revision_id"]
        support_cleanup = (atomic_id, head)
        support_note = store.get_note(atomic_id)
        bound = next(s for s in support_note["sections"] if s["section_id"] == section_id)
        contribution = Input(
            atomic_revision_id=head,
            fragments=[
                FragmentRef(
                    section_id=section_id,
                    start_offset=bound["start_offset"],
                    end_offset=bound["end_offset"],
                    sha256=bound["sha256"],
                )
            ],
            section_ids=[section_id],
            evidence_ids=[evidence],
            action="UPDATE_SECTION",
        )
        sections = [
            s.model_copy(
                update={
                    "inputs": [*s.inputs, contribution],
                    "evidence_ids": list(dict.fromkeys([*s.evidence_ids, evidence])),
                }
            )
            if s.section_id == section_id
            else s
            for s in sections
        ]
    elif current["kind"] != "MOC":
        raise InvalidKnowledge("Unsupported note kind")
    updated_candidate = candidate.model_copy(
        update={
            "sections": sections,
            "title": title or candidate.title,
            "origin_source_revisions": origins,
        }
    )

    def guard(c):
        if not store.content.valid_revision(c, current["revision"]["id"]):
            raise Conflict("Knowledge support changed during editing")

    try:
        return store.publish(
            updated_candidate,
            expected_revision=current["revision"]["id"],
            review=Review(
                candidate_sha256=updated_candidate.fingerprint,
                policy_version=candidate.policy_version,
                reviewer="author.explicit",
                passed=True,
                report={
                    "base_revision": expected_revision,
                    "authored": True,
                    "section_id": section_id,
                },
            ),
            allow_protected_edit=True,
            guard=guard,
        )
    except Exception:
        if support_cleanup:
            identity, revision = support_cleanup
            with store.repository.transaction() as c:
                support_note = one(
                    c, "SELECT current_revision_id,visibility FROM notes WHERE id=:id", id=identity
                )
                used = one(
                    c,
                    "SELECT ci.id FROM concept_inputs ci JOIN notes n ON n.current_revision_id=ci.concept_revision_id WHERE ci.atomic_revision_id=:r LIMIT 1",
                    r=revision,
                )
                if (
                    support_note
                    and support_note["current_revision_id"] == revision
                    and support_note["visibility"] == "ACTIVE"
                    and not used
                ):
                    c.execute(
                        text("UPDATE notes SET visibility='WITHDRAWN' WHERE id=:id"),
                        {"id": identity},
                    )
                    store.repository.advance_epoch(c)
                    from .repository import identifier

                    store.repository.event(
                        c,
                        "NOTE_WITHDRAWN",
                        identity,
                        revision,
                        {"reason": "AUTHOR_EDIT_ABORTED"},
                        key="author-abort:" + identifier(),
                    )
        raise
