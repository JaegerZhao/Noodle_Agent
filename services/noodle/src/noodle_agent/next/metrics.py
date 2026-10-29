"""Call usage projection and a bounded timeline, independent of editable content."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from .repository import identifier, insert, now, one, rows

STAGES = [
    ("parse", "解析"),
    ("plan", "规划"),
    ("write", "笔记"),
    ("concept", "知识归并"),
    ("index", "检索索引"),
    ("organization", "结构整理"),
]


def stage_for(operation):
    if operation.startswith(("control.", "execution.")):
        return "control"
    if operation.startswith(('next.organization_','organization.')):
        return 'organization'
    if "concept" in operation or operation.startswith(("next.entity_", "next.organization_", "next.atomic_analysis", "next.canonical_")):
        return "concept"
    if "index" in operation or "embed" in operation:
        return "index"
    if "plan" in operation or "merge" in operation or "allocate" in operation or "structure" in operation or "framework" in operation or operation.startswith(("source.learning.objects.", "source.labels.")):
        return "plan"
    if (
        "chapter-completed" in operation
        or "note-review" in operation
        or "write" in operation
        or "assembly_" in operation
        or "section_repair" in operation
        or "source-draft" in operation
        or "judge" in operation
        or "publish" in operation
        or "visual" in operation
        or operation.startswith(('next.section_', 'next.local_finding_', "source.review.", "source.repair.", "source.deferred.", "atomic-deferred"))
    ):
        return "write"
    return "parse"


def root_for(c, job_id, seen=None):
    if not job_id:
        return None
    seen = set() if seen is None else seen
    if job_id in seen:
        return None
    seen.add(job_id)
    j = one(c, "SELECT * FROM jobs WHERE id=:j", j=job_id)
    if not j:
        return None
    if j["kind"] == "GENERATE_ATOMICS":
        return job_id
    data = json.loads(j["input_json"])
    p = data.get("payload", {})
    parent = p.get("job_id") or p.get("cause")
    root = root_for(c, parent, seen)
    if root:
        return root
    source = p.get('source_revision') or (p.get('routing_batch') or {}).get('source_revision_id')
    if source:
        generation = one(c, "SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1", r=source)
        if generation:
            return generation['id']
    revision = data.get("revision_id")
    proof = (
        one(
            c,
            "SELECT job_id FROM build_artifacts WHERE kind='PUBLICATION' AND storage_ref=:r LIMIT 1",
            r=f"artifacts/builds/{revision}.json",
        )
        if revision
        else None
    )
    return root_for(c, proof["job_id"], seen) if proof else None


def project_call(c, job_id, kind, values, ref, call_id=None):
    usage = values.get("usage") or {
        k: values.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    details = usage.get("prompt_tokens_details") or {}
    existing = one(c, "SELECT id FROM provider_calls WHERE artifact_ref=:r", r=ref)
    if existing:
        c.execute(
            text(
                "UPDATE provider_calls SET input_tokens=:i,output_tokens=:o,total_tokens=:t,elapsed_ms=:ms,root_job_id=:root WHERE artifact_ref=:r"
            ),
            {
                "i": usage.get("prompt_tokens", usage.get("input_tokens")),
                "o": usage.get("completion_tokens", usage.get("output_tokens")),
                "t": usage.get("total_tokens"),
                "ms": values.get("elapsed_ms", values.get("duration_ms")),
                "root": root_for(c, job_id),
                "r": ref,
            },
        )
        return
    insert(
        c,
        "provider_calls",
        id=call_id or identifier(),
        job_id=job_id,
        root_job_id=root_for(c, job_id),
        kind=kind,
        operation=values.get("operation", kind),
        model=values.get("model"),
        started_at=values.get("started_at"),
        elapsed_ms=values.get("elapsed_ms", values.get("duration_ms")),
        input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
        output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
        total_tokens=usage.get("total_tokens"),
        cached_input_tokens=usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")),
        status=values.get("status"),
        artifact_ref=ref,
        created_at=now(),
    )


def milliseconds(start, end):
    def parse(value):
        d = datetime.fromisoformat(value)
        return d.replace(tzinfo=UTC) if d.tzinfo is None else d

    return max(0, int((parse(end) - parse(start)).total_seconds() * 1000))


def model_usage(calls):
    """Reported tokens only; unknown usage remains unknown in audit fields."""
    groups = {}
    labels = {'LLM_CALL': 'LLM', 'VISION_CALL': '视觉', 'EMBEDDING_CALL': '嵌入', 'RERANK_CALL': '重排'}
    for call in calls:
        kind = call['kind']
        group = groups.setdefault(kind, {'kind': kind, 'label': labels.get(kind, kind), 'models': {}})
        model = call['model'] or kind
        item = group['models'].setdefault(model, {'model': model, 'total_tokens': 0, 'calls': 0, 'missing_usage': 0})
        item['calls'] += 1
        item['missing_usage'] += call['total_tokens'] is None
        item['total_tokens'] += call['total_tokens'] or 0
    result = []
    for group in groups.values():
        models = list(group['models'].values())
        result.append({**group, 'models': models, 'total_tokens': sum(m['total_tokens'] for m in models),
                       'calls': sum(m['calls'] for m in models), 'missing_usage': sum(m['missing_usage'] for m in models)})
    order = list(labels)
    return sorted(result, key=lambda item: order.index(item['kind']) if item['kind'] in order else len(order))


def task_clock(job, events, progress, calls, ctl, sampled_at):
    original_calls=[r for r in calls if r.get('job_id')==job.get('id')]
    generation_trace=bool(progress or any(stage_for(e['stage']) in {'parse','plan','write'} or e['stage']=='execution.attempt-start' for e in events) or original_calls)
    if job['status']=='SUCCEEDED' and not generation_trace:
        return {'active_ms':None,'wall_ms':None,'pause_ms':0,'blocked_ms':0,'sampled_at':sampled_at,
            'ticking':False,'clock_basis':'MISSING_GENERATION_TRACE','generation_timing_available':False}
    if job.get('attempts') == 0 and not generation_trace:
        wall = milliseconds(job['created_at'], sampled_at)
        pauses = paused_within(events, job['created_at'], sampled_at)
        return {'active_ms':0, 'wall_ms':wall, 'pause_ms':pauses, 'blocked_ms':0,
            'waiting_ms':max(0,wall-pauses), 'waiting_ticking':job['status'] in {'PENDING','RETRY'} and ctl['state']!='PAUSED',
            'sampled_at':sampled_at, 'ticking':False, 'clock_basis':'QUEUED_NOT_STARTED'}
    if any(e['stage']=='execution.attempt-start' for e in events):
        windows = execution_windows(events, sampled_at)
        ready = next((e['created_at'] for e in events if e['stage']=='atomic-index-ready'
            and json.loads(e.get('detail_json') or '{}').get('scope_key')=='document'), None)
        terminal = [e['created_at'] for e in events if e['stage']=='execution.attempt-end']
        ticking = (job['status']=='RUNNING' or job['status']=='PENDING' and pending_parse(events)) and ctl['state']!='PAUSED' and not ready
        end = ready or (sampled_at if ticking or job['status'] in {'PAUSED','PENDING','RETRY','REVIEW'} else max(terminal,default=job['created_at']))
        active = sum(max(0,milliseconds(a,min(b,end))-paused_within(events,a,min(b,end))) for a,b in windows if a<end)
        wall = milliseconds(job['created_at'],end)
        pauses = paused_within(events,job['created_at'],end)
        return {'active_ms':active,'wall_ms':wall,'pause_ms':pauses,'blocked_ms':0,
            'waiting_ms':max(0,wall-active-pauses),'sampled_at':sampled_at,'ticking':ticking,
            'clock_basis':'RECORDED_EXECUTION_ATTEMPTS','notes_ready_at':ready,
            'notes_ready_wall_ms':milliseconds(job['created_at'],ready) if ready else None}
    ticking = job['status'] in {'RUNNING', 'PENDING', 'RETRY'} and ctl['state'] != 'PAUSED'
    end = sampled_at if ticking or job['status'] == 'PAUSED' else max(
        [job['created_at'], *([progress['updated_at']] if progress else []),
         *(r['created_at'] for r in original_calls), *(e['created_at'] for e in events)])
    pauses, pause_start = 0, None
    for event in events:
        if event['stage'] == 'control.paused' and pause_start is None:
            pause_start = event['created_at']
        elif event['stage'] == 'control.resumed' and pause_start:
            pauses += milliseconds(max(pause_start, job['created_at']), min(event['created_at'], end))
            pause_start = None
    if job['status'] == 'PAUSED' and not pause_start:
        pause_start = ctl.get('updated_at') or (progress['updated_at'] if progress else sampled_at)
    if pause_start:
        pauses += milliseconds(max(pause_start, job['created_at']), end)
    wall = milliseconds(job['created_at'], end)
    blocked, blocked_start = 0, None
    for event in events:
        if event['stage'] == 'execution.blocked' and blocked_start is None:
            blocked_start = event['created_at']
        elif event['stage'] == 'execution.resumed' and blocked_start:
            start, stop = max(blocked_start, job['created_at']), min(event['created_at'], end)
            blocked += max(0, milliseconds(start, stop) - paused_within(events, start, stop))
            blocked_start = None
    if blocked_start:
        start = max(blocked_start, job['created_at'])
        blocked += max(0, milliseconds(start, end) - paused_within(events, start, end))
    return {'active_ms': max(0, wall - pauses - blocked), 'wall_ms': wall, 'pause_ms': pauses, 'blocked_ms': blocked,
            'sampled_at': sampled_at, 'ticking': ticking, 'clock_basis':'HISTORICAL_OBSERVED_SPAN'}


def pending_parse(events):
    """MinerU keeps working while the local worker yields between polls."""
    parsing, pending = False, False
    for event in events:
        if event['stage'] == 'parse-started':
            parsing = True
        elif event['stage'] in {'parse-completed', 'parse-reused'}:
            parsing, pending = False, False
        elif event['stage'] == 'execution.attempt-end':
            detail = json.loads(event.get('detail_json') or '{}')
            pending = detail.get('status') == 'DEFERRED' and (
                detail.get('deferred_stage') == 'PARSE_PENDING' or parsing)
    return pending


def execution_windows(events, sampled_at):
    """Count execution and remote parsing waits once, excluding review/restart gaps."""
    result, opened, parsing = [], None, False
    for event in events:
        if event['stage'] == 'parse-started':
            parsing = True
        elif event['stage'] in {'parse-completed', 'parse-reused'}:
            parsing = False
        elif event['stage']=='execution.attempt-start':
            if opened:
                result.append((opened,event['created_at']))
            opened = event['created_at']
        elif event['stage']=='execution.attempt-end' and opened:
            detail = json.loads(event.get('detail_json') or '{}')
            if detail.get('status') == 'DEFERRED' and (
                detail.get('deferred_stage') == 'PARSE_PENDING' or parsing):
                continue
            result.append((opened,event['created_at']))
            opened = None
    if opened:
        result.append((opened,sampled_at))
    return result


def interval_ms(windows):
    merged=[]
    for start,end in sorted(windows):
        if merged and start<=merged[-1][1]:
            merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:
            merged.append((start,end))
    return sum(milliseconds(a,b) for a,b in merged)


def paused_within(events, start, end):
    """Pause overlap with one stage; parallel stage durations are independent."""
    total, opened = 0, None
    for event in events:
        if event['stage'] == 'control.paused' and opened is None:
            opened = event['created_at']
        elif event['stage'] == 'control.resumed' and opened:
            total += milliseconds(max(start, opened), min(end, event['created_at']))
            opened = None
    if opened:
        total += milliseconds(max(start, opened), end)
    return total


def backfill(store):
    """Explicit one-time projection of historical receipts; idempotent by artifact ref."""
    with store.db.engine.connect() as c:
        artifacts = rows(
            c,
            "SELECT a.* FROM build_artifacts a LEFT JOIN provider_calls p ON p.artifact_ref=a.storage_ref WHERE a.kind IN ('LLM_CALL','VISION_CALL','EMBEDDING_CALL') AND (p.id IS NULL OR p.total_tokens IS NULL) ORDER BY a.created_at",
        )
    count = 0
    for a in artifacts:
        values = json.loads(store.read(a["storage_ref"], a["sha256"]))
        with store.repository.transaction() as c:
            project_call(c, a["job_id"], a["kind"], values, a["storage_ref"])
        count += 1
    return count


def timeline(store, job_id):
    from .task_control import control

    with store.db.engine.connect() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:j", j=job_id)
        if not job:
            raise KeyError(job_id)
        calls = rows(
            c,
            "SELECT * FROM provider_calls WHERE root_job_id=:j OR job_id=:j ORDER BY created_at",
            j=job_id,
        )
        events = rows(
            c, "SELECT * FROM job_stage_events WHERE job_id=:j ORDER BY created_at", j=job_id
        )
        progress = one(c, "SELECT * FROM job_progress WHERE job_id=:j", j=job_id)
        visual_cache_hits = c.exec_driver_sql("SELECT count(*) FROM job_checkpoints WHERE job_id=? AND stage LIKE 'visual-page-cache.%.reuse' AND state='DONE'",(job_id,)).scalar_one()
    with store.db.engine.connect() as c:
        descendants = []
        for j in rows(
            c,
            "SELECT * FROM jobs WHERE kind!='GENERATE_ATOMICS' AND status IN ('PENDING','RUNNING','RETRY','REVIEW')",
        ):
            if root_for(c, j["id"]) == job_id:
                descendants.append(j)
    ctl = control(store, job_id)
    sampled_at = now()
    clock = task_clock(job, events, progress, calls, ctl, sampled_at)
    end = sampled_at if clock['ticking'] or job['status'] == 'PAUSED' else max(
        [job['created_at'], *([progress['updated_at']] if progress else []), *(r['created_at'] for r in calls)])
    stages = []
    for key, label in STAGES:
        relevant = [r for r in calls if stage_for(r["operation"]) == key]
        stamps = [e["created_at"] for e in events if stage_for(e["stage"]) == key]
        status = "WAITING"
        if relevant or stamps:
            status = "COMPLETED"
        if progress and stage_for(progress["stage"]) == key and job["status"] == "RUNNING":
            status = "RUNNING"
        if key == 'parse' and job['status'] == 'PENDING' and pending_parse(events) and ctl['state'] != 'PAUSED':
            status = 'RUNNING'
        if (
            key in {"plan", "write"}
            and status == "COMPLETED"
            and job["status"] not in {"SUCCEEDED"}
        ):
            status = "PARTIAL"
        if job["status"] == "REVIEW" and progress and stage_for(progress["stage"]) == key:
            status = "REVIEW"
        if job["status"] == "PAUSED" and key == "write":
            status = "PAUSED"
        if key == 'plan' and any(e['stage']=='source-framework' for e in events):
            status = 'COMPLETED'
        if key == 'parse' and any(e['stage'] in {'parse-completed','parse-reused'} for e in events):
            status = 'COMPLETED' if any(e['stage']=='parse-completed' for e in events) else 'REUSED'
        pending = [
            j
            for j in descendants
            if (key == "concept" and j["kind"] in {"ANALYZE_ATOMIC", "BUILD_CONCEPT"})
            or (key == "index" and j["kind"] in {"INDEX_NOTE", "BUILD_INDEX"})
        ]
        if pending:
            status = (
                "RUNNING"
                if any(j["status"] == "RUNNING" for j in pending)
                else "PARTIAL"
                if relevant
                else "WAITING"
            )
        wall = 0
        windows = execution_windows(events,sampled_at) if clock.get('clock_basis')=='RECORDED_EXECUTION_ATTEMPTS' else None
        work_events = [e for e in events if stage_for(e['stage']) != 'control']
        for n, event in enumerate(work_events):
            if stage_for(event["stage"]) == key:
                stop = work_events[n + 1]["created_at"] if n + 1 < len(work_events) else end
                spans = [(max(event['created_at'],a),min(stop,b)) for a,b in windows] if windows is not None else [(event['created_at'],stop)]
                wall += sum(max(0,milliseconds(a,b)-paused_within(events,a,b)) for a,b in spans if a<b)
        if not wall and relevant:
            starts = [r["started_at"] for r in relevant if r["started_at"]]
            if starts:
                ends = [
                    (
                        datetime.fromisoformat(r["started_at"]).replace(tzinfo=UTC)
                        + timedelta(milliseconds=r["elapsed_ms"] or 0)
                    ).isoformat()
                    for r in relevant
                    if r["started_at"]
                ]
                stop = sampled_at if status == 'RUNNING' and clock['ticking'] else max(ends)
                wall = interval_ms([(r['started_at'],(datetime.fromisoformat(r['started_at'])+timedelta(milliseconds=r['elapsed_ms'] or 0)).isoformat()) for r in relevant if r['started_at']])
        stages.append(
            {
                "stage": key,
                "label": label,
                "status": status,
                "total_tokens": sum(r["total_tokens"] or 0 for r in relevant),
                "calls": len(relevant),
                "missing_usage": sum(r["total_tokens"] is None for r in relevant),
                "service_ms": sum(r["elapsed_ms"] or 0 for r in relevant),
                "wall_ms": wall,
                'timing_available':bool(stamps or any(r['elapsed_ms'] is not None and (r['started_at'] or r['created_at']) for r in relevant)),
                'usage_available':bool(relevant or stamps),
                'timing_basis':'EXECUTION_EVENTS' if windows is not None and stamps else 'PROVIDER_INTERVAL_UNION' if relevant else 'HISTORICAL_OBSERVED_SPAN',
                'ticking': status == 'RUNNING' and clock['ticking'],
                "started_at": min(stamps) if stamps else None,
                "ended_at": max(stamps) if stamps else None,
            }
        )
    result = {
        "job": dict(job),
        "control": ctl,
        "stages": stages,
        "progress": dict(progress) if progress else None,
        "summary": {
            **clock,
            'visual_cache_hits':visual_cache_hits,
            'model_usage': model_usage(calls),
            "total_tokens": sum(r["total_tokens"] or 0 for r in calls),
            "calls": len(calls),
            "missing_usage": sum(r["total_tokens"] is None for r in calls),
            "service_ms": sum(r["elapsed_ms"] or 0 for r in calls),
            "pending_maintenance": len(descendants),
            "recorded_since": "next_0007; older receipts require explicit backfill",
        },
    }
    from .imported_metrics import overlay
    return overlay(store,result)


def project_readiness(result):
    """Existing artifacts are not waiting jobs; missing historical timing is not zero."""
    state=result.get('readiness')
    if not state:
        return result
    stages={s['stage']:s for s in result['stages']}
    summary=result['summary']
    if summary.get('generation_timing_available') is False:
        summary['execution_note']='已有笔记可用；当前库未记录原始生成用时和用量，下方知识整理仅统计本库记录。'
        for key in ('parse','plan','write'):
            stage=stages[key]
            if not stage['calls']:
                stage.update(status='AVAILABLE' if state.get('coverage',{}).get('published') else 'WAITING',
                    timing_available=False,usage_available=False,ticking=False)
    concept=state.get('concept_maintenance',{})
    if concept and 'concept' in stages:
        stages['concept']['status']={'COMPLETE':'COMPLETED'}.get(concept['status'],concept['status'])
    structure=state.get('knowledge_structure',{})
    if structure and 'organization' in stages:
        stages['organization']['status']={'COMPLETE':'COMPLETED'}.get(structure['status'],structure['status'])
    return result
