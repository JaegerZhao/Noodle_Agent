"""Read-only observer for the formal slides/paper rerun; never invokes a model.

Snapshots retain source heads, provider watermarks and immutable content hashes.
Use the real browser upload/removal flow separately; this script cannot mutate it.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
LIBRARY = REPO / 'data/knowledge-formal-20261025T043825Z'
RUN = REPO / '.local-only/pipeline-optimization-20261027'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def snapshot(label, backup=False):
    with sqlite3.connect((LIBRARY / 'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        query=lambda sql: [dict(r) for r in c.execute(sql)]
        value={'recorded_at':datetime.now(UTC).isoformat(),'library':str(LIBRARY),
            'provider_rowid':c.execute('SELECT coalesce(max(rowid),0) FROM provider_calls').fetchone()[0],
            'sources':query('SELECT s.*,r.sha256,r.blob_ref FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id'),
            'heads':query('SELECT n.id,n.kind,n.title,n.creation_key,n.current_revision_id,r.markdown_sha256 FROM notes n LEFT JOIN note_revisions r ON r.id=n.current_revision_id'),
            'pending':query("SELECT id,kind,status FROM jobs WHERE status IN ('RUNNING','PENDING','RETRY')")}
        if backup:
            destination=RUN/(label+'-noodle.db')
            if destination.exists():
                raise ValueError('Refuse to overwrite the pre-removal backup')
            RUN.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(destination) as target:
                c.backup(target)
            value['database_backup']=str(destination)
    write(RUN/(label+'.json'),value)
    print(json.dumps({'snapshot':label,'sources':len(value['sources']),'pending':value['pending'],
        'provider_rowid':value['provider_rowid']},ensure_ascii=False))


def observe():
    baseline=json.loads((RUN/'before-unload.json').read_text(encoding='utf-8'))
    with sqlite3.connect((LIBRARY/'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        query=lambda sql,args=(): [dict(r) for r in c.execute(sql,args)]
        sources=query('SELECT s.*,r.sha256 FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id')
        jobs=query("SELECT j.id,j.kind,j.status,j.created_at,j.error_json,j.result_json,json_extract(input_json,'$.revision_id') revision,p.stage,p.updated_at FROM jobs j LEFT JOIN job_progress p ON p.job_id=j.id WHERE j.created_at>=? ORDER BY j.created_at",(baseline['recorded_at'],))
        usage=query('SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) service_ms FROM provider_calls WHERE rowid>? GROUP BY kind,operation,model,status',(baseline['provider_rowid'],))
        prior={h['id']:(h['current_revision_id'],h['markdown_sha256']) for h in baseline['heads'] if h['kind']=='ATOMIC' and (h['creation_key'] or '').startswith('atomic:547cfa7a-3a92-41be-90b8-8c6493bfb785:')}
        current={r['id']:(r['current_revision_id'],r['markdown_sha256']) for r in query('SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id') if r['id'] in prior}
    value={'recorded_at':datetime.now(UTC).isoformat(),'sources':sources,'jobs':jobs,'usage':usage,
        'book_frozen':len(prior)==181 and prior==current,'book_atomic_count':len(prior)}
    write(RUN/'latest.json',value)
    with (RUN/'observations.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(value,ensure_ascii=False)+'\n')
    print(json.dumps({'book_frozen':value['book_frozen'],'calls':sum(v['calls'] for v in usage),
        'tokens':sum(v['total_tokens'] or 0 for v in usage),'jobs':[{k:j[k] for k in ('id','kind','status','stage')} for j in jobs if j['kind']=='GENERATE_ATOMICS' or j['status'] not in {'SUCCEEDED'}]},ensure_ascii=False))


def readback():
    with urlopen('http://127.0.0.1:8011/internal/next/sources',timeout=60) as response:
        sources=json.load(response)
    result={}
    for source in sources:
        if source.get('deleted_at') or 'Probabilistic Machine Learning' in source['title']:
            continue
        revision=source['current_revision_id']
        result[revision]={}
        for name in ('reading?view=manifest','readiness'):
            with urlopen(f'http://127.0.0.1:8011/internal/next/source-revisions/{revision}/{name}',timeout=60) as response:
                result[revision][name]=json.load(response)
    write(RUN/'readback.json',result)
    print(json.dumps({r:v['readiness'] for r,v in result.items()},ensure_ascii=False))


def unload():
    import sys
    sys.path.insert(0,str(REPO/'services/noodle/src'))
    from noodle_agent.next.eligibility import eligible
    from noodle_agent.next.repository import rows
    from noodle_agent.next.store import Store
    baseline=json.loads((RUN/'before-unload.json').read_text(encoding='utf-8'))
    revisions={s['current_revision_id'] for s in baseline['sources'] if 'Probabilistic Machine Learning' not in s['title']}
    old={h['current_revision_id'] for h in baseline['heads'] if h['kind']=='ATOMIC' and any((h.get('creation_key') or '').startswith('atomic:'+r+':') for r in revisions)}
    store=Store(LIBRARY)
    try:
        with store.db.engine.connect() as c:
            valid=eligible(c)
            heads=rows(c,"SELECT g.* FROM index_heads h JOIN index_generations g ON g.id=h.generation WHERE h.mode_key LIKE 'HYBRID:%' ORDER BY g.created_at DESC")
            states=[dict(v) for v in rows(c,"SELECT n.title,w.status,w.desired_generation,w.completed_generation FROM notes n JOIN concept_work_state w ON w.concept_id=n.id WHERE n.id IN (SELECT DISTINCT d.concept_id FROM contribution_decisions d JOIN note_revisions a ON a.id=d.atomic_revision_id JOIN notes an ON an.id=a.note_id WHERE an.creation_key LIKE 'atomic:f37d99d3-8d39-4608-aeb0-75d9eba0e8fd:%' OR an.creation_key LIKE 'atomic:dfa8bd77-7753-40e8-9819-b7f4b039bda0:%')")]
            removed=[dict(v) for v in rows(c,'SELECT id,title,deleted_at FROM sources') if v['deleted_at']]
        manifest=json.loads(store.read(heads[0]['manifest_ref'],heads[0]['manifest_sha256'])) if heads else []
        value={'recorded_at':datetime.now(UTC).isoformat(),'old_atomic_revisions':sorted(old),
            'old_atomics_still_eligible':sorted(old & {v['revision_id'] for v in valid}),
            'old_atomics_in_active_vector_manifest':sorted(old & {v['revision_id'] for v in manifest}),
            'valid_atomic_count':sum(v['kind']=='ATOMIC' for v in valid),'valid_concept_count':sum(v['kind']=='CONCEPT' for v in valid),
            'removed_sources':removed,'concept_states':states,'active_index':heads[0]['id'] if heads else None,
            'history_preserved':all(store.path(h['blob_ref']).is_file() for h in baseline['sources'])}
        value['passed']=len(old)==6 and not value['old_atomics_still_eligible'] and not value['old_atomics_in_active_vector_manifest'] and value['valid_atomic_count']==181 and value['history_preserved']
        write(RUN/'unload-audit.json',value)
        print(json.dumps(value,ensure_ascii=False))
    finally:
        store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['snapshot','observe','readback','unload'])
    parser.add_argument('--label',default='before-unload')
    parser.add_argument('--backup',action='store_true')
    args=parser.parse_args()
    if args.command=='snapshot':
        snapshot(args.label,args.backup)
    else:
        globals()[args.command]()


if __name__=='__main__':
    main()
