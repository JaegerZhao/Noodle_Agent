"""CAS-fenced transfer of checked Concept artifacts through the normal publisher."""

import json

from sqlalchemy import text

from ..contracts import Candidate, Review, canonical, sha
from ..jobs import require_lease
from ..repository import Conflict, insert, one, rows
from .coordinator import desired, signature

POLICY = 'checked-concept-transfer-v1'
KIND = 'APPLY_CONCEPT_MAINTENANCE'


def atomic_heads(store, connection, scope=None):
    return [dict(r) for r in rows(connection, "SELECT n.id,n.current_revision_id,n.visibility,r.markdown_ref,r.markdown_sha256 FROM notes n LEFT JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' ORDER BY n.id")
            if scope is None or r['id'] in scope]


def proof(store, revision):
    ref = f'artifacts/builds/{revision}.json'
    with store.db.engine.connect() as c:
        artifact = one(c, 'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r', r=ref)
    if not artifact:
        raise Conflict('Missing checked publication contract')
    raw = json.loads(store.read(ref, artifact['sha256']))
    candidate, review = Candidate.model_validate(raw['candidate']), Review.model_validate(raw['review'])
    if not review.passed or candidate.fingerprint != review.candidate_sha256:
        raise Conflict('Publication review does not match the unchanged candidate')
    return raw


def prepare(source, target, *, acceptance, atomic_scope=None):
    if not acceptance.get('engineering_passed') or not acceptance.get('semantic_passed') or not acceptance.get('lifecycle_passed'):
        raise Conflict('Transfer requires recorded engineering, semantic and lifecycle acceptance')
    with source.db.engine.connect() as s, target.db.engine.connect() as t:
        atomics = atomic_heads(target, t, atomic_scope)
        if atomics != atomic_heads(source, s, atomic_scope) or atomic_scope is not None and {n['id'] for n in atomics} != set(atomic_scope):
            raise Conflict('Transfer requires identical original Atomic heads and visibility')
        for head in atomics:
            if head['markdown_ref']:
                source.read(head['markdown_ref'], head['markdown_sha256'])
                target.read(head['markdown_ref'], head['markdown_sha256'])
        if source.repository.policy_version != target.repository.policy_version:
            raise Conflict('Publication policies differ')
        old_notes = {r['id']: dict(r) for r in rows(t, "SELECT * FROM notes WHERE kind!='ATOMIC'")}
        current = rows(s, "SELECT n.*,r.status revision_status FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='CONCEPT' OR (n.kind='MOC' AND n.creation_key LIKE 'knowledge-root:%') ORDER BY n.id")
        publications, retired = [], []
        for row in current:
            old = old_notes.get(row['id'])
            if row['visibility'] != 'ACTIVE':
                continue
            if row['revision_status'] == 'WITHDRAWN':
                if old and old['current_revision_id'] and not desired(s, row['id']):
                    previous = one(t, 'SELECT status FROM note_revisions WHERE id=:r', r=old['current_revision_id'])
                    if previous['status'] == 'PUBLISHED':
                        retired.append(row['id'])
                continue
            if not source.content.valid_revision(s, row['current_revision_id']):
                raise Conflict('Candidate is not currently supported')
            raw = proof(source, row['current_revision_id'])
            candidate = Candidate.model_validate(raw['candidate'])
            if old and old['current_revision_id']:
                prior = Candidate.model_validate(proof(target, old['current_revision_id'])['candidate'])
                if prior.fingerprint == candidate.fingerprint:
                    continue
            chain, seen, cursor = [], set(), raw
            while cursor['review'].get('report', {}).get('metadata_only'):
                parent = cursor['review']['report'].get('prior_revision')
                if not parent or parent in seen:
                    raise Conflict('Metadata proof chain is missing or cyclic')
                seen.add(parent)
                preceding = proof(source, parent)
                if Candidate.model_validate(preceding['candidate']).sections != candidate.sections:
                    raise Conflict('Metadata-only proof changed checked content')
                chain.append({'revision_id': parent, 'proof': preceding})
                cursor = preceding
            publications.append({'note': dict(row), 'proof': raw, 'proof_chain': chain,
                'expected_revision': old['current_revision_id'] if old else None})
        selected = {p['note']['id'] for p in publications} | set(retired)
        old_routes = {r['atomic_revision_id']: dict(r) for r in rows(t, 'SELECT * FROM atomic_entity_routes')}
        routes = [dict(r) for r in rows(s, 'SELECT * FROM atomic_entity_routes')
                  if dict(r) != old_routes.get(r['atomic_revision_id'])]
        revisions = {r['atomic_revision_id'] for r in routes}
        before_decisions = [dict(r) for r in rows(t, 'SELECT * FROM contribution_decisions')
                           if r['concept_id'] in selected or r['atomic_revision_id'] in revisions]
        old_decisions = {r['id']: r for r in before_decisions}
        decisions = [dict(r) for r in rows(s, 'SELECT * FROM contribution_decisions')
                     if r['id'] in old_decisions or r['atomic_revision_id'] in revisions and r['status'] in {'ACCEPTED', 'REVIEW'}]
        plans = []
        for key in sorted({r['plan_hash'] for r in routes}):
            if one(t, 'SELECT 1 FROM concept_entity_plans WHERE snapshot_hash=:h', h=key):
                continue
            row = one(s, "SELECT * FROM concept_entity_plans WHERE snapshot_hash=:h AND state='READY'", h=key)
            if not row:
                raise Conflict('Current route has no ready immutable plan')
            plans.append({'row': dict(row), 'artifact': json.loads(source.read(row['artifact_ref'], row['artifact_sha256']))})
        claims = [dict(r) for r in rows(s, 'SELECT * FROM concept_identity_claims') if r['concept_id'] in selected]
        overrides = [dict(r) for r in rows(s, 'SELECT * FROM relation_overrides') if r['owner_note_id'] in selected]
        value = {'policy': POLICY, 'acceptance': acceptance, 'atomics': atomics,
            'publications': publications, 'retired': retired, 'routes': routes, 'plans': plans,
            'decisions': decisions, 'claims': claims, 'overrides': overrides,
            'before': {'notes': [old_notes[i] for i in sorted(selected) if i in old_notes],
                'routes': [old_routes[r['atomic_revision_id']] for r in routes if r['atomic_revision_id'] in old_routes],
                'decisions': before_decisions,
                'claims': [dict(r) for r in rows(t, 'SELECT * FROM concept_identity_claims') if r['concept_id'] in selected],
                'overrides': [dict(r) for r in rows(t, 'SELECT * FROM relation_overrides') if r['owner_note_id'] in selected],
                'revision_statuses': {n['current_revision_id']: one(t, 'SELECT status FROM note_revisions WHERE id=:r', r=n['current_revision_id'])['status']
                    for n in old_notes.values() if n['id'] in selected and n['current_revision_id']},
                'work': [dict(r) for r in rows(t, 'SELECT * FROM concept_work_state') if r['concept_id'] in selected]}}
    if atomic_scope is not None:
        value['atomic_scope'] = sorted(atomic_scope)
    value['bundle_sha256'] = sha(canonical(value))
    return value


def enqueue(store, bundle):
    digest = bundle['bundle_sha256']
    if digest != sha(canonical({k: v for k, v in bundle.items() if k != 'bundle_sha256'})):
        raise Conflict('Transfer bundle hash differs')
    ref = f'artifacts/concept-transfers/{digest}/bundle.json'
    data = canonical(bundle).encode()
    store.put(ref, data)
    with store.repository.transaction() as c:
        store.repository.event(c, 'CONCEPT_MAINTENANCE_APPLY_REQUESTED', 'concept-maintenance', None,
            {'bundle_ref': ref, 'bundle_sha256': sha(data)}, key='concept-transfer:' + digest)
        event = one(c, 'SELECT id FROM events WHERE dedupe_key=:key', key='concept-transfer:' + digest)['id']
    return store.jobs.enqueue(event, KIND, {'payload': {'bundle_ref': ref, 'bundle_sha256': sha(data)}})


def apply(store, lease):
    payload = lease.input['payload']
    bundle = json.loads(store.read(payload['bundle_ref'], payload['bundle_sha256']))
    if bundle['bundle_sha256'] != sha(canonical({k: v for k, v in bundle.items() if k != 'bundle_sha256'})):
        raise Conflict('Transfer bundle was changed')
    before = bundle['before']
    before_notes = {n['id']: n for n in before['notes']}
    updated, published, preparations = {}, [], []
    with store.repository.transaction() as c:
        require_lease(c, lease)
        if atomic_heads(store, c, bundle.get('atomic_scope')) != bundle['atomics']:
            raise Conflict('Original Atomic heads changed since acceptance')
        for note in before['notes']:
            current = one(c, 'SELECT * FROM notes WHERE id=:id', id=note['id'])
            if dict(current) != note:
                raise Conflict('User or worker changed an affected note')
        for previous in before['routes']:
            current = one(c, 'SELECT * FROM atomic_entity_routes WHERE atomic_revision_id=:r', r=previous['atomic_revision_id'])
            if dict(current) != previous:
                raise Conflict('Affected route changed since acceptance')
        for previous in before['decisions']:
            current = one(c, 'SELECT * FROM contribution_decisions WHERE id=:id', id=previous['id'])
            if dict(current) != previous:
                raise Conflict('Affected contribution changed since acceptance')
        selected = {p['note']['id'] for p in bundle['publications']} | set(bundle['retired'])
        for table, field, expected in [('concept_identity_claims', 'concept_id', before['claims']),
                                        ('relation_overrides', 'owner_note_id', before['overrides'])]:
            actual = [dict(r) for r in rows(c, 'SELECT * FROM ' + table) if r[field] in selected]
            if sorted(map(canonical, actual)) != sorted(map(canonical, expected)):
                raise Conflict('Affected identity or relation metadata changed since acceptance')
        for item in bundle['publications']:
            note = item['note']
            if note['id'] not in before_notes:
                store.content.create_note(note['kind'], note['title'], domain=note['domain'],
                    aliases=json.loads(note['aliases_json']), creation_key=note['creation_key'],
                    lease=lease, _note_id=note['id'], _connection=c)
        for plan in bundle['plans']:
            row = plan['row']
            store.put(row['artifact_ref'], canonical(plan['artifact']).encode())
            insert(c, 'concept_entity_plans', **{**row, 'job_id': lease.job_id, 'lease_token': lease.token})
        for row in bundle['claims']:
            c.execute(text('INSERT INTO concept_identity_claims(domain,identity_key,concept_id,definition_json) VALUES(:domain,:identity_key,:concept_id,:definition_json) ON CONFLICT(domain,identity_key) DO UPDATE SET definition_json=excluded.definition_json WHERE concept_identity_claims.concept_id=excluded.concept_id'), row)
            if one(c, 'SELECT concept_id FROM concept_identity_claims WHERE domain=:d AND identity_key=:k', d=row['domain'], k=row['identity_key'])['concept_id'] != row['concept_id']:
                raise Conflict('Canonical identity was reserved concurrently')
        for row in bundle['decisions']:
            previous = one(c, 'SELECT * FROM contribution_decisions WHERE id=:id', id=row['id'])
            if previous:
                columns=('atomic_revision_id','fragment_id','policy','concept_id','section_id','action',
                         'status','fragment_json','decision_json','created_at','applied_revision_id')
                replacement={**row,'applied_revision_id':None if row['status']=='ACCEPTED' else previous['applied_revision_id']}
                c.execute(text('UPDATE contribution_decisions SET '+','.join(key+'=:'+key for key in columns)+' WHERE id=:id'),replacement)
            else:
                insert(c, 'contribution_decisions', **{**row, 'applied_revision_id': None})
        for row in bundle['routes']:
            c.execute(text('INSERT INTO atomic_entity_routes(atomic_revision_id,plan_hash,concept_id,status,route_json,policy) VALUES(:atomic_revision_id,:plan_hash,:concept_id,:status,:route_json,:policy) ON CONFLICT(atomic_revision_id) DO UPDATE SET plan_hash=excluded.plan_hash,concept_id=excluded.concept_id,status=excluded.status,route_json=excluded.route_json,policy=excluded.policy'), row)
        for identity in bundle['retired']:
            note = before_notes[identity]
            if one(c, 'SELECT 1 FROM sections WHERE revision_id=:r AND protected=1', r=note['current_revision_id']):
                raise Conflict('A protected Concept cannot be retired automatically')
            if desired(c, identity):
                raise Conflict('Retirement still has valid contributions')
            c.execute(text("UPDATE note_revisions SET status='WITHDRAWN' WHERE id=:r"), {'r': note['current_revision_id']})
            store.repository.advance_epoch(c)
            store.repository.event(c, 'NOTE_INVALIDATED', identity, note['current_revision_id'],
                {'reason': POLICY}, key='transfer-retired:' + bundle['bundle_sha256'] + ':' + identity)
            updated[identity] = note['current_revision_id']
        # Metadata-only removals first: otherwise two valid final replacements
        # can create a temporary cycle against each other's old relationships.
        # Every intermediate and final revision still uses normal validation.
        expected_heads = {item['note']['id']: item['expected_revision'] for item in bundle['publications']}
        for item in bundle['publications']:
            old = before_notes.get(item['note']['id'])
            if old and old['current_revision_id']:
                prior_proof = proof(store, old['current_revision_id'])
                prior = Candidate.model_validate(prior_proof['candidate'])
                new = Candidate.model_validate(item['proof']['candidate'])
                if any(link not in new.links for link in prior.links if link.origin == 'AUTHOR'):
                    raise Conflict('Verified transfer must preserve author-owned relationships')
                retained = [link for link in prior.links if link.origin == 'AUTHOR' or link in new.links]
                if retained != prior.links:
                    prepared = prior.model_copy(update={'links': retained})
                    review = Review(candidate_sha256=prepared.fingerprint, policy_version=prepared.policy_version,
                        reviewer='program.transfer_relation_preparation', passed=True,
                        report={**prior_proof['review']['report'], 'metadata_only': True,
                            'prior_revision': old['current_revision_id'], 'transfer_bundle': bundle['bundle_sha256']})
                    result = store.publisher.publish(prepared, expected_revision=old['current_revision_id'],
                        review=review, lease=lease, _connection=c)
                    expected_heads[prior.note_id] = result['revision_id']
                    preparations.append(result)
        for item in sorted(bundle['publications'], key=lambda item: (item['note']['kind'] == 'MOC', item['note']['id'])):
            candidate = Candidate.model_validate(item['proof']['candidate'])
            review = Review.model_validate(item['proof']['review'])
            report = {**review.report, 'verified_transfer': {'policy': POLICY, 'bundle_sha256': bundle['bundle_sha256'],
                'source_revision': item['note']['current_revision_id'], 'source_proof_sha256': sha(canonical(item['proof'])),
                'proof_chain': [p['revision_id'] for p in item['proof_chain']]}}
            report.pop('metadata_only', None)
            report.pop('prior_revision', None)
            for parent in item['proof_chain']:
                for field in ('checks', 'section_contributions'):
                    if field in parent['proof']['review']['report']:
                        report.setdefault(field, parent['proof']['review']['report'][field])
            data = canonical({'proof': item['proof'], 'chain': item['proof_chain']}).encode()
            store.put(f"artifacts/concept-transfers/{bundle['bundle_sha256']}/{item['note']['id']}.json", data)
            result = store.publisher.publish(candidate, expected_revision=expected_heads[candidate.note_id],
                review=Review(**{**review.model_dump(), 'report': report}), lease=lease, _connection=c)
            identity, revision = candidate.note_id, result['revision_id']
            updated[identity] = revision
            c.execute(text('UPDATE notes SET aliases_json=:a WHERE id=:id'), {'a': item['note']['aliases_json'], 'id': identity})
            c.execute(text("UPDATE contribution_decisions SET applied_revision_id=:r WHERE concept_id=:id AND status='ACCEPTED'"), {'r': revision, 'id': identity})
            published.append(result)
        for row in bundle['overrides']:
            replacement = {**row, 'source_revision_id': updated.get(row['owner_note_id'])}
            c.execute(text('INSERT INTO relation_overrides(owner_note_id,relation_key,operation_json,source_revision_id,run_id,created_at) VALUES(:owner_note_id,:relation_key,:operation_json,:source_revision_id,:run_id,:created_at) ON CONFLICT(owner_note_id,relation_key) DO UPDATE SET operation_json=excluded.operation_json,source_revision_id=excluded.source_revision_id,run_id=excluded.run_id'), replacement)
        for identity in updated:
            kind = one(c, 'SELECT kind FROM notes WHERE id=:id', id=identity)['kind']
            if kind != 'CONCEPT':
                continue
            items = desired(c, identity)
            state = one(c, 'SELECT desired_generation FROM concept_work_state WHERE concept_id=:id', id=identity)
            generation = state['desired_generation'] + 1 if state else 1
            c.execute(text('INSERT INTO concept_work_state(concept_id,desired_generation,desired_input_hash,completed_generation,status) VALUES(:id,:g,:h,:g,:s) ON CONFLICT(concept_id) DO UPDATE SET desired_generation=:g,desired_input_hash=:h,completed_generation=:g,status=:s'),
                {'id': identity, 'g': generation, 'h': signature(items), 's': 'READY' if items else 'NO_SUPPORTED_INPUT'})
        receipt = {'policy': POLICY, 'bundle_sha256': bundle['bundle_sha256'], 'updated_heads': updated,
            'published': published, 'retired': bundle['retired'], 'metadata_preparations': preparations, 'model_calls': 0}
        receipt['after'] = {
            'notes': [dict(r) for r in rows(c, 'SELECT * FROM notes') if r['id'] in selected],
            'claims': [dict(r) for r in rows(c, 'SELECT * FROM concept_identity_claims') if r['concept_id'] in selected],
            'routes': [dict(r) for r in rows(c, 'SELECT * FROM atomic_entity_routes') if r['atomic_revision_id'] in {r['atomic_revision_id'] for r in bundle['routes']}],
            'overrides': [dict(r) for r in rows(c, 'SELECT * FROM relation_overrides') if r['owner_note_id'] in selected],
            'decisions': [dict(r) for r in rows(c, 'SELECT * FROM contribution_decisions') if r['concept_id'] in selected or r['atomic_revision_id'] in {r['atomic_revision_id'] for r in bundle['routes']}]}
        data = canonical(receipt).encode()
        store.put(f"artifacts/concept-transfers/{bundle['bundle_sha256']}/receipt.json", data)
    return receipt


def restore_rows(connection, table, values, keys):
    for value in values:
        columns = list(value)
        statement = 'INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join(':' + k for k in columns) + ')'
        statement += ' ON CONFLICT(' + ','.join(keys) + ') DO UPDATE SET ' + ','.join(k + '=excluded.' + k for k in columns if k not in keys)
        connection.execute(text(statement), value)


def rollback(store, bundle, receipt):
    """Restore only this transfer's objects; a later user edit blocks its rollback."""
    digest = bundle['bundle_sha256']
    if digest != receipt['bundle_sha256'] or digest != sha(canonical({k: v for k, v in bundle.items() if k != 'bundle_sha256'})):
        raise Conflict('Rollback receipt and bundle differ')
    before, after = bundle['before'], receipt['after']
    selected = set(receipt['updated_heads'])
    scope = {'notes': ('notes', 'id', selected), 'claims': ('concept_identity_claims', 'concept_id', selected),
        'routes': ('atomic_entity_routes', 'atomic_revision_id', {r['atomic_revision_id'] for r in bundle['routes']}),
        'overrides': ('relation_overrides', 'owner_note_id', selected)}
    with store.repository.transaction() as c:
        existing = one(c, 'SELECT id FROM events WHERE dedupe_key=:k', k='concept-transfer-rollback:' + digest)
        if existing:
            return {'status': 'ALREADY_ROLLED_BACK', 'bundle_sha256': digest}
        if atomic_heads(store, c, bundle.get('atomic_scope')) != bundle['atomics']:
            raise Conflict('Atomic input changes require rebasing before rollback')
        for key, (table, field, identities) in scope.items():
            actual = [dict(r) for r in rows(c, 'SELECT * FROM ' + table) if r[field] in identities]
            if sorted(map(canonical, actual)) != sorted(map(canonical, after[key])):
                raise Conflict('Post-transfer edit prevents rollback: ' + key)
        affected_atomics = scope['routes'][2]
        actual = [dict(r) for r in rows(c, 'SELECT * FROM contribution_decisions') if r['concept_id'] in selected or r['atomic_revision_id'] in affected_atomics]
        if sorted(map(canonical, actual)) != sorted(map(canonical, after['decisions'])):
            raise Conflict('Post-transfer contribution change prevents rollback')
        for row in bundle['decisions']:
            if row['id'] not in {d['id'] for d in before['decisions']}:
                c.execute(text("UPDATE contribution_decisions SET status='SKIPPED' WHERE id=:id"), {'id': row['id']})
        restore_rows(c, 'contribution_decisions', before['decisions'], ['id'])
        restore_rows(c, 'atomic_entity_routes', before['routes'], ['atomic_revision_id'])
        for table, field, values, keys in [
            ('concept_identity_claims', 'concept_id', before['claims'], ['domain', 'identity_key']),
            ('relation_overrides', 'owner_note_id', before['overrides'], ['owner_note_id', 'relation_key']),
            ('concept_work_state', 'concept_id', before['work'], ['concept_id'])]:
            for identity in selected:
                c.execute(text('DELETE FROM ' + table + ' WHERE ' + field + '=:id'), {'id': identity})
            restore_rows(c, table, values, keys)
        for revision, status in before['revision_statuses'].items():
            c.execute(text('UPDATE note_revisions SET status=:s WHERE id=:r'), {'s': status, 'r': revision})
        prior_notes = {n['id']: n for n in before['notes']}
        for identity in selected:
            if identity in prior_notes:
                old = prior_notes[identity]
                c.execute(text('UPDATE notes SET current_revision_id=:r,title=:t,aliases_json=:a,visibility=:v WHERE id=:id'),
                    {'r': old['current_revision_id'], 't': old['title'], 'a': old['aliases_json'], 'v': old['visibility'], 'id': identity})
                event, revision = 'NOTE_RESTORED', old['current_revision_id']
            else:
                c.execute(text("UPDATE notes SET visibility='WITHDRAWN' WHERE id=:id"), {'id': identity})
                event, revision = 'NOTE_WITHDRAWN', receipt['updated_heads'][identity]
            store.repository.event(c, event, identity, revision, {'transfer_rollback': digest}, key='transfer-restored:' + digest + ':' + identity)
        store.repository.advance_epoch(c)
        store.repository.event(c, 'CONCEPT_MAINTENANCE_ROLLED_BACK', 'concept-maintenance', None,
            {'bundle_sha256': digest}, key='concept-transfer-rollback:' + digest)
    return {'status': 'ROLLED_BACK', 'objects': len(selected), 'bundle_sha256': digest, 'model_calls': 0}
