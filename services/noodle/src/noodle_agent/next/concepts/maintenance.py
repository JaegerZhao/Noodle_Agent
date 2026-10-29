"""Explicit, fenced rerouting over checked Atomic heads; history stays immutable."""

import json

from sqlalchemy import text

from ..contracts import canonical, sha
from ..repository import Conflict, one, rows
from .coordinator import schedule


def request_replan(store, revision_ids, run_id, *, resolution_context=None):
    if not run_id or not revision_ids or len(revision_ids) > 40:
        raise ValueError('A replan requires a named run and 1–40 Atomic revisions')
    members, expectations = [], {}
    with store.repository.transaction() as c:
        previous = one(c, "SELECT payload_json FROM events WHERE event_type='CONCEPT_REPLAN_REQUESTED' AND json_extract(payload_json,'$.concept_maintenance.run_id')=:run", run=run_id)
        if previous:
            payload = json.loads(previous['payload_json'])
            if sorted(a['revision_id'] for a in payload['routing_batch']['atomics']) != sorted(set(revision_ids)):
                raise Conflict('Maintenance run ID already belongs to different inputs')
            if payload['concept_maintenance'].get('resolution_context') != resolution_context:
                raise Conflict('Maintenance resolution intent changed; use a new named run')
            return {'snapshot': payload['concept_maintenance']['snapshot'], 'atomic_count': len(set(revision_ids)), 'reused': True}
        for revision in dict.fromkeys(revision_ids):
            row = one(c, 'SELECT r.note_id,n.kind,n.current_revision_id FROM note_revisions r JOIN notes n ON n.id=r.note_id WHERE r.id=:r', r=revision)
            old = one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)
            if not row or row['kind'] != 'ATOMIC' or row['current_revision_id'] != revision or not store.content.valid_revision(c, revision):
                raise Conflict('Replan input is no longer a current Atomic')
            if not old:
                raise Conflict('Use ordinary analysis for a previously unrouted Atomic')
            members.append({'note_id': row['note_id'], 'revision_id': revision})
            expectations[revision] = old['plan_hash']
        if resolution_context:
            exclusions=set(resolution_context.get('excluded_candidate_ids', []))
            current_targets={r['concept_id'] for revision in revision_ids
                for r in rows(c,"SELECT concept_id FROM contribution_decisions WHERE atomic_revision_id=:r AND status='ACCEPTED' AND concept_id IS NOT NULL",r=revision)}
            if not exclusions <= current_targets:
                raise Conflict('Only current targets of the scoped inputs may be proposed for retirement')
        snapshot = sha(canonical([run_id, members, expectations, resolution_context]))
        value = {'run_id': run_id, 'snapshot': snapshot, 'expected_plans': expectations}
        if resolution_context:
            value['resolution_context']=resolution_context
        store.put(f'artifacts/concept-maintenance/{snapshot}.json', canonical({**value, 'previous_routes': [dict(r) for revision in revision_ids
            for r in rows(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)]}).encode())
        batch = {'atomics': members, 'source_revision_id': None, 'input_hash': snapshot}
        store.repository.event(c, 'CONCEPT_REPLAN_REQUESTED', members[0]['note_id'], members[0]['revision_id'],
            {'routing_batch': batch, 'concept_maintenance': value}, key='concept-replan:' + snapshot)
    return {'snapshot': snapshot, 'atomic_count': len(members)}


def retire_inputs(store, connection, maintenance, cards, plan):
    """Accept successful local decisions, with CAS against the frozen route."""
    targets = set()
    for card in cards:
        revision = card['revision']
        old = one(connection, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=revision)
        if old is None or old['plan_hash'] != maintenance['expected_plans'][revision]:
            raise Conflict('Accepted route changed since the maintenance plan')
        if card['id'] in plan._unresolved_atomics:
            continue
        previous = rows(connection, "SELECT * FROM contribution_decisions WHERE atomic_revision_id=:r AND status='ACCEPTED'", r=revision)
        targets.update(r['concept_id'] for r in previous if r['concept_id'])
        connection.execute(text("UPDATE contribution_decisions SET status='SKIPPED' WHERE atomic_revision_id=:r AND status='ACCEPTED'"), {'r': revision})
    return targets


def schedule_retired(store, connection, targets, cause):
    for target in sorted(targets):
        schedule(store, connection, target, cause)
