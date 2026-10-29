"""Real partial-support removal/restoration, with bounded Concept calls and cached queries."""

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
from noodle_agent.next.audit import RequestAudit
from noodle_agent.next.concepts.maintenance_acceptance import index
from noodle_agent.next.concepts.maintenance_run import Budget
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, one, rows
from noodle_agent.next.retrieval.contracts import Query, Scope
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.retrieval.vectors import profile_for
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store


def validate(args):
    run = args.run.resolve()
    if not run.is_relative_to((ROOT / '.local-only').resolve()) or not (run / 'prepared.json').exists():
        raise ValueError('Use an independent prepared .local-only run')
    spec = importlib.util.spec_from_file_location('lifecycle_helpers', ROOT / 'scripts/validate-concept-lifecycle.py')
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    store = Store(run / 'library')
    observations, steps = [], []
    try:
        original = store.get_note(args.concept_id)
        atomic = store.get_note(args.atomic_id)
        revision = atomic['revision']['id']
        with store.db.engine.connect() as c:
            supports = rows(c, 'SELECT DISTINCT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r', r=original['revision']['id'])
            if len(supports) < 2 or revision not in {s['atomic_revision_id'] for s in supports}:
                raise ValueError('This scenario requires multiple current supports including the selected Atomic')
            for support in supports:
                if support['atomic_revision_id'] == revision:
                    continue
                identity = one(c, 'SELECT note_id FROM note_revisions WHERE id=:r', r=support['atomic_revision_id'])['note_id']
                if args.topic.casefold() in store.get_note(identity)['markdown'].casefold():
                    raise ValueError('Selected topic also appears in remaining support; choose a genuinely unique topic')
            provider_baseline = c.exec_driver_sql('SELECT coalesce(max(rowid),0) FROM provider_calls').scalar_one()
        frozen = {'policy': 'real-multi-support-lifecycle-v1', 'provider_baseline': provider_baseline,
            'budget': {'llm_calls': 10, 'estimated_input_tokens': 220000, 'observed_output_tokens': 20000, 'embedding_missing_texts': 20}}
        frozen['plan_sha256'] = sha(canonical(frozen))
        (run / 'change-plan.json').write_text(canonical(frozen), encoding='utf-8')
        settings = Settings.from_env()
        provider = OpenAIEmbedding(settings, telemetry=RequestAudit(store.repository))
        cache = helpers.CachedQueries(provider)
        queries = [args.topic, original['title']]
        binding = {'profile_id': profile_for(provider)['id'], 'queries': queries}
        cache_path = run / 'query-vectors.json'
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if cached['binding'] != binding:
                raise Conflict('Query vector binding differs')
            cache.values = cached['values']
        else:
            cache.warm(queries)
            cache_path.write_text(json.dumps({'binding': binding, 'values': cache.values}), encoding='utf-8')
        provider.embed = cache.embed
        reader = Searcher(Indexer(store, provider))
        query = Query(query=args.topic, scope=Scope(mode='NOTES', note_ids=[args.concept_id, args.atomic_id]), rerank='OFF', top_k=20)
        budget = Budget(store, run, frozen)
        token = call_guard.set(budget.charge)

        def observe(stage):
            # T1 intentionally inspects an invalid current head. It is history
            # for diagnostics, never admissible evidence for the search below.
            note = store.get_note(args.concept_id, historical=True)
            search = reader.search(query)
            with store.db.engine.connect() as c:
                inputs = [r['atomic_revision_id'] for r in rows(c, 'SELECT DISTINCT atomic_revision_id FROM concept_inputs WHERE concept_revision_id=:r', r=note['revision']['id'])]
                valid = store.content.valid_revision(c, note['revision']['id'])
            result = {'stage': stage, 'concept_revision': note['revision']['id'], 'valid': valid,
                'topic_present': args.topic.casefold() in note['markdown'].casefold(), 'inputs': inputs,
                'search_status': search['status'], 'hit_revisions': [h['revision_id'] for h in search['hits']]}
            observations.append(result)
            (run / 'multi-support-progress.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding='utf-8')
            return result

        def drain(kinds):
            app = Application(store, settings=replace(settings, embedding_api_key=None, vision_api_key=None, mineru_api_token=None),
                              index_mode='KEYWORD_ONLY', job_kinds=kinds)
            for _ in range(300):
                result = app.run_once('real-partial-support-lifecycle')
                if result is None:
                    return
                steps.append(result)
                if result['status'] != 'SUCCEEDED':
                    raise Conflict('Partial-support worker failed: ' + canonical(result))
            raise Conflict('Partial-support maintenance did not settle')

        try:
            assert observe('T0')['topic_present']
            store.withdraw_note(args.atomic_id, expected_revision=revision)
            first = observe('WITHDRAW.T1.old-index')
            assert revision not in first['hit_revisions'] and original['revision']['id'] not in first['hit_revisions']
            drain(('ANALYZE_ATOMIC', 'RECONCILE_DEPENDENCIES', 'BUILD_CONCEPT', 'UPDATE_MAPS'))
            second = observe('WITHDRAW.T2.new-core-old-index')
            assert second['valid'] and revision not in second['inputs'] and not second['topic_present']
            removed_core = store.get_note(args.concept_id)['markdown']
            assert args.remaining_text in removed_core
            index(store, run, allow_embeddings=True)
            observe('WITHDRAW.T3.new-index')
            store.restore_note(args.atomic_id, expected_revision=revision)
            drain(('ANALYZE_ATOMIC', 'RECONCILE_DEPENDENCIES', 'BUILD_CONCEPT', 'UPDATE_MAPS'))
            restored = observe('RESTORE.T2.new-core-old-index')
            assert restored['valid'] and revision in restored['inputs'] and restored['topic_present']
            index(store, run, allow_embeddings=True)
            assert observe('RESTORE.T3.new-index')['hit_revisions']
            assert store.get_note(args.atomic_id)['revision']['id'] == revision
            assert sha(store.get_note(args.atomic_id)['markdown']) == sha(atomic['markdown'])
        finally:
            call_guard.reset(token)
        with store.db.engine.connect() as c:
            usage = [dict(r) for r in rows(c, 'SELECT kind,operation,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens FROM provider_calls WHERE rowid>:base GROUP BY kind,operation,status', base=provider_baseline)]
        result = {'passed': True, 'scenario': 'L3 real independent support removal and restoration',
            'concept_id': args.concept_id, 'atomic_id': args.atomic_id, 'observations': observations,
            'worker_steps': len(steps), 'usage': usage, 'query_embedding_texts': 2, 'atomic_unchanged': True}
        (run / 'multi-support-acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--concept-id', required=True)
    parser.add_argument('--atomic-id', required=True)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--remaining-text', required=True)
    args = parser.parse_args()
    try:
        result = validate(args)
    except (ValueError, AssertionError, OSError, TypeError) as exc:
        (args.run / 'multi-support-failure.json').write_text(json.dumps({'status': 'FAILED', 'error': type(exc).__name__, 'detail': str(exc)}), encoding='utf-8')
        raise
    print(json.dumps({k: v for k, v in result.items() if k != 'observations'}, ensure_ascii=False, indent=2))
