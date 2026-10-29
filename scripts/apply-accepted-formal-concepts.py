"""Apply the specifically authorized 2026-10-27 textbook transfer, with a backup."""

import argparse
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.adapters.llm import OpenAIEmbedding
from noodle_agent.config import Settings
from noodle_agent.next.concepts.maintenance_bundle import atomic_heads, enqueue
from noodle_agent.next.contracts import Candidate, canonical
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.navigation import frozen_candidate
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store

TARGET = ROOT / 'data/knowledge-formal-20261025T043825Z'
SOURCE = ROOT / '.local-only/concept-maintenance-20261027-neural/library'
BUNDLE = ROOT / '.local-only/concept-maintenance-20261027-promotion-rehearsal/transfer-bundle.json'
EXPECTED = '452a373fb13a763cdef6a68a9a6b7a7f5e58fe2db887b205f08afcd7aa8cdcae'
RUN = ROOT / '.local-only/concept-formal-promotion-20261027'


def write(name, value):
    (RUN / name).write_text(canonical(value), encoding='utf-8')


def forbid(request):
    raise Conflict('Accepted formal transfer forbids provider calls: ' + request['operation'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['apply', 'index'])
    args = parser.parse_args()
    bundle = json.loads(BUNDLE.read_text(encoding='utf-8'))
    if bundle['bundle_sha256'] != EXPECTED:
        raise Conflict('This command only accepts the explicitly authorized bundle')
    RUN.mkdir(exist_ok=True)
    if args.phase == 'apply' and not (RUN / 'transfer-receipt.json').exists():
        spec = importlib.util.spec_from_file_location('maintenance', ROOT / 'scripts/concept-maintenance.py')
        maintenance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(maintenance)
        maintenance.prepare(TARGET.resolve(), RUN / 'before')
    source, target = Store(SOURCE), Store(TARGET)
    guard = call_guard.set(forbid)
    try:
        with target.db.engine.connect() as c:
            calls_before = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()
            if atomic_heads(target, c) != bundle['atomics']:
                raise Conflict('Frozen textbook Atomic heads differ')
        if args.phase == 'apply':
            job_id = enqueue(target, bundle)
            outcome = Application(target, index_mode='KEYWORD_ONLY', job_kinds=('APPLY_CONCEPT_MAINTENANCE',)).run_once('authorized-formal-transfer')
            if outcome is None:
                with target.db.engine.connect() as c:
                    job = one(c, 'SELECT status,result_json FROM jobs WHERE id=:id', id=job_id)
                outcome = {'status': job['status'], 'result': json.loads(job['result_json'])}
            if outcome['status'] != 'SUCCEEDED':
                write('transfer-failure.json', outcome)
                raise Conflict('Formal transfer failed; transaction and failure receipt retained')
            receipt = outcome['result']
            write('transfer-receipt.json', receipt)
            for item in bundle['publications']:
                current = target.get_note(item['note']['id'])
                if frozen_candidate(target, current['revision']['id']).fingerprint != Candidate.model_validate(item['proof']['candidate']).fingerprint:
                    raise Conflict('Formal publication differs from accepted candidate')
            result = {'status': 'APPLIED', 'publications': len(receipt['published']),
                'retired': len(receipt['retired']), 'routes': len(bundle['routes']), 'bundle_sha256': EXPECTED}
        else:
            if not (RUN / 'transfer-receipt.json').exists():
                raise Conflict('Apply and verify accepted publications before indexing')
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
                raise Conflict('Uncached formal index text; no embedding call is permitted')

            provider.embed = no_embedding
            settings = replace(settings, embedding_api_key=None, llm_api_key=None, vision_api_key=None, mineru_api_token=None)
            outcomes = []
            for kinds in [('UPDATE_MAPS',), ('INDEX_NOTE',), ('BUILD_INDEX',)]:
                app = Application(target, settings=settings, embedding=provider, index_mode='HYBRID', job_kinds=kinds)
                for _ in range(500):
                    step = app.run_once('authorized-formal-transfer-index')
                    if step is None:
                        break
                    outcomes.append(step)
                    if step['status'] != 'SUCCEEDED':
                        write('transfer-index-failure.json', step)
                        raise Conflict('Formal index failed')
            with target.db.engine.connect() as c:
                valid = eligible(c)
            generation, manifest, matrix = app.indexer.snapshot('HYBRID')
            if not generation or matrix is None or {n['revision_id'] for n in valid} != {m['revision_id'] for m in manifest}:
                raise Conflict('Formal index differs from eligible knowledge')
            result = {'status': 'INDEX_VERIFIED', 'generation': generation['id'], 'chunks': len(manifest),
                'notes': len(valid), 'cached_vectors_copied': copied, 'worker_steps': len(outcomes)}
        with target.db.engine.connect() as c:
            if atomic_heads(target, c) != bundle['atomics']:
                raise Conflict('Transfer changed frozen textbook Atomic heads')
            calls = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one() - calls_before
        if calls:
            raise Conflict('Unexpected provider audit during offline transfer')
        result.update(actual_provider_calls=calls, atomic_unchanged=True)
        write('transfer-' + args.phase + '.json', result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        call_guard.reset(guard)
        source.close()
        target.close()


if __name__ == '__main__':
    main()
