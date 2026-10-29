"""Fixed current-corpus recall/scope evaluation without paid model calls."""
import argparse
import json
from pathlib import Path
from time import perf_counter

from noodle_agent.next.retrieval.contracts import Query, Scope
from noodle_agent.next.retrieval.indexing import Indexer
from noodle_agent.next.retrieval.search import Searcher
from noodle_agent.next.store import Store


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('library', type=Path)
    p.add_argument('--cases', type=Path, default=Path('docs/evaluations/agent-qa-current-corpus-20261029.json'))
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    store = Store(args.library)
    results = []
    try:
        with store.db.engine.connect() as c:
            sources = c.exec_driver_sql("SELECT id,title FROM sources WHERE deleted_at IS NULL").fetchall()
        names = {'book':'Probabilistic Machine Learning','ml5':'ML-Slides5','ml6':'ML-Slides6','paper':'Attention Is All You Need'}
        ids = {key:next(row[0] for row in sources if title in row[1]) for key,title in names.items()}
        index = Indexer(store)
        searcher = Searcher(index)
        for case in json.loads(args.cases.read_text(encoding='utf-8'))['cases']:
            source = case['source']
            scoped = source != 'global'
            query = Query(query=case['question'],retrieval_mode='KEYWORD_ONLY',rerank='OFF',
                scope=Scope(mode='SOURCE',source_ids=[ids[source]]) if scoped else Scope(),
                allowed_kinds=['ATOMIC'] if scoped else ['ATOMIC','CONCEPT'],
                entity_note_ids=['301a26a3-8d83-47a1-b071-6e8d759d71b4'] if case.get('outside_entity') else [])
            started = perf_counter()
            value = searcher.search(query)
            hits = value['hits']
            expected = case.get('expected_status')
            passed = value['status'] == expected if expected else value['status'] == 'READY'
            if case.get('scope_only'):
                passed = value['status'] in {'READY','NO_EVIDENCE'}
            if case.get('title_any'):
                passed = passed and any(word in h['title'] for word in case['title_any'] for h in hits)
            if case.get('section_any'):
                passed = passed and any(h['section_id'] in case['section_any'] for h in hits)
            if scoped:
                with store.db.engine.connect() as c:
                    revisions = {r[0] for r in c.exec_driver_sql('SELECT id FROM source_revisions WHERE source_id=?',(ids[source],))}
                passed = passed and all(h['kind']=='ATOMIC' and all(p['source_revision_id'] in revisions for p in h['provenance']) for h in hits)
            item = {'id':case['id'],'question':case['question'],'passed':bool(passed),'status':value['status'],
                'elapsed_ms':(perf_counter()-started)*1000,'timings_ms':value['timings_ms'],
                'hits':[{'title':h['title'],'note_id':h['note_id'],'section_id':h['section_id']} for h in hits]}
            results.append(item)
            print(json.dumps({k:item[k] for k in ['id','passed','status','elapsed_ms']},ensure_ascii=False),flush=True)
        report = {'evaluation':'lexical recall, section targeting and source scope; not answer accuracy',
            'supplier_calls':0,'passed':sum(r['passed'] for r in results),'total':len(results),'cases':results}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='cases'}))
    finally:
        store.close()


if __name__ == '__main__':
    main()
