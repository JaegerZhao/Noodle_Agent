"""Rebase accepted cold-start identities onto a CAS-fenced existing library."""

import json
import posixpath
import re

from ..contracts import Candidate, Review, canonical, sha
from ..markdown_links import references, resolve_reference
from ..repository import Conflict, one, rows
from .maintenance_bundle import prepare as prepare_checked

POLICY = 'ordered-replay-promotion-v2'


def remap(value, identities):
    """Only exact structural IDs change; prose and arbitrary substrings never do."""
    if isinstance(value, str):
        return identities.get(value, value)
    if isinstance(value, list):
        return [remap(item, identities) for item in value]
    if isinstance(value, dict):
        return {identities.get(key, key): remap(item, identities) for key, item in value.items()}
    return value


def row_remap(value, identities):
    result = remap(value, identities)
    for key, item in result.items():
        if key.endswith('_json') and isinstance(item, str):
            result[key] = canonical(remap(json.loads(item), identities))
    return result


def transfer_structure_coverage(source,target,bundle):
    """Carry a real full-graph review only across an equivalent identity rebase."""
    from ..events import dispatch
    from ..organization import coverage, organization_graph
    source_coverage=coverage(source)
    if source_coverage['status']!='COMPLETE':
        raise Conflict('Source graph has no current complete structure proof')
    before,after=organization_graph(source),organization_graph(target)
    mapping=bundle['identity_rebase']['mapping']
    def logical(graph,identities):
        nodes=sorted(canonical([identities.get(n['id'],n['id']),n['title'],n['description']]) for n in graph['nodes'])
        edges=sorted(canonical([identities.get(e['source'],e['source']),identities.get(e['target'],e['target']),
            e['relation'],e['navigation_role'],e.get('position')]) for e in graph['edges'])
        return nodes,edges
    if logical(before,mapping)!=logical(after,{}):
        raise Conflict('Mapped source structure differs; cannot claim transferred global coverage')
    with source.db.engine.connect() as c:
        source_review=json.loads(one(c,'SELECT result_json FROM jobs WHERE id=:j',j=source_coverage['covering_job_id'])['result_json'])
    key='verified-structure-transfer:'+bundle['bundle_sha256']
    with target.repository.transaction() as c:
        target.repository.event(c,'ORGANIZATION_REVIEW_REQUESTED','concept-organization',None,
            {'verified_transfer':bundle['bundle_sha256']},key=key)
        event=one(c,'SELECT id FROM events WHERE dedupe_key=:k',k=key)['id']
    dispatch(target)
    with target.db.engine.connect() as c:
        queued=one(c,"SELECT id FROM jobs WHERE event_id=:e AND kind='REVIEW_ORGANIZATION'",e=event)
    if not queued:
        raise Conflict('Normal dispatch has not scheduled the structure transfer receipt')
    job=queued['id']
    lease=target.jobs.claim('verified-structure-transfer',kind='REVIEW_ORGANIZATION',ready=lambda c,j:j['id']==job)
    if lease:
        if organization_graph(target)['snapshot']!=after['snapshot']:
            raise Conflict('Target graph changed during structure proof transfer')
        with target.db.engine.connect() as c:
            epoch=c.exec_driver_sql('SELECT validity_epoch FROM storage_contract WHERE id=1').scalar_one()
        target.jobs.finish(lease,{'status':'VERIFIED_GRAPH_IDENTITY_REBASE','scope':'ALL_CURRENT_CONCEPTS',
            'scope_key':None,'concept_ids':[],'policy':source_review['policy'],'reviewed_epoch':epoch,
            'reviewed_graph_snapshot':after['snapshot'],'source_snapshot':before['snapshot'],
            'source_review_job_id':source_coverage['covering_job_id'],'bundle_sha256':bundle['bundle_sha256']})
    value=coverage(target)
    if value['status']!='COMPLETE':
        raise Conflict('Transferred graph receipt is not current')
    return value


def identity_map(source, target):
    with source.db.engine.connect() as s, target.db.engine.connect() as t:
        claims = rows(s, 'SELECT * FROM concept_identity_claims')
        existing = {(r['domain'], r['identity_key']): r['concept_id'] for r in rows(t, 'SELECT * FROM concept_identity_claims')}
        mapping = {}
        for claim in claims:
            old_id = existing.get((claim['domain'], claim['identity_key']))
            if old_id and old_id != claim['concept_id']:
                source_note = one(s, 'SELECT title,aliases_json FROM notes WHERE id=:id', id=claim['concept_id'])
                target_note = one(t, 'SELECT title,aliases_json FROM notes WHERE id=:id', id=old_id)
                def names(note):
                    return {re.sub(r'[^\w\u4e00-\u9fff]', '', name.casefold()) for name in [note['title'], *json.loads(note['aliases_json'])]}
                if not names(source_note) & names(target_note):
                    raise Conflict('Identity key has incompatible names; explicit reconciliation required: '+claim['identity_key'])
                if claim['concept_id'] in mapping and mapping[claim['concept_id']] != old_id:
                    raise Conflict('One replay identity matches multiple existing objects')
                mapping[claim['concept_id']] = old_id
        for note in rows(s, "SELECT id,creation_key FROM notes WHERE kind='MOC' AND creation_key LIKE 'knowledge-root:%'"):
            old = one(t, 'SELECT id FROM notes WHERE creation_key=:k', k=note['creation_key'])
            if old:
                mapping[note['id']] = old['id']
        if len(set(mapping.values())) != len(mapping):
            raise Conflict('Multiple replay identities match the same old object')
        return mapping


def prepare(source, target, *, acceptance, atomic_scope):
    bundle = prepare_checked(source, target, acceptance=acceptance, atomic_scope=atomic_scope)
    original = json.loads(canonical(bundle))
    mapping = identity_map(source, target)
    source_proofs = {p['note']['id']: p['proof'] for p in original['publications']}
    with source.db.engine.connect() as c, target.db.engine.connect() as t:
        source_notes=[dict(r) for r in rows(c,'SELECT * FROM notes')]
        source_by_id={n['id']:n for n in source_notes}
        target_paths={n['id']:n['stable_path'] for n in rows(t,'SELECT id,stable_path FROM notes')}
    def markdown_links(body, owner):
        changed=[]
        owner_path=target_paths.get(mapping.get(owner['id'],owner['id']),owner['stable_path'])
        for ref in reversed(references(body)):
            resolved=resolve_reference(ref['target'],owner,source_notes)
            if resolved['id'] not in mapping and owner_path==owner['stable_path']:
                continue
            target_path=target_paths.get(mapping.get(resolved['id'],resolved['id']),resolved['stable_path'])
            path=posixpath.relpath(target_path,posixpath.dirname(owner_path))
            anchor=ref['target'].partition('#')[2]
            destination=path+('#'+anchor if anchor else '')
            original_text=body[ref['start']:ref['end']]
            replacement=(f"[[{destination}|{ref['label']}]]") if ref['wiki'] else original_text[:original_text.rfind('](')+2]+destination+')'
            body=body[:ref['start']]+replacement+body[ref['end']:]
            changed.append({'target_note_id':mapping.get(resolved['id'],resolved['id']),'old_target':ref['target'],'new_target':destination})
        return body,changed
    bundle = remap(bundle, mapping)
    for name in ('routes','decisions','claims','overrides'):
        bundle[name] = [row_remap(r, mapping) for r in bundle[name]]
    plan_keys = {}
    for plan in bundle['plans']:
        old_key = plan['row']['snapshot_hash']
        artifact = plan['artifact']
        new_key = sha(canonical([POLICY, old_key, mapping]))
        artifact['snapshot'] = new_key
        data = canonical(artifact)
        plan['row'].update(snapshot_hash=new_key, artifact_ref='artifacts/entity-plans/'+sha(data)+'.json',artifact_sha256=sha(data))
        plan_keys[old_key] = new_key
    for route in bundle['routes']:
        route['plan_hash'] = plan_keys.get(route['plan_hash'], route['plan_hash'])
    for item, source_item in zip(bundle['publications'], original['publications'], strict=True):
        link_rebases=[]
        for section in item['proof']['candidate']['sections']:
            section['markdown'], changes=markdown_links(section['markdown'],source_by_id[source_item['note']['id']])
            link_rebases.extend(changes)
        candidate = Candidate.model_validate(item['proof']['candidate'])
        checked = Review.model_validate(source_item['proof']['review'])
        item['proof']['review'] = checked.model_copy(update={'candidate_sha256':candidate.fingerprint,
            'reviewer':'program.verified_identity_rebase',
            'report':{**remap(checked.report,mapping),'identity_rebase':{'policy':POLICY,
                'source_candidate_sha256':checked.candidate_sha256,'mapped_candidate_sha256':candidate.fingerprint,
                'source_proof_sha256':sha(canonical(source_proofs[source_item['note']['id']])),
                'identity_map':mapping,'link_rebases':link_rebases}}}).model_dump()
        # Metadata ancestry remains in the source proof, not as a falsely equal
        # candidate chain after structural ID remapping.
        item['proof']['review']['report'].pop('metadata_only',None)
        item['proof']['review']['report'].pop('prior_revision',None)
        for parent in source_item['proof_chain']:
            for field in ('checks','section_contributions'):
                if field in parent['proof']['review']['report']:
                    item['proof']['review']['report'].setdefault(field,remap(parent['proof']['review']['report'][field],mapping))
        item['source_proof'] = source_item['proof']
        item['source_proof_chain'] = source_item['proof_chain']
        item['proof_chain'] = []
    revisions = {n['current_revision_id'] for n in bundle['atomics']}
    selected = {p['note']['id'] for p in bundle['publications']}
    with target.db.engine.connect() as c:
        old_notes = {r['id']:dict(r) for r in rows(c,"SELECT * FROM notes WHERE kind='CONCEPT' OR kind='MOC' AND creation_key LIKE 'knowledge-root:%'")}
        old_decisions = [dict(r) for r in rows(c,'SELECT * FROM contribution_decisions') if r['atomic_revision_id'] in revisions or r['concept_id'] in selected]
        def decision_key(value):
            return value['atomic_revision_id'],value['fragment_id'],value['policy'],value['concept_id'] or ''
        canonical_rows={decision_key(d):d for d in old_decisions}
        decision_ids={d['id']:canonical_rows[decision_key(d)]['id'] for d in bundle['decisions'] if decision_key(d) in canonical_rows}
        for row in bundle['decisions']:
            if row['id'] in decision_ids:
                previous=canonical_rows[decision_key(row)]
                row['id']=decision_ids[row['id']]
                row['created_at']=previous['created_at']
        for item in bundle['publications']:
            review=item['proof']['review']
            review['report']=remap(review['report'],decision_ids)
        incoming={d['id'] for d in bundle['decisions']}
        bundle['decisions'] = [{**d,'status':'SKIPPED'} for d in old_decisions if d['id'] not in incoming] + bundle['decisions']
        for identity, note in old_notes.items():
            if note['kind']!='CONCEPT' or identity in selected or not note['current_revision_id']:
                continue
            supports = {r['atomic_revision_id'] for r in rows(c,'SELECT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r',r=note['current_revision_id'])}
            if supports and supports <= revisions:
                bundle['retired'].append(identity)
        selected.update(bundle['retired'])
        for identity in selected:
            old = old_notes.get(identity)
            if old and one(c,'SELECT 1 FROM sections WHERE revision_id=:r AND protected=1',r=old['current_revision_id']):
                raise Conflict('Protected formal content requires object-specific reconciliation: '+identity)
        for item in bundle['publications']:
            old = old_notes.get(item['note']['id'])
            item['expected_revision'] = old['current_revision_id'] if old else None
            if old:
                for field in ('stable_path','creation_key','created_at'):
                    item['note'][field] = old[field]
        bundle['before'] = {'notes':[old_notes[i] for i in sorted(selected) if i in old_notes],
            'routes':[dict(r) for r in rows(c,'SELECT * FROM atomic_entity_routes') if r['atomic_revision_id'] in revisions],
            'decisions':[dict(r) for r in rows(c,'SELECT * FROM contribution_decisions') if r['atomic_revision_id'] in revisions or r['concept_id'] in selected],
            'claims':[dict(r) for r in rows(c,'SELECT * FROM concept_identity_claims') if r['concept_id'] in selected],
            'overrides':[dict(r) for r in rows(c,'SELECT * FROM relation_overrides') if r['owner_note_id'] in selected],
            'work':[dict(r) for r in rows(c,'SELECT * FROM concept_work_state') if r['concept_id'] in selected],
            'revision_statuses':{old_notes[i]['current_revision_id']:one(c,'SELECT status FROM note_revisions WHERE id=:r',r=old_notes[i]['current_revision_id'])['status'] for i in selected if i in old_notes and old_notes[i]['current_revision_id']}}
    bundle['identity_rebase'] = {'policy':POLICY,'mapping':mapping,'decision_id_mapping':decision_ids,'source_bundle_sha256':original['bundle_sha256']}
    bundle['bundle_sha256'] = sha(canonical({k:v for k,v in bundle.items() if k!='bundle_sha256'}))
    return bundle
