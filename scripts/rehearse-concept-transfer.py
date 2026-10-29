"""Rehearse checked Concept transfer and rollback only in a prepared isolated library."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.adapters.llm import OpenAIEmbedding
from noodle_agent.config import Settings
from noodle_agent.next.concepts.maintenance_bundle import (
    atomic_heads,
    enqueue,
    prepare,
    rollback,
)
from noodle_agent.next.contracts import Candidate, canonical
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def write(path, value):
    path.write_text(canonical(value), encoding='utf-8')


def forbid(request):
    raise Conflict('Transfer rehearsal attempted an external call: ' + request['operation'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['plan', 'apply', 'rollback', 'index'])
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target-run', required=True, type=Path)
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--retry', action='store_true', help='Retry a stopped transfer job')
    args = parser.parse_args()
    run = args.target_run.resolve()
    source_path = args.source.resolve()
    isolated = (ROOT / '.local-only').resolve()
    if not run.is_relative_to(isolated) or not source_path.is_relative_to(isolated):
        raise ValueError('Both rehearsal source and target must be inside workspace .local-only')
    if not (run / 'prepared.json').is_file() or not (run / 'baseline.json').is_file():
        raise ValueError('Target must be a separately prepared rehearsal run')
    target_path = (run / 'library').resolve()
    if target_path == source_path or not target_path.is_relative_to(run):
        raise ValueError('Rehearsal requires an independent target copy')
    source, target = Store(source_path), Store(target_path)
    try:
        if args.phase == 'plan':
            if not args.acceptance:
                parser.error('--acceptance is required')
            acceptance = json.loads(args.acceptance.read_text(encoding='utf-8'))
            bundle = prepare(source, target, acceptance=acceptance)
            destination = run / 'transfer-bundle.json'
            if destination.exists() and json.loads(destination.read_text(encoding='utf-8')) != bundle:
                raise Conflict('Changed rehearsal inputs require a new prepared run')
            write(destination, bundle)
            result = {'status': 'PLANNED', 'isolated_only': True,
                'publications': len(bundle['publications']), 'retired': len(bundle['retired']),
                'routes': len(bundle['routes']), 'model_calls': 0, 'bundle_sha256': bundle['bundle_sha256']}
        else:
            bundle = json.loads((run / 'transfer-bundle.json').read_text(encoding='utf-8'))
            if args.phase == 'index':
                from noodle_agent.next.eligibility import eligible
                copied = 0
                for path in sorted(source.path('vectors/cache').rglob('*.npy')):
                    ref = path.relative_to(source.root).as_posix()
                    digest = source.path(ref + '.sha256').read_text(encoding='utf-8')
                    data = source.read(ref, digest)
                    if not target.path(ref).exists():
                        target.put(ref, data)
                        target.put(ref + '.sha256', digest.encode())
                        copied += 1
                settings = Settings.from_env()
                provider = OpenAIEmbedding(settings)
                def no_embedding(texts):
                    raise Conflict('Rehearsal index has uncached text; no embedding call is permitted')
                provider.embed = no_embedding
                settings = replace(settings, embedding_api_key=None, llm_api_key=None, vision_api_key=None, mineru_api_token=None)
                outcomes = []
                for kinds in [('UPDATE_MAPS',), ('INDEX_NOTE',), ('BUILD_INDEX',)]:
                    app = Application(target, settings=settings, embedding=provider, index_mode='HYBRID', job_kinds=kinds)
                    for _ in range(500):
                        step = app.run_once('transfer-index-rehearsal')
                        if step is None:
                            break
                        outcomes.append(step)
                        if step['status'] != 'SUCCEEDED':
                            write(run / 'transfer-index-failure.json', step)
                            raise Conflict('Rehearsal index failed')
                with target.db.engine.connect() as c:
                    valid = eligible(c)
                generation, manifest, matrix = app.indexer.snapshot('HYBRID')
                if not generation or matrix is None or {n['revision_id'] for n in valid} != {m['revision_id'] for m in manifest}:
                    raise Conflict('Active index differs from current eligible knowledge')
                # Reading every cache entry through the indexer already checks its
                # checksum, dimensions and finite vectors. No provider was called.
                result = {'status': 'INDEX_VERIFIED', 'generation': generation['id'], 'chunks': len(manifest),
                    'notes': len(valid), 'cached_vectors_copied': copied, 'worker_steps': len(outcomes), 'model_calls': 0}
            elif args.phase == 'rollback':
                receipt = json.loads((run / 'transfer-receipt.json').read_text(encoding='utf-8'))
                result = rollback(target, bundle, receipt)
            else:
                with target.db.engine.connect() as c:
                    calls_before = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()
                job_id = enqueue(target, bundle)
                if args.retry:
                    with target.db.engine.connect() as c:
                        state = one(c, 'SELECT status FROM jobs WHERE id=:id', id=job_id)['status']
                    if state == 'REVIEW':
                        target.jobs.retry_review(job_id)
                guard = call_guard.set(forbid)
                try:
                    outcome = Application(target, index_mode='KEYWORD_ONLY', job_kinds=('APPLY_CONCEPT_MAINTENANCE',)).run_once('isolated-transfer-rehearsal')
                finally:
                    call_guard.reset(guard)
                if outcome is None:
                    with target.db.engine.connect() as c:
                        job = one(c, 'SELECT status,result_json FROM jobs WHERE id=:id', id=job_id)
                    outcome = {'status': job['status'], 'result': json.loads(job['result_json'])}
                if outcome['status'] != 'SUCCEEDED':
                    write(run / 'transfer-failure.json', outcome)
                    raise Conflict('Rehearsal failed; see transfer-failure.json')
                receipt = outcome['result']
                write(run / 'transfer-receipt.json', receipt)
                for item in bundle['publications']:
                    current = target.get_note(item['note']['id'])
                    if frozen_candidate(target, current['revision']['id']).fingerprint != Candidate.model_validate(item['proof']['candidate']).fingerprint:
                        raise Conflict('Published artifact differs from accepted candidate')
                with target.db.engine.connect() as c:
                    if atomic_heads(target, c) != bundle['atomics']:
                        raise Conflict('Rehearsal changed Atomic heads')
                    calls = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one() - calls_before
                result = {'status': 'APPLIED', 'isolated_only': True, 'publications': len(receipt['published']),
                    'retired': len(receipt['retired']), 'actual_provider_calls': calls, 'atomic_unchanged': True}
        write(run / ('transfer-' + args.phase + '.json'), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        source.close()
        target.close()


if __name__ == '__main__':
    main()
