"""Real source/Atomic withdrawal, media and index acceptance in a prepared copy."""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/noodle/src'))

from noodle_agent.config import Settings
from noodle_agent.next.concepts.entity_writing import IMAGE
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.eligibility import eligible
from noodle_agent.next.graph_projection import graph
from noodle_agent.next.pipeline_state import indexed_revisions
from noodle_agent.next.provider_limits import call_guard
from noodle_agent.next.repository import Conflict, identifier, rows
from noodle_agent.next.runtime import Application
from noodle_agent.next.source_library import batch
from noodle_agent.next.store import Store


def validate(run, atomic_only=False):
    if not run.is_relative_to(ROOT / '.local-only') or not (run / 'prepared.json').exists():
        raise ValueError('Lifecycle requires a prepared isolated library')
    store = Store(run / 'library')
    settings = Settings.from_env(ROOT)
    started = time.monotonic()
    observations, steps = [], []
    def scope(request):
        if not request['operation'].startswith(('next.entity_', 'next.concept_', 'next.organization_')):
            raise Conflict('Unplanned lifecycle provider operation: '+request['operation'])
    guard = call_guard.set(scope)
    try:
        with store.db.engine.connect() as c:
            baseline = c.exec_driver_sql('SELECT coalesce(max(rowid),0) FROM provider_calls').scalar_one()
            atomics = [dict(r) for r in rows(c, "SELECT n.id,n.current_revision_id,r.markdown_ref,r.markdown_sha256 FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id WHERE n.kind='ATOMIC'")]
            sources = {r['title']: dict(r) for r in rows(c, "SELECT * FROM sources WHERE deleted_at IS NULL AND kind='PDF'")}
        def drain():
            for kinds in [('ANALYZE_ATOMIC',), ('RECONCILE_DEPENDENCIES','BUILD_CONCEPT','MAINTAIN_CONCEPT_STRUCTURE'),
                          ('REVIEW_ORGANIZATION','UPDATE_MAPS'), ('INDEX_NOTE','BUILD_INDEX')]:
                app = Application(store, settings=settings, job_kinds=kinds)
                while step := app.run_once('workspace-lifecycle'):
                    steps.append(step)
                    (run / 'lifecycle-steps.json').write_text(canonical(steps), encoding='utf8')
                    if step['status'] != 'SUCCEEDED':
                        raise Conflict('Lifecycle worker failed: '+canonical(step))
        def observe(stage, removed=()):
            with store.db.engine.connect() as c:
                current = eligible(c)
                ids = {n['id'] for n in current}
                images = []
                for n in current:
                    if n['kind'] != 'CONCEPT':
                        continue
                    for _, ref in IMAGE.findall(store.read(n['markdown_ref'],n['markdown_sha256']).decode()):
                        if sha(store.path(ref.removeprefix('../')).read_bytes()) != Path(ref).stem:
                            raise Conflict('Broken key figure after lifecycle')
                        images.append({'concept_id':n['id'],'ref':ref})
            projection = graph(store, mode='provenance', limit=100000)
            if set(removed)&{n['source_revision_id'] for n in projection['nodes'] if n['kind']=='SOURCE'}:
                raise Conflict('Removed source remains in provenance projection')
            indexed, head = indexed_revisions(store, current)
            if len(indexed) != len(current):
                raise Conflict('Current lifecycle revisions are not fully indexed')
            report={'stage':stage,'effective_notes':len(ids),'indexed':len(indexed),
                'index_generation':head['id'] if head else None,'source_nodes':sum(n['kind']=='SOURCE' for n in projection['nodes']),
                'images':images}
            observations.append(report)
            (run / 'lifecycle-progress.json').write_text(canonical(observations),encoding='utf8')
        drain()
        observe('BASELINE')
        scenarios=[] if atomic_only else [('PAPER',['Attention Is All You Need - NeurIPS 2017.pdf']),
                              ('COURSES',['ML-Slides5.pdf','ML-Slides6.pdf'])]
        for label, titles in scenarios:
            selected = [sources[t] for t in titles]
            revisions = {s['current_revision_id'] for s in selected}
            for operation in ['delete','restore']:
                result = batch(store,identifier(),operation,[{'source_id':s['id'],'expected_revision':s['current_revision_id']} for s in selected])
                if result['succeeded'] != len(selected):
                    raise Conflict('Batch lifecycle conflict: '+canonical(result))
                if operation=='delete':
                    with store.db.engine.connect() as c:
                        leaked=c.exec_driver_sql("SELECT count(*) FROM notes n JOIN section_evidence se ON se.revision_id=n.current_revision_id JOIN evidence_anchors e ON e.id=se.evidence_id WHERE n.kind='ATOMIC' AND e.source_revision_id IN ("+','.join('?' for _ in revisions)+')',tuple(revisions)).scalar_one()
                        # Current heads remain as history; eligibility, rather than
                        # destructive deletion, determines whether they can be read.
                        if not leaked:
                            raise Conflict('Lifecycle fixture has no affected Atomics')
                        effective={n['revision_id'] for n in eligible(c)}
                        if any(c.exec_driver_sql('SELECT 1 FROM section_evidence se JOIN evidence_anchors e ON e.id=se.evidence_id WHERE se.revision_id=? AND e.source_revision_id IN ('+','.join('?' for _ in revisions)+')',(r,*revisions)).first() for r in effective):
                            raise Conflict('Removed source still supplies effective evidence')
                drain()
                observe(label+'_'+operation.upper(),revisions if operation=='delete' else ())
        if atomic_only:
            with store.db.engine.connect() as c:
                current_ids={n['id'] for n in eligible(c)}
                choices=[dict(r) for r in rows(c,"SELECT n.id,n.title,count(DISTINCT d.concept_id) targets FROM contribution_decisions d JOIN note_revisions r ON r.id=d.atomic_revision_id JOIN notes n ON n.current_revision_id=r.id WHERE d.status='ACCEPTED' GROUP BY n.id HAVING count(DISTINCT d.concept_id)>1") if r['id'] in current_ids]
            if not choices:
                raise Conflict('No effective multi-target Atomic fixture')
            chosen=choices[0]
            expected=store.get_note(chosen['id'])['revision']['id']
            store.withdraw_note(chosen['id'],expected_revision=expected)
            with store.db.engine.connect() as c:
                if chosen['id'] in {n['id'] for n in eligible(c)}:
                    raise Conflict('Withdrawn Atomic remains effective')
            drain()
            observe('MULTI_TARGET_ATOMIC_WITHDRAW')
            store.restore_note(chosen['id'],expected_revision=expected)
            drain()
            observe('MULTI_TARGET_ATOMIC_RESTORE')
        with store.db.engine.connect() as c:
            usage=[dict(r) for r in rows(c,'SELECT operation,model,count(*) calls,sum(total_tokens) total_tokens FROM provider_calls WHERE rowid>:n GROUP BY operation,model',n=baseline)]
            integrity=c.exec_driver_sql('PRAGMA integrity_check').scalar_one()
            for n in atomics:
                if c.exec_driver_sql('SELECT current_revision_id FROM notes WHERE id=?',(n['id'],)).scalar_one()!=n['current_revision_id']:
                    raise Conflict('Lifecycle changed an Atomic head')
                store.read(n['markdown_ref'],n['markdown_sha256'])
        report={'passed':True,'observations':observations,'usage':usage,'elapsed_s':round(time.monotonic()-started,3),
                'atomic_heads_unchanged':True,'integrity':integrity,'steps':len(steps)}
        (run/('lifecycle-atomic-acceptance.json' if atomic_only else 'lifecycle-acceptance.json')).write_text(canonical(report),encoding='utf8')
        return {k:v for k,v in report.items() if k!='observations'}
    finally:
        call_guard.reset(guard)
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--atomic-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(validate(args.run.resolve(),args.atomic_only),ensure_ascii=False))
