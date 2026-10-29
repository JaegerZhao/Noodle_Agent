"""Read-only original generation statistics, kept outside the new usage ledger."""

import json

from .contracts import canonical, sha
from .repository import Conflict, identifier, insert, now, one, rows


def capture(source,target):
    from .metrics import model_usage, stage_for, timeline
    if source.root==target.root:
        raise Conflict('Historical display import requires a distinct target')
    copied=[]
    with target.db.engine.connect() as c:
        jobs=[dict(r) for r in rows(c,"SELECT * FROM jobs WHERE kind='GENERATE_ATOMICS' AND status='SUCCEEDED'")]
    for job in jobs:
        with source.db.engine.connect() as c:
            original=one(c,'SELECT * FROM jobs WHERE id=:j',j=job['id'])
            if not original or original['input_json']!=job['input_json']:
                raise Conflict('Original generation identity differs')
            events=[dict(r) for r in rows(c,'SELECT * FROM job_stage_events WHERE job_id=:j ORDER BY created_at,id',j=job['id'])]
            calls=[dict(r) for r in rows(c,'SELECT * FROM provider_calls WHERE root_job_id=:j OR job_id=:j ORDER BY created_at,id',j=job['id'])]
        primary=[r for r in calls if stage_for(r['operation']) in {'parse','plan','write'}]
        value=timeline(source,job['id'])
        if value['summary'].get('generation_timing_available') is False:
            continue
        proof=sha(canonical([dict(original),events,calls]))
        payload={'schema_version':'imported-generation-metrics-v1','source_metrics_sha256':proof,
            'source_job_id':job['id'],'source_library':str(source.root),
            'clock':{k:v for k,v in value['summary'].items() if k in {'active_ms','wall_ms','pause_ms','blocked_ms','waiting_ms','clock_basis','notes_ready_at','notes_ready_wall_ms','generation_timing_available'}},
            'stages':[s for s in value['stages'] if s['stage'] in {'parse','plan','write'}],
            'model_usage':model_usage(primary),'total_tokens':sum(r['total_tokens'] or 0 for r in primary)}
        ref=f"artifacts/imported-generation-metrics/{job['id']}.json"
        data=canonical(payload)
        target.put(ref,data.encode())
        with target.repository.transaction() as c:
            existing=one(c,'SELECT sha256 FROM build_artifacts WHERE storage_ref=:r',r=ref)
            if existing and existing['sha256']!=sha(data):
                raise Conflict('Original generation statistics changed; retain their first import')
            if not existing:
                insert(c,'build_artifacts',id=identifier(),job_id=job['id'],kind='IMPORTED_GENERATION_METRICS',
                    storage_ref=ref,sha256=sha(data),policy_version=target.repository.policy_version,input_sha256=proof,created_at=now())
        copied.append({'job_id':job['id'],'active_ms':payload['clock'].get('active_ms'),'source_metrics_sha256':proof})
    return {'jobs':copied,'new_provider_rows':0}


def overlay(store,result):
    ref=f"artifacts/imported-generation-metrics/{result['job']['id']}.json"
    with store.db.engine.connect() as c:
        artifact=one(c,"SELECT sha256 FROM build_artifacts WHERE storage_ref=:r AND kind='IMPORTED_GENERATION_METRICS'",r=ref)
    if not artifact:
        return result
    value=json.loads(store.read(ref,artifact['sha256']))
    result['summary'].update(value['clock'],ticking=False,generation_timing_available=True,
        imported_generation=True,original_model_usage=value['model_usage'],
        execution_note='原生成统计来自已保存的历史记录；本轮维护用量单独列出，不重复计入调用账本。')
    original={s['stage']:s for s in value['stages']}
    result['stages']=[original.get(s['stage'],s) for s in result['stages']]
    return result
