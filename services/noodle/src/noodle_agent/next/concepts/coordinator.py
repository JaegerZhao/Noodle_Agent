"""Compute desired inputs from current accepted contributions, never old prose."""

import json

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..repository import Conflict, one, rows

POLICY = "concept-decisions-v1"


def build_ready(connection, job, store=None):
    """Only known producers for this Concept can delay its current generation."""
    data = json.loads(job["input_json"])
    target = data["entity_id"]
    state = one(connection, "SELECT desired_generation FROM concept_work_state WHERE concept_id=:id", id=target)
    if not state or data["payload"]["generation"] != state["desired_generation"]:
        return True
    pending = one(connection, """SELECT 1 FROM jobs j WHERE j.kind='ANALYZE_ATOMIC'
        AND j.status IN ('PENDING','RUNNING','RETRY') AND (
            EXISTS(SELECT 1 FROM atomic_entity_routes ar JOIN note_revisions r ON r.id=ar.atomic_revision_id
                   WHERE r.note_id=json_extract(j.input_json,'$.entity_id') AND ar.concept_id=:target)
            OR EXISTS(SELECT 1 FROM json_each(j.input_json,'$.payload.routing_batch.atomics') a
                      JOIN atomic_entity_routes ar ON ar.atomic_revision_id=json_extract(a.value,'$.revision_id')
                      WHERE ar.concept_id=:target)) LIMIT 1""", target=target)
    return not pending and (store is None or cohort_ready(store, connection, job, target))


def cohort_ready(store, connection, job, target):
    """Coalesce known same-chapter producers, with a 180s freshness ceiling."""
    from datetime import UTC, datetime

    from ..cost_policy import enabled
    from ..pipeline_state import artifact

    if not enabled():
        return True
    created = datetime.fromisoformat(job['created_at'])
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if (datetime.now(UTC) - created).total_seconds() >= 180:
        return True
    claim = one(connection, 'SELECT identity_key FROM concept_identity_claims WHERE concept_id=:id ORDER BY identity_key LIMIT 1', id=target)
    if not claim:
        return True
    producers = rows(connection, """SELECT j.input_json,ctl.chapter_id,k.artifact_ref,k.artifact_sha256
        FROM jobs j JOIN job_controls ctl ON ctl.job_id=j.id
        JOIN job_checkpoints k ON k.job_id=j.id
        WHERE j.kind='GENERATE_ATOMICS' AND j.status IN ('RUNNING','PENDING','RETRY')
        AND ctl.state='RUN' AND k.stage LIKE 'source.sections.plan%' AND k.state='DONE'""")
    for producer in producers:
        plan = artifact(str(store.root), producer['artifact_ref'], producer['artifact_sha256'])
        members = [p for p in plan.get('plans', []) if p.get('chapter_key') == producer['chapter_id']
                   and (p.get('concept_outline') or {}).get('concept_key') == claim['identity_key']]
        source = json.loads(producer['input_json']).get('revision_id')
        if any(not one(connection, 'SELECT 1 FROM notes WHERE creation_key=:k AND current_revision_id IS NOT NULL',
                       k=f"atomic:{source}:source-section:{p['key']}") for p in members):
            return False
    return True


def desired(connection, concept_id):
    valid = {r["revision_id"] for r in eligible(connection)}
    return [
        dict(r)
        for r in rows(
            connection,
            "SELECT * FROM contribution_decisions WHERE concept_id=:id AND status='ACCEPTED' ORDER BY created_at,id",
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


def source_affected_concepts(connection, cause):
    """Use historical provenance too: deletion must retract old contributions."""
    job = one(connection, 'SELECT input_json FROM jobs WHERE id=:id', id=cause) if cause else None
    if not job:
        return None
    data = json.loads(job['input_json'])
    sources = set(data.get('payload', {}).get('source_ids') or [])
    if one(connection, 'SELECT 1 FROM sources WHERE id=:id', id=data.get('entity_id')):
        sources.add(data['entity_id'])
    if not sources:
        return None
    return {r['concept_id'] for r in rows(connection, '''
        WITH affected AS (
            SELECT DISTINCT se.revision_id FROM source_revisions sr
            JOIN evidence_anchors e ON e.source_revision_id=sr.id
            JOIN section_evidence se ON se.evidence_id=e.id
            WHERE sr.source_id IN (SELECT value FROM json_each(:sources))
        )
        SELECT d.concept_id FROM contribution_decisions d JOIN affected a ON a.revision_id=d.atomic_revision_id
        WHERE d.concept_id IS NOT NULL
        UNION SELECT r.note_id AS concept_id FROM concept_inputs i
            JOIN affected a ON a.revision_id=i.atomic_revision_id
            JOIN note_revisions r ON r.id=i.concept_revision_id
        UNION SELECT r.concept_id FROM atomic_entity_routes r
            JOIN affected a ON a.revision_id=r.atomic_revision_id WHERE r.concept_id IS NOT NULL
        ''', sources=canonical(sorted(sources)))}


def reconcile(store, cause=None):
    # Source validity is fenced at reads immediately. Scheduling every Concept
    # does not need one global write lock, which can stall a new PDF import.
    with store.db.engine.connect() as c:
        ids = {
            r["concept_id"]
            for r in rows(
                c,
                "SELECT DISTINCT concept_id FROM contribution_decisions WHERE concept_id IS NOT NULL",
            )
        }
        ids.update(r["concept_id"] for r in rows(c, "SELECT concept_id FROM concept_work_state"))
        affected = source_affected_concepts(c, cause)
        if affected is not None:
            ids.intersection_update(affected)
    for identity in sorted(ids):
        with store.repository.transaction() as c:
            schedule(store, c, identity, cause)
    return {"reconciled": len(ids), "scope": "SOURCE" if affected is not None else "LIBRARY"}


def request_media(store, concept_id, expected_revision):
    """Refresh registered figures without rerouting or requesting text rewriting."""
    key = 'concept-media:' + concept_id + ':' + expected_revision
    with store.repository.transaction() as c:
        event = one(c, 'SELECT id FROM events WHERE dedupe_key=:k', k=key)
        if event:
            return {'event_id': event['id'], 'reused': True}
        note = one(c, "SELECT * FROM notes WHERE id=:id AND kind='CONCEPT'", id=concept_id)
        state = one(c, 'SELECT * FROM concept_work_state WHERE concept_id=:id', id=concept_id)
        if not note or note['current_revision_id'] != expected_revision or not state or state['status'] != 'READY':
            raise Conflict('Media update requires a ready current Concept')
        if one(c, 'SELECT 1 FROM sections WHERE revision_id=:r AND protected=1', r=expected_revision):
            raise Conflict('Protected sections require author-directed media placement')
        items = desired(c, concept_id)
        digest = signature(items)
        if not items or digest != state['desired_input_hash']:
            raise Conflict('Media update requires current effective inputs')
        generation = state['desired_generation'] + 1
        c.execute(text("UPDATE concept_work_state SET desired_generation=:g,status='PENDING' WHERE concept_id=:id"),
                  {'g': generation, 'id': concept_id})
        store.repository.event(c, 'CONCEPT_REBUILD_REQUESTED', concept_id, None,
            {'generation': generation, 'input_sha256': digest, 'media_refresh': True,
             'expected_revision': expected_revision}, key=key)
        return {'event_id': one(c, 'SELECT id FROM events WHERE dedupe_key=:k', k=key)['id'],
                'generation': generation, 'reused': False}


def request_repair(store, concept_id, expected_revision, findings):
    """Explicit source-grounded correction, preserving identity and old revisions."""
    if not findings:
        raise ValueError('At least one located finding is required')
    note = store.get_note(concept_id)
    if note['kind'] != 'CONCEPT' or note['revision']['id'] != expected_revision:
        raise Conflict('Concept repair must address the current revision')
    sections = {s['section_id']:s for s in note['sections']}
    normalized = []
    for finding in findings:
        section = sections.get(finding.get('section_id'))
        quote, issue = finding.get('candidate_quote', ''), finding.get('issue', '')
        if not section or section['protected'] or not quote or not issue.strip():
            raise ValueError('Repair needs an unprotected section, exact quote and issue')
        if quote not in note['markdown'][section['start_offset']:section['end_offset']]:
            raise ValueError('Repair quote is not in the current section')
        normalized.append({'section_id':section['section_id'],'candidate_quote':quote,'issue':issue})
    key = 'concept-repair:' + expected_revision + ':' + sha(canonical(normalized))
    with store.repository.transaction() as c:
        existing = one(c, 'SELECT id,payload_json FROM events WHERE dedupe_key=:k', k=key)
        if existing:
            return {'event_id':existing['id'], 'reused':True}
        current = one(c, 'SELECT current_revision_id FROM notes WHERE id=:id', id=concept_id)
        state = one(c, 'SELECT * FROM concept_work_state WHERE concept_id=:id', id=concept_id)
        if current['current_revision_id'] != expected_revision or not state or state['status'] != 'READY':
            raise Conflict('Concept changed or has unfinished work')
        items = desired(c, concept_id)
        digest = signature(items)
        if not items or digest != state['desired_input_hash']:
            raise Conflict('Repair requires the current effective source inputs')
        generation = state['desired_generation'] + 1
        c.execute(text("UPDATE concept_work_state SET desired_generation=:g,status='PENDING' WHERE concept_id=:id"), {'g':generation,'id':concept_id})
        proof = one(c, 'SELECT job_id FROM build_artifacts WHERE storage_ref=:r', r=f'artifacts/builds/{expected_revision}.json')
        store.repository.event(c, 'CONCEPT_REBUILD_REQUESTED', concept_id, None,
            {'generation':generation, 'input_sha256':digest, 'expected_revision':expected_revision,
                'repair_findings':normalized, 'cause':proof['job_id'] if proof else None}, key=key)
        event = one(c, 'SELECT id FROM events WHERE dedupe_key=:k', k=key)
    return {'event_id':event['id'], 'generation':generation, 'reused':False}
