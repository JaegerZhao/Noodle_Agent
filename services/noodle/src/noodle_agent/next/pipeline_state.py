"""Product readiness is coverage of current revisions, never queue success."""

import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .contracts import canonical, sha
from .repository import Conflict, identifier, insert, now, one, rows
from .retrieval.chunks import CHUNK_POLICY


@lru_cache(maxsize=12)
def artifact(root, ref, digest):
    data = (Path(root) / ref).read_bytes()
    if sha(data) != digest:
        raise Conflict("Readiness artifact hash differs")
    return json.loads(data)


@lru_cache(maxsize=2048)
def compiled_artifact(root, ref, digest):
    return artifact(root, ref, digest)


def source_plan(store, revision):
    with store.db.engine.connect() as c:
        job = one(c, "SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC,id DESC LIMIT 1", r=revision)
        checkpoints = rows(c, "SELECT * FROM job_checkpoints WHERE job_id=:j AND state='DONE' ORDER BY created_at DESC", j=job['id']) if job else []
    row = next((r for r in checkpoints if r['stage'].startswith('source.sections.plan')), None)
    plan = artifact(str(store.root), row['artifact_ref'], row['artifact_sha256']) if row else None
    return job, plan, checkpoints


def indexed_revisions(store, notes, profile_id=None):
    """Check exact chunk sets in one activated HYBRID generation/profile."""
    with store.db.engine.connect() as c:
        heads = rows(c, "SELECT g.* FROM index_heads h JOIN index_generations g ON g.id=h.generation WHERE h.mode_key LIKE 'HYBRID:%' AND g.status='READY' ORDER BY g.created_at DESC")
    for head in heads:
        if not head['mode_key'].endswith(':' + CHUNK_POLICY) or profile_id and head['profile_id'] != profile_id:
            continue
        manifest = artifact(str(store.root), head['manifest_ref'], head['manifest_sha256'])
        actual = defaultdict(set)
        for chunk in manifest:
            actual[chunk['revision_id']].add(chunk['id'])
        covered = set()
        for note in notes:
            revision = note['current_revision_id']
            if revision not in actual:
                continue
            binding = {'revision': revision, 'title': note['title'], 'markdown': note['markdown_sha256'], 'policy': CHUNK_POLICY}
            ref = f"indexes/compiled/{sha(canonical(binding))}.json"
            digest_file = store.path(ref + '.sha256')
            if not digest_file.exists():
                continue
            compiled = compiled_artifact(str(store.root), ref, digest_file.read_text(encoding='utf-8'))
            expected = {v['id'] for v in compiled['chunks']}
            if compiled['binding'] == binding and expected and expected == actual[revision]:
                covered.add(revision)
        return covered, dict(head)
    return set(), None


def readiness(store, revision, *, profile_id=None, structure=None):
    from .eligibility import eligible
    from .organization import coverage as organization_coverage
    with store.db.engine.connect() as c:
        source = one(c, "SELECT r.source_id,s.title FROM source_revisions r JOIN sources s ON s.id=r.source_id WHERE r.id=:r", r=revision)
        if not source or not store.sources.source_valid(c, revision):
            raise Conflict('Source revision is not current and available')
        notes = rows(c, "SELECT n.*,r.markdown_sha256,r.created_at AS published_at FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND substr(n.creation_key,1,length(:p))=:p", p=f'atomic:{revision}:')
        policy = one(c, 'SELECT active_policy FROM storage_contract WHERE id=1')['active_policy']
        valid = {n['revision_id'] for n in eligible(c, [n['current_revision_id'] for n in notes])} if policy==store.repository.policy_version else set()
        notes = [n for n in notes if n['current_revision_id'] in valid]
        concepts = rows(c, "SELECT DISTINCT d.concept_id,w.status,w.desired_generation,w.completed_generation,n.current_revision_id,n.title,r.markdown_sha256 FROM contribution_decisions d LEFT JOIN concept_work_state w ON w.concept_id=d.concept_id LEFT JOIN notes n ON n.id=d.concept_id LEFT JOIN note_revisions r ON r.id=n.current_revision_id JOIN note_revisions a ON a.id=d.atomic_revision_id JOIN notes an ON an.current_revision_id=a.id WHERE d.status='ACCEPTED' AND d.concept_id IS NOT NULL AND substr(an.creation_key,1,length(:p))=:p", p=f'atomic:{revision}:')
        routes = rows(c, "SELECT r.atomic_revision_id,r.status FROM atomic_entity_routes r JOIN note_revisions a ON a.id=r.atomic_revision_id JOIN notes n ON n.current_revision_id=a.id WHERE substr(n.creation_key,1,length(:p))=:p", p=f'atomic:{revision}:')
    job, plan, checkpoints = source_plan(store, revision)
    from .metrics import root_for
    from .provider_errors import provider_issue
    failures = []
    concept_ids = {v['concept_id'] for v in concepts}
    atomic_revisions = {v['current_revision_id'] for v in notes}
    settled = {r['atomic_revision_id'] for r in routes if r['status'] in {'ACCEPTED', 'SKIPPED'}}
    work = {v['concept_id']: v for v in concepts}
    with store.db.engine.connect() as c:
        for failed in rows(c, "SELECT * FROM jobs WHERE kind IN ('ANALYZE_ATOMIC','BUILD_CONCEPT') AND status='REVIEW'"):
            data = json.loads(failed['input_json'])
            if failed['kind'] == 'ANALYZE_ATOMIC':
                batch = data.get('payload', {}).get('routing_batch', {})
                members = {v['revision_id'] for v in batch.get('atomics', [])} or {data.get('revision_id')}
                if not (members & atomic_revisions) - settled:
                    continue
            else:
                state = work.get(data.get('entity_id'))
                generation = data.get('payload', {}).get('generation')
                if state and generation is not None and (generation != state['desired_generation']
                        or state['status'] == 'READY' and state['completed_generation'] >= generation):
                    continue
            belongs = (job and root_for(c, failed['id']) == job['id']
                or data.get('revision_id') in atomic_revisions
                or data.get('entity_id') in concept_ids
                or data.get('payload', {}).get('routing_batch', {}).get('source_revision_id') == revision)
            if belongs:
                error = json.loads(failed['error_json'])
                failures.append({'job_id': failed['id'], 'kind': failed['kind'], 'error': error,
                    'provider_issue': provider_issue(error)})
    generation_issue = provider_issue(json.loads(job['error_json'])) if job and job['status'] == 'REVIEW' else None
    indexed, head = indexed_revisions(store, notes, profile_id)
    by_key = {n['creation_key'].split(':source-section:', 1)[-1]: n for n in notes}
    resolved, deferred = set(), set()
    for row in checkpoints:
        if not row['stage'].startswith('source.publish.'):
            continue
        value = artifact(str(store.root), row['artifact_ref'], row['artifact_sha256'])
        resolved.update(value.get('resolved_plan_keys', []))
        deferred.update(v['plan_key'] for v in value.get('deferred', []))
    deferred.difference_update(resolved)
    plans = plan.get('plans', []) if plan else []
    scopes = plan.get('chapter_tasks', []) if plan else []
    groups = [('document', source['title'], [p['key'] for p in plans])]
    groups.extend((g['chapter_key'], g['title'], g['plan_keys']) for g in scopes)
    results = []
    for key, title, keys in groups:
        active = [by_key[k] for k in keys if k in by_key]
        done = set(keys) <= set(by_key) | resolved and not set(keys) & deferred
        full = bool(keys and active) and done and all(n['current_revision_id'] in indexed for n in active)
        status = 'READY' if full else 'INDEX_PENDING' if done and active else 'PARTIAL' if active else 'NO_CONTENT' if keys and done else 'GENERATING' if job and job['status'] == 'RUNNING' else 'WAITING'
        results.append({'key': key, 'title': title, 'status': status, 'planned': len(keys), 'published': len(active), 'indexed': sum(n['current_revision_id'] in indexed for n in active), 'omitted': len(set(keys) & (resolved - set(by_key))), 'deferred': len(set(keys) & deferred), 'manifest_sha256': sha(canonical({'keys': keys, 'revisions': sorted(n['current_revision_id'] for n in active), 'policy': plan.get('planning_policy') if plan else None, 'profile_id': profile_id or (head['profile_id'] if head else None), 'chunk_policy': CHUNK_POLICY}))})
    current = results[0]
    if job and job['status'] in {'PAUSED', 'REVIEW', 'CANCELLED'} and current['status'] not in {'READY', 'INDEX_PENDING'}:
        current['status'] = 'PARTIAL' if notes else job['status']
    completed = sum(v['status'] == 'READY' and v['desired_generation'] == v['completed_generation'] for v in concepts)
    concept_indexed, _ = indexed_revisions(store, [n for n in concepts if n['current_revision_id'] and n['status'] == 'READY'], profile_id)
    routing_pending = max(0, len(notes) - sum(r['status'] in {'ACCEPTED', 'SKIPPED'} for r in routes))
    background = 'PARTIAL' if any(r['status'] == 'REVIEW' for r in routes) else 'COMPLETE' if current['status']=='READY' and not routing_pending and completed == len(concepts) and len(concept_indexed) == len(concepts) and notes else 'RUNNING' if notes else 'WAITING'
    if any(v['status'] == 'REVIEW' for v in concepts):
        background = 'PARTIAL'
    if failures:
        background = 'PROVIDER_BLOCKED' if any(v['provider_issue'] for v in failures) else 'PARTIAL'
    if job and job['status'] == 'PAUSED' and background != 'COMPLETE':
        background = 'PAUSED'
    ready_at = None
    if current['status'] == 'READY' and job:
        with store.db.engine.connect() as c:
            milestone = one(c, "SELECT created_at FROM job_stage_events WHERE job_id=:j AND stage='atomic-index-ready' AND json_extract(detail_json,'$.scope_key')='document' AND json_extract(detail_json,'$.manifest_sha256')=:m ORDER BY created_at LIMIT 1", j=job['id'], m=current['manifest_sha256'])
        ready_at = milestone['created_at'] if milestone else head['created_at']
    return {'schema_version': 'source-readiness-v1', 'source_id': source['source_id'], 'source_revision_id': revision,
        'job_id': job['id'] if job else None, 'generation_status': job['status'] if job else None, 'generation_issue': generation_issue,
        'material_profile': plan.get('document_profile', {}) if plan else {}, 'notes_status': current['status'],
        'readable': bool(notes), 'coverage': current, 'chapters': results[1:],
        'atomic_index': {'status': 'READY' if current['status'] == 'READY' else 'PARTIAL' if indexed else 'PENDING', 'generation': head['id'] if head else None, 'profile_id': head['profile_id'] if head else None, 'ready_at': ready_at},
        'concept_maintenance': {'status': background, 'total': len(concepts), 'completed': completed, 'indexed': len(concept_indexed), 'routing_pending': routing_pending,
            'failed_routing_jobs': sum(v['kind'] == 'ANALYZE_ATOMIC' for v in failures), 'failed_build_jobs': sum(v['kind'] == 'BUILD_CONCEPT' for v in failures), 'issues': failures[:10]},
        'knowledge_structure': {**(organization_coverage(store) if structure is None else structure), 'scope': 'PUBLISHED_CONCEPTS',
            'upstream_pending': routing_pending + max(0, len(concepts) - completed),
            'upstream_status': background},
        'first_readable_at': min((n['published_at'] for n in notes), default=None)}


def reading(store, revision, *, profile_id=None):
    from .source_structure import source_structure
    data = source_structure(store, revision)
    data['readiness'] = readiness(store, revision, profile_id=profile_id)
    return data


def record_index_milestones(store, profile_id):
    from .organization import coverage

    structure = coverage(store)
    with store.db.engine.connect() as c:
        revisions = rows(c, "SELECT s.current_revision_id FROM sources s WHERE s.kind='PDF' AND s.deleted_at IS NULL")
    for source in revisions:
        state = readiness(store, source['current_revision_id'], profile_id=profile_id, structure=structure)
        if not state['job_id']:
            continue
        for scope in [state['coverage'], *state['chapters']]:
            if scope['status'] != 'READY':
                continue
            with store.repository.transaction() as c:
                old = one(c, "SELECT 1 FROM job_stage_events WHERE job_id=:j AND stage='atomic-index-ready' AND json_extract(detail_json,'$.scope_key')=:k AND json_extract(detail_json,'$.manifest_sha256')=:m LIMIT 1", j=state['job_id'], k=scope['key'], m=scope['manifest_sha256'])
                if not old:
                    insert(c, 'job_stage_events', id=identifier(), job_id=state['job_id'], stage='atomic-index-ready', detail_json=canonical({'scope_key': scope['key'], 'manifest_sha256': scope['manifest_sha256'], 'generation': state['atomic_index']['generation']}), created_at=now())
