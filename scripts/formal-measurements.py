"""Read-only measurements for a formal frontend run; never generates or retries content."""

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen


def instant(value):
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def group(operation):
    if operation.startswith(('next.entity_', 'next.organization_', 'next.canonical_')):
        return 'concept'
    if 'embed' in operation or 'index' in operation:
        return 'index'
    if 'visual' in operation:
        return 'vision'
    if operation.startswith(('next.structure', 'next.plan_labels')):
        return 'atomic_plan'
    return 'atomic_content'


def totals(calls):
    starts = [instant(v['started_at']) for v in calls if v.get('started_at')]
    ends = [instant(v['started_at']) + timedelta(milliseconds=v.get('elapsed_ms') or 0) for v in calls if v.get('started_at')]
    return {'calls':len(calls), 'known_total_tokens':sum(v.get('total_tokens') or 0 for v in calls),
        'known_input_tokens':sum(v.get('input_tokens') or 0 for v in calls),
        'known_output_tokens':sum(v.get('output_tokens') or 0 for v in calls),
        'known_cached_input_tokens':sum(v.get('cached_input_tokens') or 0 for v in calls),
        'missing_cached_usage_llm_calls':sum(v.get('kind') == 'LLM_CALL' and v.get('cached_input_tokens') is None for v in calls),
        'missing_usage_calls':sum(v.get('total_tokens') is None for v in calls),
        'failed_calls':sum(v.get('status') == 'FAILED' for v in calls),
        'service_ms':sum(v.get('elapsed_ms') or 0 for v in calls),
        'observed_call_span_ms':int((max(ends)-min(starts)).total_seconds()*1000) if starts else None,
        'span_includes_gaps':True}


def historical_parse(document):
    cache = document.get('parse_cache')
    if not cache:
        return {'mode':'CURRENT_FRESH_PARSE'}
    root = Path(cache)
    receipt = root.parent / 'parse-result.json'
    if receipt.is_file():
        value = json.loads(receipt.read_text(encoding='utf-8'))
        seconds = value.get('historical_elapsed_seconds',value.get('elapsed_seconds'))
        return {'mode':'HISTORICAL_RECEIPT','seconds':seconds,'receipt':str(receipt),'source_sha256':document['sha256']}
    with sqlite3.connect((root/'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
        job = c.execute("SELECT j.id,j.created_at FROM jobs j JOIN source_revisions r ON r.id=json_extract(j.input_json,'$.revision_id') WHERE j.kind='GENERATE_ATOMICS' AND r.sha256=? ORDER BY j.created_at LIMIT 1",(document['sha256'],)).fetchone()
        checkpoints = c.execute("SELECT stage,created_at FROM job_checkpoints WHERE job_id=? AND stage LIKE 'parse.%' ORDER BY created_at",(job[0],)).fetchall() if job else []
    downloads = [v[1] for v in checkpoints if v[0].endswith('.download')]
    return {'mode':'RECONSTRUCTED_EXTERNAL_READY_INTERVAL','job_id':job[0] if job else None,
        'enqueue_at':job[1] if job else None,'all_external_downloads_ready_at':max(downloads) if downloads else None,
        'seconds':(instant(max(downloads))-instant(job[1])).total_seconds() if downloads and job else None,
        'normalization_checkpoint_at':next((v[1] for v in checkpoints if v[0]=='parse.normalized'),None),
        'qualification':'Queue enqueue to last external part download. A later local normalization checkpoint is recorded separately, not silently treated as MinerU service time.'}


def main(run):
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    library = Path(manifest['library'])
    with sqlite3.connect((library/'noodle.db').as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        calls = [dict(r) for r in c.execute('SELECT * FROM provider_calls ORDER BY created_at')]
        jobs = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' ORDER BY created_at")]
        events = [dict(r) for r in c.execute('SELECT * FROM job_stage_events ORDER BY created_at')]
    by_operation = defaultdict(list)
    for call in calls:
        by_operation[call['operation']].append(call)
    documents = []
    for job in jobs:
        revision = json.loads(job['input_json'])['revision_id']
        try:
            with urlopen(f"http://127.0.0.1:{manifest['port']}/internal/next/source-revisions/{revision}/readiness",timeout=30) as response:
                state = json.load(response)
        except (OSError, ValueError) as exc:
            state = {'read_error':str(exc)}
        mine = [v for v in calls if v['root_job_id'] == job['id'] or v['job_id'] == job['id']]
        stages = defaultdict(list)
        for call in mine:
            stages[group(call['operation'])].append(call)
        marks = [dict(v, detail=json.loads(v['detail_json'])) for v in events if v['job_id']==job['id'] and v['stage'] in {'claimed','atomic-index-ready','parse-started','parse-completed','source-plan-started','source-framework','chapter-completed','atomic-deferred'}]
        documents.append({'job_id':job['id'],'queue_status':job['status'],'created_at':job['created_at'],
            'readiness':state,'usage':totals(mine),'stage_calls':{k:totals(v) for k,v in stages.items()},'milestones':marks})
    api_calls = []
    for path in sorted((library/'artifacts/api-calls').glob('*/*.json')):
        v = json.loads(path.read_text(encoding='utf-8'))
        usage = v.get('usage') or {}
        api_calls.append({'receipt':str(path.relative_to(library)), 'operation':v.get('operation'), 'model':v.get('model'),
            'kind':v.get('kind'),'request_id':v['request_id'], 'status':v.get('status'), 'elapsed_ms':v.get('elapsed_ms',v.get('duration_ms')),
            'input_tokens':usage.get('prompt_tokens',usage.get('input_tokens',v.get('prompt_tokens'))),
            'output_tokens':usage.get('completion_tokens',usage.get('output_tokens',v.get('completion_tokens'))),
            'total_tokens':usage.get('total_tokens',v.get('total_tokens')),'started_at':v.get('started_at')})
    reranks = []
    for path in sorted((library/'artifacts/rerank').glob('*.json')):
        v=json.loads(path.read_text(encoding='utf-8'))
        reranks.append({'receipt':str(path.relative_to(library)),'usage':v['response'].get('usage'),'request_id':v['response'].get('request_id')})
    output = {'recorded_at':datetime.now(UTC).isoformat(),'schema_version':'formal-measurements-v1','documents':documents,
        'background_and_generation':totals(calls),'operations':{k:totals(v) for k,v in by_operation.items()},
        'api_usage':totals(api_calls),'api_receipts':api_calls,'rerank_receipts':reranks,
        'historical_mineru':{d['label']:historical_parse(d) for d in manifest['documents']},
        'dsh_frontend':json.loads((run/'dsh-acceptance.json').read_text(encoding='utf-8')) if (run/'dsh-acceptance.json').is_file() else None,
        'notes':['Observed call span includes queue gaps, recovery and supplier waits; service time sums concurrent calls.',
            'Historical MinerU time and charges are separate from current cache reads. Unknown token usage is not zero.',
            'Request receipts are independent from background provider_calls; rerank usage is reported separately.']}
    destination=run/'measurements'
    destination.mkdir(exist_ok=True)
    path=destination/(datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.json')
    path.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'path':str(path),'usage':output['background_and_generation'],'sources':[{'job_id':v['job_id'],'status':v['queue_status'],'coverage':v['readiness'].get('coverage')} for v in documents]},ensure_ascii=True))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    main(parser.parse_args().run.resolve())
