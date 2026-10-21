"""The single fan-out table used by every next-v1 worker."""

from .repository import identifier, one, rows


def published(connection, event):
    note = one(connection, "SELECT kind FROM notes WHERE id=:id", id=event["entity_id"])
    if not note or note["kind"] == "MOC":
        return []
    return ["ANALYZE_ATOMIC", "INDEX_NOTE"] if note["kind"] == "ATOMIC" else ["INDEX_NOTE"]


def added(connection, event):
    source = one(connection, "SELECT kind FROM sources WHERE id=:id", id=event["entity_id"])
    return ["GENERATE_ATOMICS"] if source and source["kind"] == "PDF" else []


def replaced(connection, event):
    return added(connection, event) + ["RECONCILE_DEPENDENCIES", "INDEX_NOTE"]


ROUTES = {
    "SOURCE_ADDED": added,
    "SOURCE_REPLACED": replaced,
    "NOTE_PUBLISHED": published,
    "NOTE_RESTORED": ["RECONCILE_DEPENDENCIES", "INDEX_NOTE", "ANALYZE_ATOMIC"],
    "CONCEPT_REBUILD_REQUESTED": ["BUILD_CONCEPT"],
    "INDEX_BUILD_REQUESTED": ["BUILD_INDEX"],
    "CONCEPT_CONFLICT_DETECTED": ["INDEX_NOTE"],
    "CONTRIBUTION_RESOLVED": ["RECONCILE_DEPENDENCIES", "INDEX_NOTE"],
    **{
        event: ["RECONCILE_DEPENDENCIES", "INDEX_NOTE"]
        for event in (
            "SOURCE_DELETED",
            "SOURCE_RESTORED",
            "NOTE_INVALIDATED",
            "NOTE_WITHDRAWN",
            "POLICY_CHANGED",
        )
    },
}


def dispatch(store):
    return store.jobs.dispatch(ROUTES)


def bootstrap(store, note_ids=None):
    with store.repository.transaction() as c:
        notes = rows(
            c,
            "SELECT id,current_revision_id FROM notes WHERE kind IN ('ATOMIC','CONCEPT') AND visibility='ACTIVE' AND current_revision_id IS NOT NULL",
        )
        count = 0
        for note in notes:
            if note_ids and note["id"] not in note_ids:
                continue
            store.repository.event(
                c,
                "NOTE_PUBLISHED",
                note["id"],
                note["current_revision_id"],
                {"bootstrap": True},
                key=f"bootstrap-p3p4-v1:{note['id']}:{note['current_revision_id']}",
            )
            count += 1
        return count


def request_reconcile(store):
    with store.repository.transaction() as c:
        store.repository.event(
            c, "POLICY_CHANGED", "maintenance", None, {}, key=f"reconcile:{identifier()}"
        )
