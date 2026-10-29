"""Durable paid-attempt limits survive transport failures and Job retries."""

import json
from collections import Counter

from .contracts import canonical, sha
from .jobs import require_lease
from .repository import identifier, insert, now, one, rows


def charge(store, lease, unit, operation, limit, *, request_sha256=None):
    key = sha(unit)
    with store.repository.transaction() as c:
        require_lease(c, lease)
        recovery = one(c, "SELECT id,created_at FROM events WHERE event_type='JOB_RETRY_REQUESTED' AND entity_id=:j ORDER BY rowid DESC LIMIT 1", j=lease.job_id)
        count = c.exec_driver_sql(
            "SELECT count(*) FROM build_artifacts WHERE job_id=? AND kind='MODEL_ATTEMPT' AND input_sha256=? AND created_at>=?",
            (lease.job_id, key, recovery['created_at'] if recovery else ''),
        ).scalar_one()
        completed = c.exec_driver_sql("SELECT count(*) FROM job_checkpoints WHERE job_id=? AND state='DONE'",(lease.job_id,)).scalar_one()
        if count == limit:
            previous = rows(c,"SELECT storage_ref,sha256 FROM build_artifacts WHERE job_id=:j AND kind='MODEL_ATTEMPT' AND input_sha256=:k AND created_at>=:t ORDER BY created_at",j=lease.job_id,k=key,t=recovery['created_at'] if recovery else '')
            history = [json.loads(store.read(r['storage_ref'],r['sha256'])) for r in previous]
            signatures = Counter(r['request_sha256'] for r in history if r.get('request_sha256'))
            repeated = bool(request_sha256 and signatures[request_sha256]>=2)
            progressed = completed>history[0].get('completed_checkpoints_before',completed) if history else False
            diagnostic = canonical({'policy':'OBSERVE_CONTINUE','unit':unit,'operation':operation,
                'threshold':limit,'attempt':count+1,'operation_counts':dict(Counter(r['operation'] for r in history)),
                'identical_request_repeated':repeated,'checkpoint_progress':progressed,
                'analysis':'REPEATED_REQUEST_WITHOUT_PROGRESS' if repeated and not progressed else 'COST_GROWTH_WITHOUT_PROVEN_LOOP',
                'action':'CONTINUE_BOUNDED_OPERATION; reuse signed cached responses and investigate repeated transport work; never reject valid content for cost alone',
                'recovery_event_id':recovery['id'] if recovery else None}).encode()
            ref = f'artifacts/cost-observations/{lease.job_id}-{key}-{recovery["id"] if recovery else "initial"}.json'
            store.put(ref, diagnostic)
            insert(c,'build_artifacts',id=identifier(),job_id=lease.job_id,kind='COST_OBSERVATION',storage_ref=ref,
                sha256=sha(diagnostic),policy_version=store.repository.policy_version,input_sha256=key,created_at=now())
        identity = identifier()
        data = canonical({"unit": unit, "operation": operation, "attempt": count + 1, "limit": limit, "at": now(),
                          'request_sha256':request_sha256,'completed_checkpoints_before':completed,
                          'recovery_event_id': recovery['id'] if recovery else None}).encode()
        ref = f"artifacts/attempts/{identity}.json"
        store.put(ref, data)
        insert(c, "build_artifacts", id=identity, job_id=lease.job_id, kind="MODEL_ATTEMPT",
               storage_ref=ref, sha256=sha(data), policy_version=store.repository.policy_version,
               input_sha256=key, created_at=now())
