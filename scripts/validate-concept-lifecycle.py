"""Real cached-vector lifecycle on an independent prepared Concept library; no LLM."""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.adapters.llm import OpenAIEmbedding
from noodle_agent.config import Settings
from noodle_agent.next.audit import RequestAudit
from noodle_agent.next.availability import target_state
from noodle_agent.next.contracts import sha
from noodle_agent.next.graph_projection import graph
from noodle_agent.next.repository import (
    Conflict,
    InvalidKnowledge,
    one,
    rows,
)
from noodle_agent.next.retrieval.contracts import Query, Scope
from noodle_agent.next.retrieval.vectors import profile_for
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


class ForbidModel:
    name = 'program-forbid-unplanned-llm'

    def structured_json(self, **kwargs):
        raise Conflict('Lifecycle made an unexpected LLM request: ' + kwargs['operation'])


class CachedQueries:
    def __init__(self, provider):
        self.provider, self.values = provider, {}

    def __getattr__(self, key):
        return getattr(self.provider, key)

    def warm(self, queries):
        values = self.provider.embed(queries)
        self.values.update(zip(queries, values, strict=True))

    def embed(self, texts):
        if any(t not in self.values for t in texts):
            raise Conflict('Lifecycle attempted a new, unplanned embedding text')
        return [self.values[t] for t in texts]


def drain(store, settings, vectors, kinds):
    app = Application(store, settings=settings, embedding=vectors, agent=ForbidModel(),
                      index_mode='HYBRID', job_kinds=kinds)
    result = []
    for _ in range(500):
        step = app.run_once('real-concept-lifecycle')
        if step is None:
            return result
        result.append(step)
        if step['status'] != 'SUCCEEDED':
            raise Conflict('Lifecycle worker failed: ' + json.dumps(step, ensure_ascii=False))
    raise Conflict('Lifecycle did not settle')


def validate(run, concept_id):
    run = run.resolve()
    if not run.is_relative_to((ROOT / '.local-only').resolve()):
        raise ValueError('Lifecycle only runs in an independent .local-only library')
    saved = json.loads((run / 'baseline.json').read_text(encoding='utf-8'))
    store = Store(run / 'library')
    observations, operations = [], []
    try:
        with store.db.engine.connect() as c:
            concept = one(c, 'SELECT * FROM notes WHERE id=:id', id=concept_id)
            supports = rows(c, 'SELECT DISTINCT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r', r=concept['current_revision_id'])
            if len(supports) != 1:
                raise ValueError('L1 requires exactly one current Atomic support')
            revision = supports[0]['atomic_revision_id']
            atomic = one(c, 'SELECT n.* FROM notes n JOIN note_revisions r ON r.note_id=n.id WHERE r.id=:r', r=revision)
            related = {r['note_id'] for r in rows(c, 'SELECT DISTINCT r.note_id FROM concept_inputs i JOIN notes n ON n.current_revision_id=i.concept_revision_id JOIN note_revisions r ON r.id=i.concept_revision_id WHERE i.atomic_revision_id=:r', r=revision)}
            for identity in related:
                n = one(c, 'SELECT current_revision_id FROM notes WHERE id=:id', id=identity)
                if len(rows(c, 'SELECT DISTINCT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r', r=n['current_revision_id'])) != 1:
                    raise ValueError('Selected Atomic also affects a multi-support object; use L3 instead')
            base_calls = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one()
        settings = Settings.from_env()
        provider = OpenAIEmbedding(settings, telemetry=RequestAudit(store.repository))
        cache = CachedQueries(provider)
        queries = [concept['title'], '支持向量机']
        binding = {'profile_id': profile_for(provider)['id'], 'queries': queries}
        cache_path = run / 'query-vectors.json'
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if cached['binding'] != binding:
                raise Conflict('Cached query vectors have a different model or text binding')
            cache.values = cached['values']
        else:
            cache.warm(queries)
            cache_path.write_text(json.dumps({'binding': binding, 'values': cache.values}), encoding='utf-8')
        # Preserve the actual provider identity used by the index profile.
        # Only its network method is fenced by this exact-text replay cache.
        provider.embed = cache.embed
        vectors = provider
        settings = replace(settings, llm_api_key=None, embedding_api_key=None, vision_api_key=None, mineru_api_token=None)
        reader = Application(store, settings=settings, embedding=vectors, agent=ForbidModel(), index_mode='HYBRID')
        query = Query(query=concept['title'], scope=Scope(mode='NOTES', note_ids=[atomic['id'], *sorted(related)]), rerank='OFF', top_k=20)
        original_hash = sha(store.get_note(atomic['id'])['markdown'])

        def observe(stage):
            answer = reader.searcher.search(query)
            with store.db.engine.connect() as c:
                states = {identity: target_state(store, c, identity) for identity in [atomic['id'], *sorted(related)]}
            display = graph(store, layers=['SUPPORT'], limit=100000)
            generation, manifest, matrix = reader.indexer.snapshot('HYBRID')
            if generation is None or matrix is None or matrix.shape[0] != len(manifest) or not np.isfinite(matrix).all():
                raise Conflict('Active vector mapping is inconsistent')
            observation = {'stage': stage, 'status': answer['status'],
                'hits': [{'note_id': h['note_id'], 'revision_id': h['revision_id']} for h in answer['hits']],
                'availability': states, 'index_generation': generation['id'],
                'manifest_revisions': sorted({m['revision_id'] for m in manifest if m['note_id'] in {atomic['id'], *related}}),
                'support_edges': [e['edge_id'] for e in display['edges'] if e['source'] in related or e['target'] == atomic['id']]}
            observations.append(observation)
            (run / 'lifecycle-progress.json').write_text(json.dumps({'observations': observations,
                'worker_steps': len(operations)}, ensure_ascii=False, indent=2), encoding='utf-8')
            return observation

        assert observe('T0')['hits']
        for iteration in range(2):
            assert not store.withdraw_note(atomic['id'], expected_revision=revision)['unchanged']
            assert store.withdraw_note(atomic['id'], expected_revision=revision)['unchanged']
            first = observe(f'L1.{iteration}.T1.old-index')
            assert not first['hits'] and not first['support_edges']
            operations += drain(store, settings, vectors, ('RECONCILE_DEPENDENCIES', 'BUILD_CONCEPT', 'UPDATE_MAPS'))
            assert not observe(f'L1.{iteration}.T2.core-maintained')['hits']
            operations += drain(store, settings, vectors, ('INDEX_NOTE', 'BUILD_INDEX'))
            assert not observe(f'L1.{iteration}.T3.new-index')['manifest_revisions']
            assert not store.restore_note(atomic['id'], expected_revision=revision)['unchanged']
            assert store.restore_note(atomic['id'], expected_revision=revision)['unchanged']
            operations += drain(store, settings, vectors, ('ANALYZE_ATOMIC', 'RECONCILE_DEPENDENCIES', 'BUILD_CONCEPT', 'UPDATE_MAPS'))
            operations += drain(store, settings, vectors, ('INDEX_NOTE', 'BUILD_INDEX'))
            assert observe(f'L1.{iteration}.RESTORED')['hits']
            assert sha(store.get_note(atomic['id'])['markdown']) == original_hash
            assert store.get_note(atomic['id'])['revision']['id'] == revision
        # L7: a removed original source cannot be bypassed by visibility restore.
        store.withdraw_note(atomic['id'], expected_revision=revision)
        with store.db.engine.connect() as c:
            origin = one(c, 'SELECT s.id FROM author_origin_sources a JOIN source_revisions r ON r.id=a.source_revision_id JOIN sources s ON s.id=r.source_id WHERE a.revision_id=:r LIMIT 1', r=revision)
            if not origin:
                origin = one(c, 'SELECT s.id FROM section_evidence a JOIN evidence_anchors e ON e.id=a.evidence_id JOIN source_revisions r ON r.id=e.source_revision_id JOIN sources s ON s.id=r.source_id WHERE a.revision_id=:r LIMIT 1', r=revision)
        if not origin:
            raise ValueError('The real Atomic has no original source dependency')
        store.set_source_deleted(origin['id'], True)
        try:
            store.restore_note(atomic['id'], expected_revision=revision)
        except InvalidKnowledge:
            pass
        else:
            raise Conflict('Invalid-source restoration was incorrectly accepted')
        with store.db.engine.connect() as c:
            assert one(c, 'SELECT visibility FROM notes WHERE id=:id', id=atomic['id'])['visibility'] == 'WITHDRAWN'
            additional_calls = c.exec_driver_sql('SELECT count(*) FROM provider_calls').scalar_one() - base_calls
        result = {'passed': True, 'scenarios': ['L1 repeated twice with real cached vectors', 'L7 invalid original source restoration'],
            'base': saved['base'], 'concept_id': concept_id, 'atomic_id': atomic['id'],
            'observations': observations, 'worker_steps': len(operations), 'additional_worker_provider_calls': additional_calls,
            'query_embedding_texts': 2, 'query_audit': 'library/artifacts/api-calls',
            'atomic_head_and_markdown_preserved': True, 'disposable_branch': True}
        (run / 'lifecycle-acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--concept-id', required=True)
    args = parser.parse_args()
    try:
        result = validate(args.run, args.concept_id)
    except (ValueError, AssertionError, OSError, TypeError, AttributeError) as exc:
        (args.run / 'lifecycle-failure.json').write_text(json.dumps({'status': 'FAILED',
            'error_type': type(exc).__name__, 'detail': str(exc)}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    print(json.dumps({k: v for k, v in result.items() if k != 'observations'}, ensure_ascii=False, indent=2))
