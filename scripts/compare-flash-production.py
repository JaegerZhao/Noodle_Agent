"""Read-only paired production comparison. Never invokes providers or jobs."""

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.local-only/flash-comparison-20261027'
OBS=ROOT/'.local-only/pipeline-optimization-20261027'
LIBRARY=ROOT/'data/knowledge-formal-20261025T043825Z'
sys.path.insert(0,str(ROOT/'services/noodle/src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,default=RUN)
    parser.add_argument('--baseline',default='baseline-mixed.json')
    parser.add_argument('--before-generation',default='before-flash-generation')
    parser.add_argument('--before-unload',default='before-flash-unload')
    args=parser.parse_args()
    run=args.run.resolve()
    from noodle_agent.next.figure_bindings import source_catalogue
    from noodle_agent.next.metrics import timeline
    from noodle_agent.next.navigation import current_notes
    from noodle_agent.next.pipeline_state import readiness
    from noodle_agent.next.reading_manifest import manifest, note_anchors
    from noodle_agent.next.repository import rows
    from noodle_agent.next.store import Store
    from noodle_agent.next.verification import verify_generation

    baseline=json.loads((run/args.baseline).read_text(encoding='utf-8'))
    before=json.loads((OBS/(args.before_generation+'.json')).read_text(encoding='utf-8'))
    unload=json.loads((OBS/(args.before_unload+'.json')).read_text(encoding='utf-8'))
    origin=json.loads((OBS/'before-unload.json').read_text(encoding='utf-8'))
    result={'recorded_at':datetime.now(UTC).isoformat(),'provider_calls_issued_by_report':0,'documents':{}}
    with sqlite3.connect((LIBRARY/'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        calls=[dict(r) for r in c.execute('SELECT rowid,* FROM provider_calls WHERE rowid>? ORDER BY rowid',(unload['provider_rowid'],))]
        sources=[dict(r) for r in c.execute('SELECT * FROM sources WHERE deleted_at IS NULL')]
        heads={r['id']:(r['current_revision_id'],r['markdown_sha256']) for r in c.execute('SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id')}
        result['pending']=[dict(r) for r in c.execute("SELECT id,kind,status FROM jobs WHERE status IN ('RUNNING','PENDING','RETRY')")]
    book={h['id']:(h['current_revision_id'],h['markdown_sha256']) for h in origin['heads'] if h['kind']=='ATOMIC' and (h['creation_key'] or '').startswith('atomic:547cfa7a-3a92-41be-90b8-8c6493bfb785:')}
    result['book_unchanged']=len(book)==181 and all(heads.get(k)==v for k,v in book.items())
    result['unload']={'calls':len([c for c in calls if c['rowid']<=before['provider_rowid']]),
        'tokens':sum(c['total_tokens'] or 0 for c in calls if c['rowid']<=before['provider_rowid'])}
    result['pro_calls']=[c for c in calls if 'pro' in c['model'].lower()]
    store=Store(LIBRARY)
    try:
        for source in sources:
            old=baseline['documents'].get(source['title'])
            revision=source['current_revision_id']
            if not old or revision==old.get('source',{}).get('current_revision_id',old.get('revision_id')):
                continue
            with store.db.engine.connect() as c:
                job=rows(c,"SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1",r=revision)[0]
                plan_rows=rows(c,"SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND stage LIKE 'source.sections.plan.%' AND state='DONE' ORDER BY created_at DESC LIMIT 1",j=job['id'])
                routes=rows(c,"SELECT n.title,c.title concept_title,r.status,json_extract(r.route_json,'$.route.contribution_mode') contribution_mode FROM atomic_entity_routes r JOIN notes n ON n.current_revision_id=r.atomic_revision_id LEFT JOIN notes c ON c.id=r.concept_id WHERE n.creation_key LIKE :p",p=f'atomic:{revision}:%')
            state=readiness(store,revision)
            data=manifest(store,revision) if plan_rows else {'notes':[]}
            notes=[store.get_note(n['note_id']) for n in data['notes']]
            plan=json.loads(store.read(plan_rows[0]['artifact_ref'],plan_rows[0]['artifact_sha256'])) if plan_rows else {}
            images=Counter(path for n in notes for path in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',n['markdown']))
            mismatches=[]
            for note in notes:
                owner=next(n['primary_node_id'] for n in data['notes'] if n['note_id']==note['id'])
                mismatches.extend(a for a in note_anchors(store,revision,note['revision']['id']) if a['node_id']!=owner)
            model_calls=[c for c in calls if c['root_job_id']==job['id'] or c['job_id']==job['id']]
            catalogue=source_catalogue(store,revision) if plan_rows else []
            result['documents'][source['title']]={'revision_id':revision,'job_id':job['id'],'job_status':job['status'],
                'readiness':state,'metrics':timeline(store,job['id']),'baseline_summary':old['metrics']['summary'],
                'baseline_sections':old['section_count'],'baseline_plans':old.get('plan',old.get('plans',[])),
                'plans':plan.get('plans',[]),'routes':[dict(r) for r in routes],
                'section_count':sum(len(n['sections']) for n in notes),'reading_owner_mismatches':mismatches,
                'figures':[{'kind':f.get('kind'),'number':f['number'],'paths':{p['asset_ref']:images[p['asset_ref']] for p in f['panels']}} for f in catalogue],
                'calls':model_calls,'engineering':verify_generation(store,job['id']) if job['status']=='SUCCEEDED' else None,
                'notes':[{'id':n['id'],'title':n['title'],'revision_id':n['revision']['id'],
                    'markdown':n['markdown'],'sections':[s['title'] for s in n['sections']]} for n in notes]}
        result['valid_concepts']=current_notes(store,kind='CONCEPT')['total']
    finally:
        store.close()
    (run/'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'book_unchanged':result['book_unchanged'],'pro_calls':len(result['pro_calls']),
        'pending':result['pending'],'documents':{k:{'status':v['job_status'],'coverage':v['readiness']['coverage'],
            'tokens':v['metrics']['summary']['total_tokens'],'ready_ms':v['metrics']['summary'].get('notes_ready_wall_ms')} for k,v in result['documents'].items()}},ensure_ascii=False))


if __name__=='__main__':
    main()
