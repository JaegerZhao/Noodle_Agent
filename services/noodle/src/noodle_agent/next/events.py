"""The single fan-out table used by every next-v1 worker."""

import json

from .repository import identifier, one, rows


def published(connection, event):
    note = one(
        connection, "SELECT kind,creation_key FROM notes WHERE id=:id", id=event["entity_id"]
    )
    if not note or note["kind"] == "MOC":
        return []
    if note["kind"] != "ATOMIC":
        return ["INDEX_NOTE", "UPDATE_MAPS"]
    payload = json.loads(event["payload_json"])
    consumers = ["INDEX_NOTE", "UPDATE_MAPS"]
    if not payload.get("routing_batch") and not (note["creation_key"] or "").startswith("author:"):
        consumers.insert(0, "ANALYZE_ATOMIC")
    if payload.get("previous_revision_id"):
        consumers.insert(0, "RECONCILE_DEPENDENCIES")
    return consumers



def added(connection, event):
    source = one(connection, "SELECT kind FROM sources WHERE id=:id", id=event["entity_id"])
    return ["GENERATE_ATOMICS"] if source and source["kind"] == "PDF" else []


def replaced(connection, event):
    return added(connection, event) + ["RECONCILE_DEPENDENCIES", "INDEX_NOTE"]


def audit_only(connection, event):
    """A recognized audit event has no worker consumers; never swallow unknown events."""
    return []


def restored(connection, event):
    note = one(connection, 'SELECT kind FROM notes WHERE id=:id', id=event['entity_id'])
    return ['RECONCILE_DEPENDENCIES', 'INDEX_NOTE', 'UPDATE_MAPS'] + (
        ['ANALYZE_ATOMIC'] if note and note['kind'] == 'ATOMIC' else [])


ROUTES = {
    "JOB_RETRY_REQUESTED": audit_only,
    "SOURCE_ADDED": added,
    "SOURCE_REPLACED": replaced,
    'SOURCES_STATE_CHANGED': ['RECONCILE_DEPENDENCIES', 'INDEX_NOTE', 'UPDATE_MAPS'],
    "NOTE_PUBLISHED": published,
    "ATOMIC_BATCH_CLOSED": ["ANALYZE_ATOMIC"],
    'CONCEPT_REPLAN_REQUESTED': ['ANALYZE_ATOMIC'],
    'CONCEPT_STRUCTURE_MAINTENANCE_REQUESTED': ['MAINTAIN_CONCEPT_STRUCTURE'],
    'CONCEPT_MAINTENANCE_APPLY_REQUESTED': ['APPLY_CONCEPT_MAINTENANCE'],
    'CONCEPT_MAINTENANCE_ROLLED_BACK': audit_only,
    "NOTE_RESTORED": restored,
    "CONCEPT_REBUILD_REQUESTED": ["BUILD_CONCEPT"],
    "ATOMIC_REPAIR_REQUESTED": ["REPAIR_ATOMIC"],
    "ORGANIZATION_REVIEW_REQUESTED": ["REVIEW_ORGANIZATION"],
    "MOC_ENTRY_SUGGESTED": ["UPDATE_MAPS"],
    "INDEX_BUILD_REQUESTED": ["BUILD_INDEX"],
    "CONCEPT_CONFLICT_DETECTED": ["INDEX_NOTE"],
    "CONTRIBUTION_RESOLVED": ["RECONCILE_DEPENDENCIES", "INDEX_NOTE"],
    **{
        event: ["RECONCILE_DEPENDENCIES", "INDEX_NOTE", "UPDATE_MAPS"]
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
