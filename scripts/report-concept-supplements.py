"""Read-only observations of the authorized formal supplementary validation."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.local-only/concept-supplement-validation-20261027'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compact',action='store_true')
    args=parser.parse_args()
    baseline = json.loads((RUN / 'baseline.json').read_text(encoding='utf-8'))
    library = Path(baseline['library'])
    with sqlite3.connect((library / 'noodle.db').as_uri() + '?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        def rows(sql, args=()):
            return [dict(r) for r in c.execute(sql, args)]

        heads = {r['id']: r for r in rows('SELECT n.id,n.kind,n.title,n.current_revision_id,r.markdown_sha256 FROM notes n LEFT JOIN note_revisions r ON r.id=n.current_revision_id')}
        book = [r for r in baseline['heads'] if r['kind']=='ATOMIC']
        sources = rows("SELECT s.id,s.title,r.id revision_id,r.sha256 FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.kind='PDF' ORDER BY s.created_at")
        details = []
        for source in sources:
            if source['id'] in baseline['source_ids']:
                continue
            jobs = rows("SELECT j.*,p.stage,p.details_json,p.updated_at FROM jobs j LEFT JOIN job_progress p ON p.job_id=j.id WHERE j.kind='GENERATE_ATOMICS' AND json_extract(j.input_json,'$.revision_id')=? ORDER BY j.created_at DESC LIMIT 1", (source['revision_id'],))
            if not jobs:
                details.append({**source, 'status':'NOT_CLAIMED'})
                continue
            job = jobs[0]
            checkpoints = rows("SELECT stage,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=? AND state='DONE'", (job['id'],))
            plans = [r for r in checkpoints if r['stage'].startswith('source.sections.plan')]
            plan = json.loads((library / plans[-1]['artifact_ref']).read_text(encoding='utf-8')) if plans else {}
            atomics = rows("SELECT n.id,n.title,n.current_revision_id FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC' AND n.visibility='ACTIVE' AND r.status='PUBLISHED' AND n.creation_key LIKE ?", ('atomic:'+source['revision_id']+':%',))
            decisions = rows("SELECT d.concept_id,n.title,d.action,d.decision_json,d.status FROM contribution_decisions d JOIN notes n ON n.id=d.concept_id WHERE d.atomic_revision_id IN (SELECT r.id FROM note_revisions r JOIN notes n ON n.current_revision_id=r.id WHERE n.creation_key LIKE ?) AND d.status='ACCEPTED'", ('atomic:'+source['revision_id']+':%',))
            roles = sorted({json.loads(d['decision_json']).get('source_role', 'UNKNOWN') for d in decisions})
            concepts = {d['concept_id']:d['title'] for d in decisions}
            old = {r['id'] for r in baseline['heads'] if r['kind']=='CONCEPT'}
            details.append({**source,'job_id':job['id'],'status':job['status'],'stage':job['stage'],
                'error':json.loads(job['error_json']),'result':json.loads(job['result_json']),
                'created_at':job['created_at'],'updated_at':job['updated_at'],
                'material_profile':plan.get('document_profile'), 'source_role':plan.get('source_role'),
                'planned':[{'key':p['key'],'title':p['title']} for p in plan.get('plans',[])],
                'published':atomics,'mineru_reused':any(r['stage']=='parse.reuse' for r in checkpoints),
                'contribution_roles':roles,'concepts':concepts,'reused_concepts':sorted(set(concepts)&old),
                'new_concepts':sorted(set(concepts)-old),'decision_actions':{a:sum(d['action']==a for d in decisions) for a in sorted({d['action'] for d in decisions})}})
        usage = rows('SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms,sum(total_tokens IS NULL) unknown_usage FROM provider_calls WHERE rowid>? GROUP BY 1,2,3,4', (baseline['provider_rowid'],))
        jobs = rows('SELECT id,kind,status,error_json FROM jobs WHERE rowid>? AND status IN (\'PENDING\',\'RUNNING\',\'RETRY\',\'REVIEW\') ORDER BY created_at', (baseline['job_rowid'],))
        result = {'recorded_at':datetime.now(UTC).isoformat(),'frozen_book_atomics_unchanged':all(heads.get(r['id'])==r for r in book),
            'sources':details,'usage':usage,'unsettled_jobs':jobs,
            'published_concepts':c.execute("SELECT count(*) FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='CONCEPT' AND n.visibility='ACTIVE' AND r.status='PUBLISHED'").fetchone()[0]}
    destination = RUN / 'observations.jsonl'
    with destination.open('a', encoding='utf-8') as f:
        f.write(json.dumps(result,ensure_ascii=False)+'\n')
    (RUN / 'latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    display={**result,'sources':[{k:v for k,v in s.items() if k not in {'planned','published','result'}}|{'planned_count':len(s.get('planned',[])),'published_count':len(s.get('published',[]))} for s in details]}
    if args.compact:
        display={'recorded_at':result['recorded_at'],'book_unchanged':result['frozen_book_atomics_unchanged'],
            'sources':[{k:s.get(k) for k in ['title','status','stage','planned_count','published_count','concepts','contribution_roles']} for s in display['sources']],
            'unsettled':[{k:j[k] for k in ['id','kind','status']} for j in jobs],
            'usage':{kind:{'calls':sum(v['calls'] for v in usage if v['kind']==kind),
                'tokens':sum(v['total_tokens'] or 0 for v in usage if v['kind']==kind)} for kind in sorted({v['kind'] for v in usage})}}
    print(json.dumps(display,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
