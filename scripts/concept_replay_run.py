"""Ordered replay orchestration; every semantic operation stays in native workers."""

import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf8')
    temporary.replace(path)


def prepare(base, run, selected=None):
    sys.path.insert(0, str(ROOT / 'services/noodle/src'))
    from concept_validation_replay import import_atomics
    from noodle_agent.next.concepts.replay_inputs import ordered_inputs
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.store import Store
    if run.exists():
        raise ValueError('Use resume/report for an existing replay; never replace it')
    if not run.is_relative_to((ROOT / '.local-only').resolve()) or run == base:
        raise ValueError('Replay target must be an isolated .local-only directory')
    inputs = ordered_inputs(base)
    if selected is not None:
        wanted = set(selected)
        for source in inputs['sources']:
            source['atomics'] = [n for n in source['atomics'] if n['note_id'] in wanted]
        found = {n['note_id'] for s in inputs['sources'] for n in s['atomics']}
        if found != wanted:
            raise ValueError('Selected inputs are not all current eligible Atomics')
        inputs['atomic_count'] = len(wanted)
        inputs['selection'] = sorted(wanted)
    run.mkdir(parents=True)
    dump(run / 'input-manifest.json', inputs)
    files = {}
    for path in (ROOT / 'services/noodle/src').rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(ROOT)
        target = run / 'execution-code' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files[relative.as_posix()] = sha(path.read_bytes())
    for name in ['concept_replay_run.py', 'concept_validation_replay.py', 'validate-concept-replay.py']:
        shutil.copy2(ROOT / 'scripts' / name, run / name)
    dump(run / 'execution-freeze.json', {'files': files, 'created_at': datetime.now(UTC).isoformat(),
        'base': str(base), 'driver_sha256': sha((ROOT / 'scripts/concept_replay_run.py').read_bytes()),
        'scope': 'Concept-only; no MinerU, Atomic writing, vision or answer generation'})
    receipt = import_atomics(base, run / 'library', Store, sha, note_ids=selected)
    dump(run / 'atomic-replay-manifest.json', receipt)
    cache = base / 'vectors/cache'
    vector_files = []
    if cache.exists():
        for path in cache.rglob('*'):
            if not path.is_file() or not (path.name.endswith('.npy') or path.name.endswith('.npy.sha256')):
                continue
            destination = run / 'library' / path.relative_to(base)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            digest = sha(path.read_bytes())
            if sha(destination.read_bytes()) != digest:
                raise ValueError('Vector cache copy differs')
            vector_files.append({'ref': path.relative_to(base).as_posix(), 'sha256': digest})
    dump(run / 'vector-cache-import.json', {'files': vector_files, 'network_calls': 0})
    # The immutable input is verified first. Activation in this disposable target
    # controls arrival order, not the production eligibility/publication policy.
    with sqlite3.connect(run / 'library/noodle.db') as connection:
        count = connection.execute("SELECT count(*) FROM notes WHERE kind='CONCEPT'").fetchone()[0]
        if count or connection.execute('SELECT count(*) FROM atomic_entity_routes').fetchone()[0]:
            raise ValueError('Cold target contains old Concept answers')
        connection.execute("UPDATE notes SET visibility='WITHDRAWN' WHERE kind='ATOMIC'")
        connection.execute('UPDATE storage_contract SET validity_epoch=validity_epoch+1 WHERE id=1')
    dump(run / 'progress.json', {'completed_batches': [], 'sources': {}, 'status': 'PREPARED', 'network_calls': 0})
    return {'root': str(run), 'prepared': True, 'atomics': inputs['atomic_count']}


def bootstrap(run):
    execution = json.loads((run / 'execution-freeze.json').read_text(encoding='utf8'))
    sys.path.insert(0, str(run / execution.get('active_code','execution-code') / 'services/noodle/src'))
    # Explicit target settings win over .env defaults and cannot point at formal data.
    os.environ['NOODLE_DATA_DIR'] = str(run / 'library')
    os.environ['NOODLE_DATABASE_URL'] = 'sqlite:///' + str(run / 'library/noodle.db')
    os.environ['NOODLE_EXPORT_DIR'] = str(run / 'exports')
    from noodle_agent.config import Settings
    from noodle_agent.next.store import Store
    settings = replace(Settings.from_env(ROOT), mineru_api_token=None, vision_api_key=None)
    if settings.data_dir != run / 'library':
        raise ValueError('Replay configuration escaped the selected target')
    store = Store(run / 'library')
    return store, settings


def upgrade_execution(run, reason):
    """A new frozen generation at a stopped checkpoint; retain every old byte."""
    if not reason.strip():
        raise ValueError('Code upgrade requires a recorded reason')
    previous = json.loads((run / 'execution-freeze.json').read_text(encoding='utf8'))
    from hashlib import sha256
    files = {p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest()
             for p in (ROOT/'services/noodle/src').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    digest = sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
    directory = 'execution-code-'+digest[:16]
    generations = previous.get('generations', [{'directory':previous.get('active_code','execution-code'),'files':previous['files']}])
    for item in generations:
        if any(sha256((run/item['directory']/ref).read_bytes()).hexdigest()!=expected for ref,expected in item['files'].items()):
            raise ValueError('Historical execution freeze differs')
    if previous.get('active_code')==directory:
        return previous
    for ref in files:
        target=run/directory/ref
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/ref,target)
    dump(run/'execution-history'/('generation-'+str(len(generations))+'.json'),previous)
    generations.append({'directory':directory,'files':files,'reason':reason,'created_at':datetime.now(UTC).isoformat()})
    value={**previous,'files':files,'active_code':directory,'generations':generations}
    dump(run/'execution-freeze.json',value)
    return value


def retry_review(run, identities):
    store,_=bootstrap(run)
    try:
        for identity in identities:
            store.jobs.retry_review(identity)
    finally:
        store.close()


def run_replay(run):
    store, settings = bootstrap(run)
    from noodle_agent.next.contracts import canonical, sha
    from noodle_agent.next.eligibility import eligible
    from noodle_agent.next.provider_limits import call_guard
    from noodle_agent.next.repository import rows
    from noodle_agent.next.runtime import Application
    def provider_scope(request):
        if not request['operation'].startswith(('next.entity_', 'next.concept_', 'next.organization_')):
            raise RuntimeError('Ordered Concept replay forbids provider operation: '+request['operation'])
    guard_token = call_guard.set(provider_scope)
    inputs = json.loads((run / 'input-manifest.json').read_text(encoding='utf8'))
    progress = json.loads((run / 'progress.json').read_text(encoding='utf8'))
    apps = {name: Application(store, settings=settings, job_kinds=kinds) for name, kinds in [
        ('routing', ['ANALYZE_ATOMIC']), ('fusion', ['BUILD_CONCEPT', 'RECONCILE_DEPENDENCIES', 'MAINTAIN_CONCEPT_STRUCTURE']),
        ('organization', ['REVIEW_ORGANIZATION', 'UPDATE_MAPS']), ('index', ['INDEX_NOTE', 'BUILD_INDEX'])]}
    # Immutable vector bytes may be reused only through the indexer's own exact
    # profile/text cache. Copy their files/table with a verified helper separately.
    started = time.monotonic()

    def observe(value):
        with (run / 'timeline.jsonl').open('a', encoding='utf8') as stream:
            stream.write(canonical({'observed_at': datetime.now(UTC).isoformat(), **value})+'\n')
        print(json.dumps(value, ensure_ascii=False, default=str), flush=True)

    def drain(name, source_index, chapter):
        app = apps[name]
        phase_start = time.monotonic()
        while True:
            result = app.run_once('ordered-replay-'+name)
            if result is not None:
                observe({'source_index': source_index, 'chapter': chapter, 'phase': name, **result})
                if result.get('status') == 'REVIEW':
                    detail = canonical(result)
                    if any(s in detail for s in ['Insufficient Balance', 'AccountOverdueError', 'insufficient_quota', 'AuthenticationError']):
                        raise RuntimeError('Provider dependency stopped replay; inspect recorded failure and resume')
                    if name in {'fusion', 'index'}:
                        raise RuntimeError('Chapter publication/index failed; retain this checkpoint before adding later inputs')
                continue
            with store.db.engine.connect() as c:
                pending = [dict(r) for r in rows(c, "SELECT id,kind,status FROM jobs WHERE status IN ('PENDING','RETRY','RUNNING')") if r['kind'] in app.job_kinds]
            if not pending:
                break
            observe({'phase': name, 'chapter': chapter, 'waiting_jobs': pending})
            time.sleep(2)
        return round(time.monotonic()-phase_start, 3)

    try:
        progress['status'] = 'RUNNING'
        # A stopped run may have one explicitly retried old routing decision.
        # Settle that recovery before admitting any new input batch.
        for name in apps:
            drain(name, -1, 'checkpoint-recovery')
        with store.db.engine.connect() as c:
            unresolved = [dict(r) for r in rows(c,"SELECT atomic_revision_id,status FROM atomic_entity_routes WHERE status='REVIEW'")]
        if unresolved:
            raise RuntimeError('Unresolved routing checkpoint requires targeted recovery before new inputs: '+canonical(unresolved))
        for source in inputs['sources']:
            if not source['atomics']:
                continue
            grouped = {}
            for note in source['atomics']:
                key = note['chapter_key'] if source['kind']=='BOOK' else 'source'
                grouped.setdefault(key, []).append(note)
            for chapter, members in grouped.items():
                batch_key = sha(canonical([source['source_revision_id'], chapter, [(n['note_id'], n['revision_id']) for n in members]]))
                if batch_key in progress['completed_batches']:
                    continue
                with store.repository.transaction() as c:
                    for note in members:
                        row = c.exec_driver_sql('SELECT current_revision_id FROM notes WHERE id=?', (note['note_id'],)).scalar_one()
                        if row != note['revision_id']:
                            raise ValueError('Frozen input head changed')
                        c.exec_driver_sql("UPDATE notes SET visibility='ACTIVE' WHERE id=?", (note['note_id'],))
                    store.repository.advance_epoch(c)
                    batch = {'source_revision_id': source['source_revision_id'],
                             'atomics': [{'note_id': n['note_id'], 'revision_id': n['revision_id']} for n in members],
                             'input_hash': batch_key}
                    store.repository.event(c, 'ATOMIC_BATCH_CLOSED', source['source_revision_id'], source['source_revision_id'],
                        {'routing_batch': batch}, key='ordered-replay:'+batch_key)
                    for note in members:
                        store.repository.event(c, 'NOTE_PUBLISHED', note['note_id'], note['revision_id'],
                            {'routing_batch': batch}, key='ordered-replay-published:'+note['revision_id'])
                times = {name: drain(name, source['position'], chapter) for name in apps}
                from noodle_agent.next.pipeline_state import indexed_revisions
                with store.db.engine.connect() as c:
                    effective = eligible(c)
                covered, _ = indexed_revisions(store, effective, profile_id=apps['index'].indexer.profile['id'])
                if len(covered) != len(effective):
                    raise RuntimeError('Chapter current revisions are not fully indexed; next batch was not injected')
                with store.db.engine.connect() as c:
                    routes = [dict(r) for r in rows(c, 'SELECT * FROM atomic_entity_routes')]
                    heads = [dict(r) for r in rows(c, "SELECT id,title,current_revision_id FROM notes WHERE kind='CONCEPT' AND visibility='ACTIVE'")]
                    index = [dict(r) for r in rows(c, 'SELECT * FROM index_heads')]
                    usage = [dict(r) for r in rows(c, 'SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens FROM provider_calls GROUP BY kind,operation,model,status')]
                destination = run / 'snapshots' / (str(source['position'])+'-'+str(len(progress['completed_batches']))+'-'+batch_key[:10]+'.json')
                dump(destination, {'source': source['title'], 'chapter': chapter, 'inputs': members, 'times': times,
                    'routes': routes, 'concepts': heads, 'index_heads': index, 'usage': usage})
                route_revisions = {r['atomic_revision_id'] for r in routes if r['status'] in {'ACCEPTED','SKIPPED'}}
                if not {n['revision_id'] for n in members} <= route_revisions:
                    raise RuntimeError('Current input batch has unresolved/unconsumed routing; do not inject later sources')
                progress['completed_batches'].append(batch_key)
                dump(run / 'progress.json', progress)
            progress['sources'][source['source_revision_id']] = {'status': 'COMPLETED', 'title': source['title']}
            dump(run / 'progress.json', progress)
        progress['status'] = 'COMPLETED'
        progress['latest_elapsed_s'] = round(time.monotonic()-started, 3)
        dump(run / 'progress.json', progress)
        return report(run, store)
    except Exception as exc:
        progress['status'] = 'STOPPED'
        progress['error'] = {'type': type(exc).__name__, 'detail': str(exc)}
        dump(run / 'progress.json', progress)
        raise
    finally:
        call_guard.reset(guard_token)
        store.close()


def report(run, store=None):
    owned = store is None
    if owned:
        store, _ = bootstrap(run)
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.eligibility import eligible
    from noodle_agent.next.pipeline_state import indexed_revisions
    from noodle_agent.next.repository import rows
    frozen = json.loads((run / 'atomic-replay-manifest.json').read_text(encoding='utf8'))
    with store.db.engine.connect() as c:
        valid = eligible(c)
        routes = [dict(r) for r in rows(c, 'SELECT * FROM atomic_entity_routes')]
        jobs = [dict(r) for r in rows(c, "SELECT id,kind,status,error_json FROM jobs WHERE status!='SUCCEEDED'")]
        usage = [dict(r) for r in rows(c, 'SELECT kind,operation,model,status,count(*) calls,sum(total_tokens) total_tokens FROM provider_calls GROUP BY kind,operation,model,status')]
        integrity = c.exec_driver_sql('PRAGMA integrity_check').scalar_one()
        foreign_keys = [list(r) for r in c.exec_driver_sql('PRAGMA foreign_key_check')]
        decisions = [dict(r) for r in rows(c, 'SELECT * FROM contribution_decisions')]
        heads = {r['id']: dict(r) for r in rows(c, "SELECT id,current_revision_id FROM notes WHERE kind='ATOMIC'")}
    execution = json.loads((run / 'execution-freeze.json').read_text(encoding='utf8'))
    base = Path(execution['base'])
    changed_files = [item['directory']+'/'+ref for item in execution.get('generations',
        [{'directory':execution.get('active_code','execution-code'),'files':execution['files']}])
        for ref,digest in item['files'].items() if sha((run/item['directory']/ref).read_bytes())!=digest]
    unchanged = all(heads.get(n['id'], {}).get('current_revision_id')==n['current_revision_id'] and
        all(sha((root / n['markdown_ref']).read_bytes())==n['markdown_sha256'] for root in [base, store.root])
        for n in frozen['atomics'])
    concepts = [n for n in valid if n['kind']=='CONCEPT']
    indexed, index_head = indexed_revisions(store, valid)
    current_concepts = {n['id']: n['revision_id'] for n in concepts}
    by_revision = {n['current_revision_id']: n for n in frozen['atomics']}
    coverage = []
    for route in routes:
        related = [d for d in decisions if d['atomic_revision_id']==route['atomic_revision_id']]
        issues = []
        if route['status'] not in {'ACCEPTED', 'SKIPPED'}:
            issues.append('UNRESOLVED_ROUTE')
        for d in related:
            fragment = json.loads(d['fragment_json'])
            source = by_revision.get(d['atomic_revision_id'])
            if not source:
                issues.append('OUTSIDE_FROZEN_INPUT')
                continue
            markdown = store.path(source['markdown_ref']).read_text(encoding='utf8')
            if sha(markdown[fragment['start_offset']:fragment['end_offset']]) != fragment['sha256']:
                issues.append('FRAGMENT_HASH_DIFFERS')
            if d['status']=='ACCEPTED' and d['action']!='NO_CHANGE' and (not d['applied_revision_id'] or d['concept_id'] not in current_concepts or d['applied_revision_id']!=current_concepts.get(d['concept_id'])):
                issues.append('ACCEPTED_CONTRIBUTION_NOT_CURRENT')
        coverage.append({'atomic_revision_id': route['atomic_revision_id'], 'status': route['status'],
                         'decisions': len(related), 'issues': issues, 'passed': not issues})
    no_other_calls = all(u['kind']=='EMBEDDING_CALL' or u['kind']=='LLM_CALL' and
                        u['operation'].startswith(('next.entity_', 'next.concept_', 'next.organization_')) for u in usage)
    images = sum(bool('![' in store.path(n['markdown_ref']).read_text(encoding='utf8')) for n in concepts)
    result = {'scope': 'ORDERED_CONCEPT_REPLAY', 'input_atomics': len(frozen['atomics']),
        'routed_atomics': len({r['atomic_revision_id'] for r in routes}), 'accepted_routes': sum(r['status']=='ACCEPTED' for r in routes),
        'effective_concepts': len(concepts), 'concepts_with_images': images, 'atomic_bodies_unchanged': unchanged,
        'integrity': integrity, 'unfinished_jobs': jobs, 'usage': usage}
    result.update({'freeze_passed': not changed_files, 'changed_frozen_files': changed_files,
                   'only_concept_provider_calls': no_other_calls, 'foreign_key_violations': foreign_keys,
                   'coverage': coverage, 'indexed_current_revisions': len(indexed),
                   'effective_current_revisions': len(valid), 'index_generation': index_head['id'] if index_head else None})
    result['passed'] = bool(concepts) and unchanged and not changed_files and no_other_calls and integrity=='ok' and not foreign_keys and all(x['passed'] for x in coverage) and len(indexed)==len(valid) and result['routed_atomics']==result['input_atomics'] and not jobs
    dump(run / 'report.json', result)
    if owned:
        store.close()
    return result
