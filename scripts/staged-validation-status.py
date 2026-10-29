"""Read-only stage evidence for a frozen frontend run; no retries or model calls."""

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen


def report(run, label):
    manifest = json.loads((run/'manifest.json').read_text(encoding='utf8'))
    library = Path(manifest['library'])
    with sqlite3.connect((library/'noodle.db').as_uri()+'?mode=ro', uri=True) as c:
        c.row_factory = sqlite3.Row
        sources = [dict(r) for r in c.execute("SELECT s.title,s.current_revision_id FROM sources s WHERE kind='PDF' ORDER BY created_at")]
        jobs = [dict(r) for r in c.execute('SELECT id,kind,status,error_json FROM jobs ORDER BY created_at')]
        calls = [dict(r) for r in c.execute('SELECT operation,status,total_tokens,input_tokens,output_tokens,cached_input_tokens,elapsed_ms FROM provider_calls')]
        concepts = [dict(r) for r in c.execute("SELECT n.id,n.title,n.current_revision_id FROM notes n WHERE kind='CONCEPT' AND current_revision_id IS NOT NULL AND visibility='ACTIVE'")]
        for note in concepts:
            note['source_count'] = c.execute("SELECT count(DISTINCT e.source_revision_id) FROM concept_inputs ci JOIN section_evidence se ON se.revision_id=ci.atomic_revision_id JOIN evidence_anchors e ON e.id=se.evidence_id WHERE ci.concept_revision_id=?", (note['current_revision_id'],)).fetchone()[0]
    states = []
    for source in sources:
        with urlopen(f"http://127.0.0.1:{manifest['port']}/internal/next/source-revisions/{source['current_revision_id']}/readiness",timeout=30) as r:
            states.append(json.load(r))
    value = {'recorded_at':datetime.now(UTC).isoformat(), 'stage':label, 'reuse_policy':manifest['reuse_policy'],
        'readiness':states, 'job_statuses':dict(Counter(j['status'] for j in jobs)), 'review_jobs':[j for j in jobs if j['status']=='REVIEW'],
        'concepts':concepts, 'usage':{'calls':len(calls), 'known_tokens':sum(r['total_tokens'] or 0 for r in calls),
        'missing_usage':sum(r['total_tokens'] is None for r in calls), 'failed_calls':sum(r['status']=='FAILED' for r in calls),
        'service_ms':sum(r['elapsed_ms'] or 0 for r in calls)}, 'operations':{}}
    for op in sorted({r['operation'] for r in calls}):
        selected = [r for r in calls if r['operation']==op]
        value['operations'][op] = {'calls':len(selected), 'known_tokens':sum(r['total_tokens'] or 0 for r in selected), 'service_ms':sum(r['elapsed_ms'] or 0 for r in selected)}
    folder = run/'stages'
    folder.mkdir(exist_ok=True)
    target = folder/(datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')+'-'+label+'.json')
    target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'report':str(target),'jobs':value['job_statuses'],'coverage':[s['coverage'] for s in states], 'concepts':len(concepts),'usage':value['usage']},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('run',type=Path)
    p.add_argument('--stage',required=True,choices=['book','slides','paper','complete','problem'])
    args=p.parse_args()
    report(args.run.resolve(),args.stage)
