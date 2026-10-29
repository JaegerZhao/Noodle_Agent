"""Read back the formal optimization runs, with no model or generation calls."""

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.local-only/pipeline-optimization-20261027'
LIBRARY=ROOT/'data/knowledge-formal-20261025T043825Z'
sys.path.insert(0,str(ROOT/'services/noodle/src'))


def stage(call):
    operation=call['operation']
    if call['kind']=='VISION_CALL':
        return 'visual'
    if call['kind']=='EMBEDDING_CALL':
        return 'embedding'
    if operation in {'next.structure','next.structure_exclusion_review','next.plan_labels'}:
        return 'planning'
    return {'next.section_write':'atomic_write','next.assembly_review':'atomic_check',
        'next.section_repair':'atomic_repair','next.entity_plan':'concept_routing',
        'next.organization_plan':'organization'}.get(operation,'concept_core' if operation.startswith('next.entity_') else operation)


def usage(calls):
    groups=defaultdict(list)
    for call in calls:
        groups[stage(call)].append(call)
    return {'calls':len(calls),'tokens':sum(c['total_tokens'] or 0 for c in calls),
        'input_tokens':sum(c['input_tokens'] or 0 for c in calls),'output_tokens':sum(c['output_tokens'] or 0 for c in calls),
        'unknown_usage':sum(c['total_tokens'] is None for c in calls),
        'service_ms':sum(c['elapsed_ms'] or 0 for c in calls),
        'stages':{key:{'calls':len(values),'tokens':sum(c['total_tokens'] or 0 for c in values),
            'service_ms':sum(c['elapsed_ms'] or 0 for c in values)} for key,values in groups.items()}}


def main():
    from noodle_agent.next.figure_bindings import source_catalogue
    from noodle_agent.next.metrics import timeline
    from noodle_agent.next.navigation import current_notes
    from noodle_agent.next.pipeline_state import readiness
    from noodle_agent.next.reading_manifest import manifest, note_anchors
    from noodle_agent.next.repository import rows
    from noodle_agent.next.store import Store
    from noodle_agent.next.verification import verify_generation

    before=json.loads((RUN/'before-unload.json').read_text(encoding='utf-8'))
    first=json.loads((RUN/'before-generation.json').read_text(encoding='utf-8'))
    after_slides=json.loads((RUN/'after-slides.json').read_text(encoding='utf-8'))
    fresh=json.loads((RUN/'before-paper-fresh.json').read_text(encoding='utf-8'))
    store=Store(LIBRARY)
    result={'recorded_at':datetime.now(UTC).isoformat(),'provider_calls_issued_by_report':0,'documents':{}}
    try:
        with sqlite3.connect((LIBRARY/'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            calls=[dict(r) for r in c.execute('SELECT rowid,* FROM provider_calls WHERE rowid>? ORDER BY rowid',(before['provider_rowid'],))]
            sources=[dict(r) for r in c.execute("SELECT * FROM sources WHERE deleted_at IS NULL AND title NOT LIKE 'Probabilistic Machine Learning%'")]
            all_heads={r['id']:(r['current_revision_id'],r['markdown_sha256']) for r in c.execute('SELECT n.id,n.current_revision_id,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id')}
            pending=[dict(r) for r in c.execute("SELECT id,kind,status FROM jobs WHERE status IN ('RUNNING','PENDING','RETRY')")]
        book={h['id']:(h['current_revision_id'],h['markdown_sha256']) for h in before['heads'] if h['kind']=='ATOMIC' and (h['creation_key'] or '').startswith('atomic:547cfa7a-3a92-41be-90b8-8c6493bfb785:')}
        result['book_unchanged']=len(book)==181 and all(all_heads.get(key)==value for key,value in book.items())
        result['pending']=pending
        result['phases']={
            'initial_unload':usage([c for c in calls if c['rowid']<=first['provider_rowid']]),
            'slides':usage([c for c in calls if first['provider_rowid']<c['rowid']<=after_slides['provider_rowid']]),
            'paper_failed_and_its_unload':usage([c for c in calls if after_slides['provider_rowid']<c['rowid']<=fresh['provider_rowid']]),
            'paper_fresh':usage([c for c in calls if c['rowid']>fresh['provider_rowid']])}
        for source in sources:
            revision=source['current_revision_id']
            with store.db.engine.connect() as c:
                job=rows(c,"SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1",r=revision)[0]
                plan_row=rows(c,"SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND stage LIKE 'source.sections.plan%' AND state='DONE' ORDER BY created_at DESC LIMIT 1",j=job['id'])
                reuse=rows(c,"SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND stage='parse.reuse' AND state='DONE'",j=job['id'])
                visual_receipts=rows(c,"SELECT stage,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=:j AND (stage LIKE 'visual-page-cache.%.reuse' OR stage LIKE 'visual-page-cache.%.fresh') AND state='DONE'",j=job['id'])
                routes=[dict(r) for r in rows(c,"SELECT n.title,c.title concept_title,r.status,json_extract(r.route_json,'$.route.contribution_mode') contribution_mode FROM atomic_entity_routes r JOIN notes n ON n.current_revision_id=r.atomic_revision_id LEFT JOIN notes c ON c.id=r.concept_id WHERE n.creation_key LIKE :p",p=f'atomic:{revision}:%')]
            data=manifest(store,revision)
            plan=json.loads(store.read(plan_row[0]['artifact_ref'],plan_row[0]['artifact_sha256'])) if plan_row else {}
            notes=[store.get_note(n['note_id']) for n in data['notes']]
            images=Counter(path for n in notes for path in re.findall(r'!\[[^\]]*\]\(([^)]+)\)',n['markdown']))
            catalogue=source_catalogue(store,revision)
            mismatches=[]
            for note in notes:
                owner=next(n['primary_node_id'] for n in data['notes'] if n['note_id']==note['id'])
                mismatches.extend({'note':note['id'],'section':a['section_id'],'owner':a['node_id'],'expected':owner}
                    for a in note_anchors(store,revision,note['revision']['id']) if a['node_id']!=owner)
            entry={'source':source,'job_id':job['id'],'job_status':job['status'],'readiness':readiness(store,revision),
                'plan':[{'title':p['title'],'pages':p['pages'],'concept_outline':p.get('concept_outline')} for p in plan.get('plans',[])],
                'engineering':verify_generation(store,job['id']) if job['status']=='SUCCEEDED' else None,
                'metrics':timeline(store,job['id']), 'parse_reuse':bool(reuse),
                'visual_cache_receipts':[json.loads(store.read(r['artifact_ref'],r['artifact_sha256'])) for r in visual_receipts],
                'routes':routes,'section_count':sum(len(n['sections']) for n in notes),'reading_owner_mismatches':mismatches,
                'figures':[{'kind':f.get('kind'),'number':f['number'],'paths':{p['asset_ref']:images[p['asset_ref']] for p in f['panels']}} for f in catalogue],
                'formula_direction_source_issue_notes':[n['title'] for n in notes if re.search(r'\\min_\{?\\?alpha',n['markdown'])],
                'notes':[{'id':n['id'],'revision_id':n['revision']['id'],'title':n['title'],'sections':[s['title'] for s in n['sections']]} for n in notes]}
            result['documents'][source['title']]=entry
        result['valid_concept_count']=current_notes(store,kind='CONCEPT',limit=1000)['total']
        result['engineering_complete']=result['book_unchanged'] and not pending and len(result['documents'])==2 and all(d['readiness']['notes_status']=='READY' and d['readiness']['concept_maintenance']['status']=='COMPLETE' and d['engineering']['status']=='ENGINEERING_READBACK_PASSED' for d in result['documents'].values())
        result['business_findings']=['SVM source uses min for the concave dual objective; retained faithfully, not treated as a pipeline blocker per user discussion.',
            'Slides planning produced 7 Atomics rather than the previous 2; granularity remains a follow-up.',
            'Vision is warm-cache reuse, explicitly retained by user; this is not a fresh-vision/cold-parser benchmark.']
        path=RUN/'final-readback.json'
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'report':str(path),'engineering_complete':result['engineering_complete'],'book_unchanged':result['book_unchanged'],
            'phases':result['phases'],'documents':{name:{'planned':d['readiness']['coverage']['planned'],'published':d['readiness']['coverage']['published'],
                'notes_status':d['readiness']['notes_status'],'concept_status':d['readiness']['concept_maintenance']['status'],
                'sections':d['section_count'],'figures':d['figures'],'owner_mismatches':len(d['reading_owner_mismatches']),
                'clock':d['metrics']['summary']} for name,d in result['documents'].items()}},ensure_ascii=False))
    finally:
        store.close()


if __name__=='__main__':
    main()
