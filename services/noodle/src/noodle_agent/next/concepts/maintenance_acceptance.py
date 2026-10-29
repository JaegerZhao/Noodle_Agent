"""Read-back, actual embedding-cache preflight and current index activation."""

import json
import time

from ..contracts import Candidate, Review, sha
from ..eligibility import eligible
from ..knowledge_roots import roots, tree
from ..repository import Conflict, one, rows
from .maintenance_run import write


def audit(store, run):
    baseline = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))['baseline']
    atomic_expected = {n['id']: (n['current_revision_id'], n['markdown_sha256']) for n in baseline['heads'] if n['kind'] == 'ATOMIC'}
    with store.db.engine.connect() as c:
        atomics = {n['id']: (n['current_revision_id'], n['markdown_sha256']) for n in rows(c, "SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC'")}
        current = eligible(c)
        invalid = [dict(r) for r in rows(c, "SELECT n.id,n.title FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='CONCEPT' AND n.visibility='ACTIVE' AND r.status='PUBLISHED'") if r['id'] not in {n['id'] for n in current}]
        jobs = [dict(r) for r in rows(c, "SELECT id,kind,status,error_json FROM jobs WHERE rowid>:base AND status='REVIEW'", base=baseline['job_rowid'])]
        integrity = c.exec_driver_sql('PRAGMA integrity_check').scalar_one()
        foreign_keys = c.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
    proof_errors, concepts = [], []
    for note in current:
        if note['kind'] != 'CONCEPT':
            continue
        candidate = None
        try:
            ref = f"artifacts/builds/{note['revision_id']}.json"
            with store.db.engine.connect() as c:
                proof = one(c, 'SELECT sha256 FROM build_artifacts WHERE storage_ref=:ref', ref=ref)
                raw = json.loads(store.read(ref, proof['sha256']))
                candidate, review = Candidate.model_validate(raw['candidate']), Review.model_validate(raw['review'])
                if candidate.fingerprint != review.candidate_sha256 or not review.passed:
                    raise ValueError('Review fingerprint differs')
                store.publisher.validate_inputs(c, note, candidate)
            concepts.append({'id': note['id'], 'title': note['title'], 'revision_id': note['revision_id'],
                'chars': sum(len(s.markdown) for s in candidate.sections),
                'sections': [s.title for s in candidate.sections], 'foundation': review.report.get('foundation', {}).get('state')})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            proof_errors.append({'id': note['id'], 'detail': str(exc)})
    root_values = roots(store)
    all_tree = tree(store, 'all', limit=3000)
    found = {e['concept_id'] for e in all_tree['entries']}
    expected = {n['id'] for n in current if n['kind'] == 'CONCEPT'}
    result = {'atomic_unchanged': atomics == atomic_expected, 'atomic_count': len(atomics),
        'integrity': integrity, 'foreign_key_errors': [list(r) for r in foreign_keys],
        'proof_errors': proof_errors, 'invalid_published_concepts': invalid, 'review_jobs': jobs,
        'tree_complete': found == expected and len(all_tree['entries']) == len(found),
        'concepts': concepts, 'roots': root_values['items']}
    result['engineering_passed'] = result['atomic_unchanged'] and integrity == 'ok' and not foreign_keys and not proof_errors and not invalid and result['tree_complete']
    write(run / 'engineering-acceptance.json', result)
    return result


def index(store, run, *, allow_embeddings=False, embedding_budget=None):
    from dataclasses import replace

    from ...adapters.llm import OpenAIEmbedding
    from ...config import Settings
    from ..retrieval.chunks import compile_chunks
    from ..retrieval.vectors import profile_for
    from ..runtime import Application

    frozen = json.loads((run / 'change-plan.json').read_text(encoding='utf-8'))
    settings = Settings.from_env()
    provider = OpenAIEmbedding(settings)
    profile = profile_for(provider, False)
    missing, atomic_missing, chunks = set(), set(), 0
    with store.db.engine.connect() as c:
        notes = eligible(c)
    for note in notes:
        for chunk in compile_chunks(store, store.get_note(note['id'])):
            chunks += 1
            digest = sha(chunk['embedding_text'])
            ref = f"vectors/cache/{profile['id']}/{digest}.npy"
            if not store.path(ref).exists() or not store.path(ref + '.sha256').exists():
                missing.add(digest)
                if note['kind'] == 'ATOMIC':
                    atomic_missing.add(digest)
    preflight = {'profile_id': profile['id'], 'chunks': chunks, 'missing_texts': len(missing),
                 'atomic_missing_texts': len(atomic_missing), 'cached_texts': chunks - len(missing)}
    write(run / 'index-preflight.json', preflight)
    limit = frozen['budget']['embedding_missing_texts']
    if embedding_budget is not None:
        if embedding_budget < 0:
            raise ValueError('Embedding budget must be nonnegative')
        write(run / 'index-budget-amendment.json', {'plan_sha256': frozen['plan_sha256'],
            'original_limit': limit, 'new_limit': embedding_budget, 'preflight': preflight,
            'reason': 'Explicit index budget after deterministic formatting and accepted Concept updates'})
        limit = embedding_budget
    if len(missing) > limit:
        raise Conflict('Missing embeddings exceed the frozen run budget')
    if missing and not allow_embeddings:
        raise ValueError('Use --allow-embeddings for the planned new Concept texts')
    settings = replace(settings, vision_api_key=None, mineru_api_token=None)
    outcomes, started = [], time.perf_counter()
    for mode in ('KEYWORD_ONLY', 'HYBRID'):
        # Drain invalidation requests first, then build each active mode once.
        if mode == 'KEYWORD_ONLY':
            app = Application(store, settings=replace(settings, embedding_api_key=None), index_mode=mode, job_kinds=('INDEX_NOTE',))
            for _ in range(500):
                outcome = app.run_once('concept-index-requests')
                if outcome is None:
                    break
                outcomes.append(outcome)
        from ..retrieval.indexing import request_index
        request_index(store, 'concept-maintenance:' + run.name + ':' + mode)
        app = Application(store, settings=settings, index_mode=mode, job_kinds=('BUILD_INDEX',))
        for _ in range(100):
            outcome = app.run_once('concept-index-activate')
            if outcome is None:
                break
            outcomes.append(outcome)
    result = {**preflight, 'elapsed_seconds': time.perf_counter() - started, 'outcomes': outcomes,
              'passed': all(o['status'] == 'SUCCEEDED' for o in outcomes)}
    write(run / 'index-audit.json', result)
    return result


def repair(store, run, requests):
    """Located source-based corrections use the normal worker and one repair budget."""
    from dataclasses import replace

    from ...config import Settings
    from ..provider_limits import call_guard
    from ..runtime import Application
    from .coordinator import request_repair
    from .maintenance_run import Budget

    frozen = json.loads((run / 'change-plan.json').read_text(encoding='utf-8'))
    for request in requests:
        request_repair(store, request['concept_id'], request['expected_revision'], request['findings'])
    settings = replace(Settings.from_env(), vision_api_key=None, mineru_api_token=None,
                       embedding_api_key=None, llm_max_retries=0)
    budget = Budget(store, run, frozen)
    token = call_guard.set(budget.charge)
    outcomes, started = [], time.perf_counter()
    try:
        app = Application(store, settings=settings, index_mode='KEYWORD_ONLY', job_kinds=('BUILD_CONCEPT',))
        for _ in range(100):
            outcome = app.run_once('concept-located-repair')
            if outcome is None:
                break
            outcomes.append(outcome)
            if budget.blocked:
                break
    finally:
        call_guard.reset(token)
    result = {'requests': requests, 'outcomes': outcomes, 'elapsed_seconds': time.perf_counter() - started,
        'budget_stopped': budget.blocked, 'passed': bool(outcomes) and all(o['status'] == 'SUCCEEDED' for o in outcomes)}
    write(run / 'located-repair.json', result)
    return result
