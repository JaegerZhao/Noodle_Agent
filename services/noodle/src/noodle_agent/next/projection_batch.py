"""Coalesce queued global projections before reading their current inputs."""


def run_batch(store, lease, kind, operation, *, limit=64):
    extras = []
    for _ in range(limit - 1):
        extra = store.jobs.claim('projection-batch:' + lease.job_id, kind=kind)
        if extra is None:
            break
        extras.append(extra)
    try:
        result = operation()
    except Exception:
        for extra in extras:
            store.jobs.fail(extra, {'code': 'COALESCED_PROJECTION_RETRY'}, retry=True)
        raise
    for extra in extras:
        store.jobs.finish(extra, {**result, 'coalesced': True})
    return {**result, 'coalesced_jobs': len(extras)}
